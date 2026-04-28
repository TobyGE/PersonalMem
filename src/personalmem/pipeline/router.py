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

from ..ax import pruner as ax_pruner
from ..config import Config
import logging
from ..store.threads import Thread
from ..llm import call_llm, extract_text

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


@dataclass
class RouteDecision:
    action: str                     # 'continue' | 'new' | 'close_and_new'
    thread_id: str | None           # target thread for 'continue'
    close_thread_ids: list[str]     # threads to close (only for close_and_new)
    new_title: str | None           # title for new thread
    updated_title: str              # refined title for the affected thread (may upgrade existing)
    updated_summary: str            # the running narrative the affected thread should now have
    reason: str
    raw: str                        # raw LLM output for debugging


def _load_prompt() -> str:
    return resources.files("personalmem.prompts").joinpath("router.md").read_text()


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _render_thread_context(
    threads: Iterable[Thread],
    thread_captures: dict[str, list[CaptureView]],
    buffer_dir: Path | None = None,
) -> str:
    """Render each open thread with the FULL list of captures already routed
    to it. The router judges by complete activity history, not by an
    abstract narrative — concrete activities resist over-merging on
    surface topical overlap.

    No per-capture truncation: each capture's full pruned text + focused
    user_text reaches the LLM. If the prompt exceeds the context window
    that's a downstream problem to solve via budgeting / fewer threads
    in top-K, not by pre-truncating signal away.
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
        lines.append(f"    {len(caps)} captures so far:")
        for c in caps:
            ts_short = c.timestamp[11:19] if len(c.timestamp) > 11 else c.timestamp
            lines.append(f"      [{ts_short}] {c.app or '?'} — {c.window_title or ''}")
            if c.focused_value:
                lines.append(f"        user_text: {c.focused_value!r}")
            pruned = ax_pruner.load_pruned_text(c.id, buffer_dir=buffer_dir, fallback=c.visible_text or "")
            if pruned:
                lines.append(f"        visible_text: {pruned!r}")
            if c.url:
                lines.append(f"        url: {c.url}")
    return "\n".join(lines) if lines else "(none — no threads currently open)"


def route(
    cfg: Config,
    *,
    capture: CaptureView,
    open_threads: list[Thread],
    thread_captures: dict[str, list[CaptureView]] | None = None,
    buffer_dir: Path | None = None,
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

    # No length truncation on the new-capture block. Use AX-pruned
    # visible_text (chrome stripped) so chat-message bodies / verbatim
    # quotes the router needs for topic judgment actually reach the LLM.
    pruned_visible = ax_pruner.load_pruned_text(
        capture.id, buffer_dir=buffer_dir, fallback=capture.visible_text or ""
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
    raw = extract_text(response)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("router LLM returned non-JSON; defaulting to new thread. raw=%r", raw[:200])
        return RouteDecision(
            action="new",
            thread_id=None,
            close_thread_ids=[],
            new_title=_default_title(capture),
            updated_title=_default_title(capture),
            updated_summary="",
            reason="parse_error",
            raw=raw,
        )

    action = data.get("action") or "new"
    if action not in {"continue", "new", "close_and_new"}:
        action = "new"

    thread_id = data.get("thread_id")
    close_ids = data.get("close_thread_ids") or []
    if not isinstance(close_ids, list):
        close_ids = []
    new_title = data.get("new_title") or (_default_title(capture) if action != "continue" else None)
    updated_title = (data.get("updated_title") or "").strip()[:120]
    updated_summary = (data.get("updated_summary") or "").strip()
    reason = (data.get("reason") or "")[:300]

    open_ids = [t.id for t in open_threads]
    if action == "continue":
        resolved = _resolve_thread_id(thread_id, open_threads)
        if resolved is None:
            logger.warning(
                "router picked unknown thread_id=%r; falling back to new", thread_id
            )
            action = "new"
            thread_id = None
            new_title = new_title or _default_title(capture)
        else:
            if resolved != thread_id:
                logger.info(
                    "router thread_id %r resolved to %r (fuzzy match)",
                    thread_id, resolved,
                )
            thread_id = resolved

    close_ids = [
        tid for tid in (_resolve_thread_id(c, open_threads) for c in close_ids)
        if tid is not None
    ]

    return RouteDecision(
        action=action,
        thread_id=thread_id if action == "continue" else None,
        close_thread_ids=close_ids if action == "close_and_new" else [],
        new_title=new_title if action != "continue" else None,
        updated_title=updated_title,
        updated_summary=updated_summary,
        reason=reason,
        raw=raw,
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

    try:
        idx = int(s) - 1
        if 0 <= idx < len(open_threads):
            return open_threads[idx].id
    except ValueError:
        pass

    bare = s[4:] if s.startswith("thr_") else s
    if len(bare) >= 6:
        for tid in open_ids:
            tid_bare = tid[4:] if tid.startswith("thr_") else tid
            if len(tid_bare) == len(bare):
                diffs = sum(1 for a, b in zip(tid_bare, bare) if a != b)
                if diffs <= 2:
                    return tid

    return None
