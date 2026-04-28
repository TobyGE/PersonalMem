"""Thread summarizer: invoked once per thread on closure.

Unlike session_reducer (time-window based, runs every flush_minutes), this
runs only when a thread closes — so it sees the entire arc and can dedupe
draft evolution, extract outcomes, and refine the title.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..ax import pruner as ax_pruner
from ..config import Config
import logging
from ..llm import call_llm, extract_text
from .router import CaptureView

logger = logging.getLogger("personalmem.pipeline.summarizer")

SUMMARIZER_STAGE = "thread_summarizer"


@dataclass
class ThreadSummary:
    title: str
    narrative: str
    key_events: list[str]
    outcome: str
    raw: str


def _load_prompt() -> str:
    return resources.files("personalmem.prompts").joinpath("summarizer.md").read_text()


def _render_captures(captures: list[CaptureView], buffer_dir: Path | None = None) -> str:
    """Render captures for the thread-summary prompt.

    No length truncation. Loads AX-pruned visible_text from the
    capture-buffer JSON (chrome buttons stripped, parent-child duplicates
    collapsed) so the LLM sees signal-dense content. Truncating at small
    char limits silently dropped chat-message bodies (WeChat) and verbatim
    quotes (typed code) — never do that here.
    """
    lines: list[str] = []
    for c in captures:
        ts = c.timestamp
        bits = [f"[{ts}] {c.app}"]
        if c.window_title:
            bits.append(f"win={c.window_title!r}")
        if c.focused_role:
            bits.append(f"role={c.focused_role}")
        if c.url:
            bits.append(f"url={c.url!r}")
        if c.focused_value:
            bits.append(f"input={c.focused_value!r}")
        pruned = ax_pruner.load_pruned_text(c.id, buffer_dir=buffer_dir, fallback=c.visible_text or "")
        if pruned:
            bits.append(f"text={pruned!r}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def summarize(
    cfg: Config,
    *,
    thread_id: str,
    title: str,
    opened_at: str,
    closed_at: str,
    captures: list[CaptureView],
    buffer_dir: Path | None = None,
) -> ThreadSummary:
    template = _load_prompt()
    rendered = template.format(
        thread_id=thread_id,
        title=title,
        opened_at=opened_at,
        closed_at=closed_at,
        capture_count=len(captures),
        captures_text=_render_captures(captures, buffer_dir=buffer_dir),
    )

    response = call_llm(
        cfg,
        SUMMARIZER_STAGE,
        messages=[{"role": "user", "content": rendered}],
        json_mode=True,
    )
    raw = extract_text(response)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("summarizer returned non-JSON; using fallback. raw=%r", raw[:200])
        return ThreadSummary(
            title=title,
            narrative="(LLM output failed to parse; fallback summary unavailable)",
            key_events=[],
            outcome="unclear",
            raw=raw,
        )

    return ThreadSummary(
        title=(data.get("title") or title).strip()[:120],
        narrative=(data.get("narrative") or "").strip(),
        key_events=[str(x).strip() for x in (data.get("key_events") or []) if str(x).strip()],
        outcome=(data.get("outcome") or "unclear").strip(),
        raw=raw,
    )
