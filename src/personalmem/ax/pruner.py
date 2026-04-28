"""AX tree pruning: drop UI chrome and structural redundancy from AX dumps
before they reach the LLM.

Three kinds of cleanup:

1. **Chrome buttons**: AXButton nodes whose title is a known piece of UI
   plumbing (Send / Search / Back / Voice input / etc) — these dominate AX
   dumps for chat and browser apps but carry zero task signal.

2. **Parent-child same-title duplicates**: when a node has exactly one child
   whose title matches the parent's title (and the parent has no own value),
   collapse the wrapper. Pattern: `[Button] Mobile / [Button] Mobile`.

3. **Empty containers**: AXGroup / AXSplitter / AXSplitGroup nodes with no
   own text get their children promoted to the same depth (keeps text but
   drops one level of indentation noise).

The output mirrors `ax_app_to_markdown` so it can drop in as a `visible_text`
replacement — same `## App / ### window / - bullets` shape.

This module is import-safe (no live-daemon side effects). The replay scripts
opt into it; the live capture path keeps using `ax_app_to_markdown` for now.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Buttons whose label is a UI affordance with no task signal.
# When an AXButton matches, we drop the button AND its children (their
# children are typically tooltip mirrors or icon labels — pure plumbing).
_CHROME_BUTTON_TITLES = frozenset({
    # WeChat sidebar & chat toolbar
    "WeChat", "Contacts", "Favorites", "Moments", "Search",
    "Mini Programs Panel", "Mobile", "More",
    "Chat History", "Import chat history from phone",
    "Voice Call", "Chat Info", "Send File",
    "Send Favorites​Item",
    "Hide window screenshot",
    "Shortcuts",
    # Chrome window chrome
    "Back", "Forward", "Reload this page", "Reload",
    "Home", "Customize and control Google Chrome",
    "View site information", "Address and search bar",
    "Bookmark this tab", "Side panel", "Customize Chrome",
    "Edison (YG)",
    "Show side panel", "Show bookmarks bar",
    # macOS generic window controls
    "Hide", "Show", "Close", "Minimize", "Maximize",
    "Toggle", "Done", "Cancel", "OK",
    # menu bar items that show up everywhere
    "Help", "Window", "View", "Edit", "File", "Format", "Tools",
})

# Buttons whose title starts with one of these prefixes are also chrome.
_CHROME_BUTTON_PREFIXES = (
    "Send sticker",
    "Send Favorites",
    "Send File",
    "Voice input",
    "Hide window",
    "Screenshot",
    "View site information",
)


def is_chrome_button(title: str) -> bool:
    if not title:
        return False
    if title in _CHROME_BUTTON_TITLES:
        return True
    return any(title.startswith(p) for p in _CHROME_BUTTON_PREFIXES)


def prune_ax_app(app_data: dict[str, Any]) -> str:
    """Render one AX-app subtree as pruned LLM-friendly markdown.

    Mirrors ``ax_models.ax_app_to_markdown`` shape so callers can swap in.
    """
    lines: list[str] = []
    name = app_data.get("name", "Unknown")
    badge = " [active]" if app_data.get("is_frontmost") else ""
    bundle = app_data.get("bundle_id", "")
    lines.append(f"## {name}{badge}")
    if bundle:
        lines.append(f"_{bundle}_")
    for win in app_data.get("windows", []):
        title = win.get("title") or "(untitled)"
        lines.append(f"### {title}")
        for el in win.get("elements", []):
            _walk(el, lines, depth=0)
    return "\n".join(lines)


def prune_ax_tree(ax_tree: dict[str, Any]) -> str:
    """Pick the frontmost app and prune-render it. Empty string if no app."""
    apps = ax_tree.get("apps") or []
    chosen = None
    for app in apps:
        if app.get("is_frontmost"):
            chosen = app
            break
    if chosen is None and apps:
        chosen = apps[0]
    if chosen is None:
        return ""
    return prune_ax_app(chosen)


def load_pruned_text(
    capture_id: str,
    *,
    buffer_dir: Path | None = None,
    fallback: str = "",
) -> str:
    """Read the capture-buffer JSON for ``capture_id`` and return pruned text.

    Returns ``fallback`` if the JSON is missing/malformed (e.g. the capture
    has been screenshot-stripped or evicted by retention policy).
    """
    if buffer_dir is None:
        buffer_dir = Path.home() / ".openchronicle" / "capture-buffer"
    json_path = buffer_dir / f"{capture_id}.json"
    if not json_path.exists():
        return fallback
    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return fallback
    ax_tree = data.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return fallback
    return prune_ax_tree(ax_tree)


def extract_sub_context(ax_tree: dict[str, Any]) -> str:
    """Extract a per-app sub-view identifier so coalesce/routing can tell
    different conversations / files / tabs apart inside the same app window.

    Without this, every WeChat capture has window_title='WeChat' regardless
    of which chat is active, and Round 1 coalesce folds different
    conversations into one representative.
    """
    apps = ax_tree.get("apps") or []
    chosen = None
    for app in apps:
        if app.get("is_frontmost"):
            chosen = app
            break
    if chosen is None and apps:
        chosen = apps[0]
    if chosen is None:
        return ""

    bundle = chosen.get("bundle_id", "") or ""
    if bundle == "com.tencent.xinWeChat":
        return _wechat_active_chat(chosen)
    return ""


def _wechat_active_chat(app_data: dict[str, Any]) -> str:
    """In WeChat the active conversation surfaces as the title of the
    AXTextArea chat-input field; the sidebar's search box is also an
    AXTextArea but its title is literally 'Search', so we skip that.
    """
    for win in app_data.get("windows", []):
        partner = _walk_for_wechat_partner(win.get("elements") or [])
        if partner:
            return f"chat:{partner}"
    return ""


def _walk_for_wechat_partner(elements: list[dict[str, Any]]) -> str:
    for el in elements:
        if el.get("role") == "AXTextArea":
            title = (el.get("title") or "").strip()
            if title and title != "Search":
                return title
        result = _walk_for_wechat_partner(el.get("children") or [])
        if result:
            return result
    return ""


def load_sub_context(
    capture_id: str,
    *,
    buffer_dir: Path | None = None,
) -> str:
    """Load capture JSON and return its sub-context (or '' if unavailable)."""
    if buffer_dir is None:
        buffer_dir = Path.home() / ".openchronicle" / "capture-buffer"
    json_path = buffer_dir / f"{capture_id}.json"
    if not json_path.exists():
        return ""
    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    ax_tree = data.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return ""
    return extract_sub_context(ax_tree)


def _walk(el: dict[str, Any], lines: list[str], depth: int) -> None:
    title = (el.get("title") or "").strip()
    value = (el.get("value") or "").strip()
    role = (el.get("role") or "").replace("AX", "")
    children = el.get("children") or []

    if role == "Button" and is_chrome_button(title):
        return

    if len(children) == 1:
        child = children[0]
        child_title = (child.get("title") or "").strip()
        child_value = (child.get("value") or "").strip()
        if child_title == title and not value and not child_value:
            _walk(child, lines, depth)
            return

    texts: list[str] = []
    if title:
        texts.append(title)
    if value and value != title:
        texts.append(value)

    if texts:
        text = " — ".join(texts)
        if role and role not in ("StaticText", "Group"):
            text = f"[{role}] {text}"
        lines.append("  " * depth + "- " + text)
        for c in children:
            _walk(c, lines, depth + 1)
    elif children:
        for c in children:
            _walk(c, lines, depth)
