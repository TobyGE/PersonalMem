"""Q&A over thread memory.

Single-stage retrieval+answer: send all active thread digests (title,
time range, narrative, key events) to a powerful LLM and let it both
pick the relevant threads AND answer with citations. No embedding index,
no BM25 — the LRU-bounded active-thread set (~200) fits comfortably in
GPT-5.5's context.

The default model is ``gpt-5.5`` via Codex OAuth (free for ChatGPT
Plus / Pro / Team subscribers). Configurable via ``[models.qa]``.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from importlib import resources
from pathlib import Path

from .config import Config
from .store import threads as thread_store


_DEFAULT_QA_MODEL = "gpt-5.5"

# Per-thread capture cap when building digests. Each capture line is
# ~120 chars (timestamp + app + URL + description) so 5 caps × 200
# threads × 120 = ~120KB before narrative/key_events — well within
# GPT-5.5's context window. Bump if you want more drill-down.
_CAPTURES_PER_THREAD = 5

# Rough byte budget for the assembled threads_block. If the digest set
# would exceed this, we drop per-capture detail (digest-only fallback)
# so the answer call still goes through.
_DIGEST_BLOCK_MAX_BYTES = 180 * 1024


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _fmt_capture_line(row: sqlite3.Row, description: str) -> str:
    """One-line representation of a capture for the Q&A digest.

    Shape: ``HH:MM [App] url — description``. URL and description are
    truncated; we keep timestamps to the minute (date already implied
    by thread's time range header).
    """
    ts = (row["timestamp"] or "")
    # Extract HH:MM from ISO timestamp 2026-05-09T14:32:30-04:00
    hhmm = ts[11:16] if len(ts) >= 16 else ts[:16]
    app = (row["app_name"] or "?").strip()
    url = (row["url"] or "").strip()
    desc = (description or "").strip()
    win = (row["window_title"] or "").strip()
    head = f"- {hhmm} [{app}]"
    extras: list[str] = []
    if url:
        extras.append(_truncate(url, 80))
    elif win:
        extras.append(_truncate(win, 60))
    if desc:
        extras.append(_truncate(desc, 140))
    return head + (" " + " — ".join(extras) if extras else "")


def _thread_digest(
    t: thread_store.Thread, *,
    narrative_chars: int = 400,
    capture_rows: list[tuple[sqlite3.Row, str]] | None = None,
    capture_count: int = 0,
) -> str:
    """Render a single thread as a compact block for the Q&A prompt.

    If ``capture_rows`` is provided (a list of ``(row, description)``
    tuples sampled from the thread), they're appended as a "Recent
    captures" sub-section — that's the drill-down level where URLs
    and exact timestamps become visible to the answer model.
    """
    narrative = (t.narrative or t.summary or "").strip()
    narrative = _truncate(narrative, narrative_chars)
    closed = t.closed_at or t.last_active_at
    parts = [
        f"## [{t.id}] {t.title}",
        f"_{t.opened_at} → {closed} · {capture_count} captures_",
    ]
    if narrative:
        parts.append(narrative)
    try:
        events = json.loads(t.key_events_json or "[]")
    except (TypeError, ValueError):
        events = []
    if isinstance(events, list) and events:
        bullets = "\n".join(f"- {str(e).strip()}" for e in events[:8])
        parts.append("**Key events:**\n" + bullets)
    if t.outcome:
        parts.append(f"_outcome: {t.outcome}_")
    if capture_rows:
        cap_lines = [_fmt_capture_line(r, d) for r, d in capture_rows]
        parts.append("**Recent captures:**\n" + "\n".join(cap_lines))
    return "\n".join(parts)


def _sample_capture_rows(
    in_conn: sqlite3.Connection,
    out_conn: sqlite3.Connection,
    thread_id: str,
    *,
    cap: int,
) -> list[tuple[sqlite3.Row, str]]:
    """Fetch up to ``cap`` captures from a thread.

    Strategy: if the thread has ≤ cap captures, return all chronological.
    Otherwise return the first 2 + last (cap - 2) so the model sees both
    how the thread started and where it ended up — better signal than
    the tail alone for "how did this thread evolve" questions.
    """
    all_ids = thread_store.thread_capture_ids(out_conn, thread_id)
    if not all_ids:
        return []
    if len(all_ids) <= cap:
        sample_ids = all_ids
    else:
        sample_ids = all_ids[:2] + all_ids[-(cap - 2):]
    ph = ",".join("?" * len(sample_ids))
    rows = in_conn.execute(
        "SELECT id, timestamp, app_name, window_title, focused_role, "
        "       focused_value, visible_text, url "
        f"  FROM captures WHERE id IN ({ph}) "
        " ORDER BY timestamp ASC",
        sample_ids,
    ).fetchall()
    desc_for = {
        r["capture_id"]: (r["description"] or "")
        for r in out_conn.execute(
            f"SELECT capture_id, description FROM thread_captures "
            f"WHERE thread_id = ? AND capture_id IN ({ph})",
            (thread_id, *sample_ids),
        )
    }
    return [(r, desc_for.get(r["id"], "")) for r in rows]


def gather_thread_digests(
    out_conn: sqlite3.Connection, *,
    in_conn: sqlite3.Connection | None = None,
    since: str | None = None,
    until: str | None = None,
    max_threads: int | None = None,
    captures_per_thread: int = _CAPTURES_PER_THREAD,
    byte_budget: int = _DIGEST_BLOCK_MAX_BYTES,
) -> list[str]:
    """Build per-thread digest blocks.

    Drill-down: when ``in_conn`` is supplied, each digest appends a
    ``Recent captures`` section showing up to ``captures_per_thread``
    capture lines (timestamp + app + URL + description) — that's what
    lets the answer model cite exact URLs and minute-resolution times.

    Budget: if the full set of capture-rich digests would exceed
    ``byte_budget``, we degrade to digest-only mode (no per-capture
    section). This stops a runaway active-thread count from blowing up
    the prompt size.

    Archived threads ARE included — Q&A is the long-term-recall surface,
    so LRU eviction (which removes threads from the router's candidate
    set) must not also hide them from search. Use the LRU's
    ``max_active_threads`` config to bound the router; use ``--max-threads``
    here to bound the Q&A retrieval slice independently.

    Ordering of filters: time-window FIRST (so an older `--since` doesn't
    lose its matches to a recency-biased LIMIT), then ``max_threads``
    cap on the in-scope set, then budget. Trimming for budget drops the
    oldest threads first (they're returned ordered by recency desc).
    """
    # Fetch unbounded; the time-window predicate runs in Python below.
    # Pushing time predicates into SQL would shave a few rows but the
    # active set is already capped by LRU and Python filtering keeps
    # the policy (window before LIMIT) explicit.
    threads = thread_store.list_recent_threads(
        out_conn, top_k=None, include_archived=True,
    )

    # Filter by time window first so an older window doesn't lose matches
    # to recency-biased LIMITing.
    in_scope: list[thread_store.Thread] = []
    for t in threads:
        if since and t.last_active_at < since:
            continue
        if until and t.opened_at > until:
            continue
        in_scope.append(t)
        if max_threads is not None and len(in_scope) >= max_threads:
            break
    if not in_scope:
        return []

    # Build digest-only versions first as a fallback baseline.
    counts = {t.id: thread_store.thread_capture_count(out_conn, t.id) for t in in_scope}
    digests_basic = [
        _thread_digest(t, capture_count=counts[t.id]) for t in in_scope
    ]

    if in_conn is None or captures_per_thread <= 0:
        return _trim_to_budget(digests_basic, byte_budget)

    # Try the rich (capture-augmented) version, falling back if budget
    # would blow.
    digests_rich: list[str] = []
    rich_size = 0
    over_budget = False
    for t in in_scope:
        rows = _sample_capture_rows(
            in_conn, out_conn, t.id, cap=captures_per_thread,
        )
        d = _thread_digest(t, capture_rows=rows, capture_count=counts[t.id])
        digests_rich.append(d)
        rich_size += len(d.encode("utf-8"))
        if rich_size > byte_budget:
            over_budget = True
            break
    if over_budget:
        # Rich blew budget; basic might fit. If basic also blows, trim
        # from the tail (oldest threads) until it fits.
        return _trim_to_budget(digests_basic, byte_budget)
    return digests_rich


def _trim_to_budget(blocks: list[str], byte_budget: int) -> list[str]:
    """Keep blocks in order (recency desc) until the cumulative UTF-8
    size would exceed ``byte_budget``. Drops oldest entries silently.
    """
    kept: list[str] = []
    total = 0
    for b in blocks:
        size = len(b.encode("utf-8"))
        if total + size > byte_budget and kept:
            break
        kept.append(b)
        total += size
    return kept


def _is_codex_model(model: str) -> bool:
    """ChatGPT-Codex-only models that must go through the chatgpt.com
    Responses endpoint (the public OpenAI API rejects this OAuth token)."""
    m = model.lower()
    return "gpt-5.5" in m or "gpt-5.3-codex" in m


def _stream_qa_llm(cfg: Config, messages: list[dict]) -> Iterator[str]:
    """Provider-aware streaming dispatch for Q&A.

    Resolution order:
      1. ``[models.qa]`` explicitly set → respect the configured model,
         api_key, base_url. Codex-only models (gpt-5.5 / gpt-5.3-codex)
         go through Codex OAuth; everything else goes through litellm
         with ``stream=True``.
      2. No override → default to gpt-5.5 via Codex OAuth.
    """
    qa_cfg = cfg.models.get("qa")
    if qa_cfg is None or not qa_cfg.model:
        from .llm import codex_oauth
        yield from codex_oauth.stream_codex_oauth(
            model=_DEFAULT_QA_MODEL, messages=messages,
        )
        return

    from .config import resolve_api_key
    model_id = qa_cfg.model
    api_key = resolve_api_key(qa_cfg)

    # Codex-only model AND no API key configured → use the OAuth path.
    if _is_codex_model(model_id) and not api_key:
        from .llm import codex_oauth
        yield from codex_oauth.stream_codex_oauth(
            model=model_id.split("/", 1)[-1], messages=messages,
        )
        return

    # Everything else: dispatch through litellm with streaming. Works
    # for OpenAI API, Ollama, LM Studio (OpenAI-compatible), Gemini,
    # etc. — anything the rest of the pipeline already speaks.
    import litellm
    kwargs: dict = {"model": model_id, "messages": messages, "stream": True}
    if qa_cfg.base_url:
        kwargs["api_base"] = qa_cfg.base_url
    if api_key:
        kwargs["api_key"] = api_key
    if qa_cfg.max_tokens:
        kwargs["max_tokens"] = qa_cfg.max_tokens
    if qa_cfg.num_ctx:
        kwargs["num_ctx"] = qa_cfg.num_ctx
    for chunk in litellm.completion(**kwargs):
        try:
            delta = chunk.choices[0].delta.content or ""
        except (AttributeError, IndexError, TypeError):
            delta = ""
        if delta:
            yield delta


def _build_prompt(
    *, threads_block: str, question: str, history: list[tuple[str, str]] | None = None,
) -> str:
    """Render the Q&A prompt with optional chat history.

    History is a list of (role, content) where role ∈ {"user","assistant"}.
    It's rendered as a `# Conversation so far` block inserted between
    the thread reference data and the current question.
    """
    template = resources.files("personalmem.prompts").joinpath("qa_answer.md").read_text()
    today = datetime.now().date().isoformat()
    base = template.format(
        today=today, threads_block=threads_block, question=question,
    )
    if not history:
        return base
    # Inject the history just before the # Question section.
    convo_lines = ["", "# Conversation so far", ""]
    for role, content in history:
        speaker = "User" if role == "user" else "Assistant"
        convo_lines.append(f"**{speaker}:** {content.strip()}")
        convo_lines.append("")
    convo_block = "\n".join(convo_lines)
    # The template ends with `# Question\n\n{question}\n`. Insert convo
    # right before `# Question`.
    marker = "# Question"
    idx = base.rfind(marker)
    if idx < 0:
        return base + "\n" + convo_block
    return base[:idx] + convo_block + base[idx:]


def ask_stream(
    cfg: Config,
    *,
    out_conn: sqlite3.Connection,
    in_conn: sqlite3.Connection | None,
    question: str,
    since: str | None = None,
    until: str | None = None,
    max_threads: int | None = None,
    history: list[tuple[str, str]] | None = None,
) -> Iterator[str]:
    """Stream the answer text deltas. Caller prints each yielded chunk.

    Pass ``in_conn`` to enable capture-level drill-down in the prompt
    (URLs, timestamps, focused values become visible to the model).
    Pass ``history`` for multi-turn conversation continuity.
    """
    blocks = gather_thread_digests(
        out_conn, in_conn=in_conn,
        since=since, until=until, max_threads=max_threads,
    )
    if not blocks:
        yield "_(no threads matched the time window)_"
        return

    prompt = _build_prompt(
        threads_block="\n\n".join(blocks),
        question=question, history=history,
    )
    yield from _stream_qa_llm(
        cfg,
        messages=[
            {"role": "system", "content": "You are PersonalMem's Q&A assistant."},
            {"role": "user", "content": prompt},
        ],
    )


def expand_thread_citations(text: str, *, out_dir: Path) -> str:
    """Replace inline `[thr_xxxxxxxx]` markers in a finished answer with
    a footer listing each cited thread's local .md file path.

    The streaming output already includes the bracketed IDs; we just
    append a "## Sources" block after the answer so the user can drill
    down into the full thread .md.
    """
    import re
    ids = sorted(set(re.findall(r"\[(thr_[0-9a-f]+)\]", text)))
    if not ids:
        return text
    lines = ["", "", "## Sources"]
    for tid in ids:
        md_path = out_dir / f"{tid}.md"
        lines.append(f"- `{tid}` → {md_path}")
    return text + "\n".join(lines)
