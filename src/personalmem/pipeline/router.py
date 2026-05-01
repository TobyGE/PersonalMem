"""LLM-driven thread router: decides whether a new capture continues an open
thread, opens a new one, or closes some threads first.

This is the *only* LLM call on the hot path — keep it cheap. Summarization is
deferred to thread closure (see thread_summarizer.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable

from ..capture import ax_pruner, vision_ocr
from ..config import Config
import logging
from ..store.threads import Thread
from ..llm import call_llm, extract_full_text, extract_json_text, extract_text

logger = logging.getLogger("personalmem.pipeline.router")

ROUTER_STAGE = "thread_router"


@dataclass
class CaptureView:
    """Compact projection of a capture row, suitable for prompt rendering."""
    id: str
    timestamp: str
    app: str
    window_title: str
    focused_role: str
    focused_value: str
    url: str
    visible_text: str
    description: str = ""    # router-generated one-liner, populated for history captures


@dataclass
class RouteDecision:
    action: str                     # 'continue' | 'new'
    thread_id: str | None           # target thread for 'continue'
    reason: str
    capture_description: str        # one-sentence concrete description of THIS new capture
    raw: str                        # raw LLM output for debugging


def _load_prompt() -> str:
    return resources.files("personalmem.prompts").joinpath("router.md").read_text()


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


# Per-thread history cap rendered into the router prompt. The full capture
# list balloons prompts to 30K+ tokens once a thread accumulates 50+
# captures (e.g. a long coding session). The most recent N captures carry
# enough activity-pattern signal for routing decisions; older ones are
# essentially redundant for "is this new capture a continuation".
_HISTORY_PER_THREAD = 5


def _render_thread_context(
    threads: Iterable[Thread],
    thread_captures: dict[str, list[CaptureView]],
    buffer_dir: Path | None = None,
) -> str:
    """Render each open thread with up to _HISTORY_PER_THREAD most recent
    captures so the router can judge by activity pattern (concrete actions,
    not abstract topic summaries — those over-merge on "all about AI").

    Older captures are summarized as a count; the title line still shows
    how many total captures exist so the LLM knows the thread's scale.
    """
    lines: list[str] = []
    for i, t in enumerate(threads, 1):
        lines.append(
            f"[{i}] id={t.id} title={t.title!r} last_active={t.last_active_at}"
        )
        caps = thread_captures.get(t.id, [])
        if not caps:
            lines.append("    (no captures yet)")
            continue
        if len(caps) <= _HISTORY_PER_THREAD:
            lines.append(f"    {len(caps)} captures so far:")
            shown = caps
        else:
            hidden = len(caps) - _HISTORY_PER_THREAD
            lines.append(
                f"    {len(caps)} captures so far "
                f"(showing last {_HISTORY_PER_THREAD}; {hidden} earlier hidden):"
            )
            shown = caps[-_HISTORY_PER_THREAD:]
        for c in shown:
            # History captures get rendered as a one-line activity log
            # using the router-generated description (cached on the
            # thread_captures row when the capture was first routed).
            # No raw AX dump — keeps the prompt tight (~40-100 chars per
            # capture instead of ~1.5 KB), which is the difference
            # between a 5K and a 50K token routing prompt.
            ts_short = c.timestamp[11:19] if len(c.timestamp) > 11 else c.timestamp
            line = f"      [{ts_short}] {c.app or '?'} — {c.window_title or ''}"
            if c.description:
                line += f"\n        » {c.description}"
            lines.append(line)
    return "\n".join(lines) if lines else "(none — no threads currently open)"


def _visible_text_for(
    capture_id: str,
    *,
    buffer_dir: Path | None,
    fallback: str,
    extra_capture_ids: tuple[str, ...] = (),
) -> str:
    """Pruned AX text for the focal capture + OCR text merged across the
    capture and any folded-by-coalesce siblings (line-level dedup with
    fuzzy matching for OCR jitter).

    Combining is just concatenation with a section break: the LLM
    handles a "[OCR]:" header fine.
    """
    pruned = ax_pruner.load_pruned_text(
        capture_id, buffer_dir=buffer_dir, fallback=fallback,
    )
    ocr = vision_ocr.merge_ocr_texts(
        (capture_id, *extra_capture_ids), buffer_dir=buffer_dir,
    )
    if not ocr:
        return pruned
    if not pruned:
        return f"[OCR]:\n{ocr}"
    return f"{pruned}\n\n[OCR]:\n{ocr}"


def route(
    cfg: Config,
    *,
    capture: CaptureView,
    open_threads: list[Thread],
    thread_captures: dict[str, list[CaptureView]] | None = None,
    buffer_dir: Path | None = None,
    folded_capture_ids: tuple[str, ...] = (),
) -> RouteDecision:
    """Ask the LLM where this capture belongs.

    The router sees each open thread's complete capture history so it can
    judge by *activity pattern* (what's actually been happening in the
    thread) rather than by an abstract narrative summary. The latter
    over-merged on topical overlap (work + podcast both 'about AI' got
    glued together); concrete activities preserve the work / leisure
    boundary.
    """
    thread_captures = thread_captures or {}
    template = _load_prompt()

    # No length truncation on the new-capture block. Pruned AX (chrome
    # stripped) plus OCR text merged across the focal capture and any
    # folded-by-coalesce siblings, so videos/canvas captures aren't
    # blind and ephemeral OCR signals (subtitle that flashed in frame 2
    # but not frame 5) survive to the LLM.
    pruned_visible = _visible_text_for(
        capture.id,
        buffer_dir=buffer_dir,
        fallback=capture.visible_text or "",
        extra_capture_ids=tuple(folded_capture_ids),
    )
    rendered = template.format(
        open_threads_block=_render_thread_context(open_threads, thread_captures, buffer_dir=buffer_dir),
        ts=capture.timestamp,
        app=capture.app or "?",
        window_title=capture.window_title or "",
        focused_role=capture.focused_role or "",
        focused_value=capture.focused_value or "",
        url=capture.url or "",
        visible_text=pruned_visible,
    )

    response = call_llm(
        cfg,
        ROUTER_STAGE,
        messages=[{"role": "user", "content": rendered}],
        json_mode=True,
    )
    # Keep the full untouched output (incl. side-channel reasoning_content
    # from LM Studio / DeepSeek-R1, in-band <think>...</think> from Qwen3,
    # code fences, leading prose) for debugging in _decisions.json. The
    # cleaned form is only used for json.loads().
    raw_full = extract_full_text(response)
    cleaned = extract_json_text(response)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("router LLM returned non-JSON; defaulting to new thread. "
                       "stripped=%r full=%r", cleaned[:200], raw_full[:300])
        return RouteDecision(
            action="new",
            thread_id=None,
            reason="parse_error",
            capture_description="",
            raw=raw_full,
        )

    action = data.get("action") or "new"
    if action not in {"continue", "new"}:
        action = "new"

    thread_id = data.get("thread_id")
    reason = (data.get("reason") or "")[:300]

    if action == "continue":
        resolved = _resolve_thread_id(thread_id, open_threads)
        if resolved is None:
            logger.warning(
                "router picked unknown thread_id=%r; falling back to new", thread_id
            )
            action = "new"
            thread_id = None
        else:
            if resolved != thread_id:
                logger.info(
                    "router thread_id %r resolved to %r (fuzzy match)",
                    thread_id, resolved,
                )
            thread_id = resolved

    capture_description = (data.get("capture_description") or "").strip()[:300]

    return RouteDecision(
        action=action,
        thread_id=thread_id if action == "continue" else None,
        reason=reason,
        capture_description=capture_description,
        raw=raw_full,
    )


def _default_title(capture: CaptureView) -> str:
    base = capture.window_title or capture.app or "Untitled"
    return _truncate(base, 60) or "Untitled"


def _resolve_thread_id(raw, open_threads: list[Thread]) -> str | None:
    """Forgiving thread-id resolver. Handles small LLM transcription mistakes
    (missing ``thr_`` prefix, hex typos, returning the [N] index instead of
    the id) while still rejecting genuinely-wrong picks.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    open_ids = [t.id for t in open_threads]
    open_set = set(open_ids)

    if s in open_set:
        return s

    if not s.startswith("thr_"):
        with_prefix = f"thr_{s}"
        if with_prefix in open_set:
            return with_prefix

    # Numeric index — model returned "3" or "thr_3" intending the [3]
    # row in the prompt's open-threads listing.
    idx_candidate = s[4:] if s.startswith("thr_") else s
    try:
        idx = int(idx_candidate) - 1
        if 0 <= idx < len(open_threads):
            return open_threads[idx].id
    except ValueError:
        pass

    bare = s[4:] if s.startswith("thr_") else s
    if len(bare) >= 6:
        # Same-length hex typos (≤ 2 char-position diffs)
        for tid in open_ids:
            tid_bare = tid[4:] if tid.startswith("thr_") else tid
            if len(tid_bare) == len(bare):
                diffs = sum(1 for a, b in zip(tid_bare, bare) if a != b)
                if diffs <= 2:
                    return tid

        # Missing/extra char(s): the LLM dropped or duplicated a hex
        # nibble. e.g. real "fe2efe0aba35" → emitted "2efe0aba35".
        # Accept if the candidate is a substring of exactly one real id
        # AND it covers ≥ 80% of that id's bare length (avoids matching
        # arbitrarily short suffixes that could ambiguously hit several
        # ids).
        cands = [
            tid for tid in open_ids
            if (
                bare in (tid[4:] if tid.startswith("thr_") else tid)
                and len(bare) >= 0.8 * len(tid[4:] if tid.startswith("thr_") else tid)
            )
        ]
        if len(cands) == 1:
            return cands[0]

    return None
