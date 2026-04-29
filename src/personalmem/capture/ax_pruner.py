"""AX tree pruning: drop UI chrome and structural redundancy from AX dumps
before they reach the LLM.

Cleanups:

1. **Chrome buttons**: AXButton nodes whose title is a known piece of UI
   plumbing (Send / Search / Back / Voice input / etc) — these dominate AX
   dumps for chat and browser apps but carry zero task signal.

2. **Parent-child same-title duplicates**: when a node has exactly one child
   whose title matches the parent's title (and the parent has no own value),
   collapse the wrapper. Pattern: `[Button] Mobile / [Button] Mobile`.

3. **Empty containers**: AXGroup / AXSplitter / AXSplitGroup nodes with no
   own text get their children promoted to the same depth (keeps text but
   drops one level of indentation noise).

4. **Long values**: TextField / ComboBox values get truncated past a few
   hundred chars (the Chrome address bar can dump 2 KB OAuth URLs).

5. **Chrome browser-specific**: strip `- Inactive tab - 547 MB freed up`
   memory suffixes from tab titles, drop the bookmarks toolbar entirely,
   strip `- Has/Wants access to this site` from extension popups, and
   dedup repeated tab listings within a single window.

The output mirrors `ax_app_to_markdown` so it can drop in as a `visible_text`
replacement — same `## App / ### window / - bullets` shape.

This module is import-safe (no live-daemon side effects). The replay scripts
opt into it; the live capture path keeps using `ax_app_to_markdown` for now.
"""

from __future__ import annotations

import json
import re
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


# ─── Chrome browser-specific cleanups ────────────────────────────────────────

# Tab title suffixes Chrome adds for memory/lifecycle state. They drown the
# real tab title in noise and have zero session signal.
_CHROME_TAB_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:Inactive tab|Memory usage)\b.*$"
)

# Extension popups (PopUpButton) carry " - Has access to this site" or
# " - Wants access to this site" appended to their value. Strip — including
# any leading separator (newline / dash / whitespace).
_CHROME_EXT_ACCESS_RE = re.compile(
    r"[\s\-]*(?:Has access to this site|Wants access to this site)\s*$",
)


def _clean_chrome_tab_title(title: str) -> str:
    return _CHROME_TAB_SUFFIX_RE.sub("", title).strip()


def _clean_chrome_extension_value(value: str) -> str:
    return _CHROME_EXT_ACCESS_RE.sub("", value).strip()


# Drop entire subtrees rooted at these chrome containers (Toolbar role +
# title). The bookmarks toolbar in particular is pure plumbing.
_CHROME_DROP_TOOLBARS = frozenset({"Bookmarks"})


# Truncate any element value or title longer than this. The Chrome address
# bar can dump 2 KB+ OAuth redirect URLs (and tabs without page titles
# fall back to URLs as their RadioButton title); nothing else legitimately
# needs this much in a single AX node.
_MAX_VALUE_LEN = 200


def _truncate_value(value: str) -> str:
    if len(value) <= _MAX_VALUE_LEN:
        return value
    head = value[:_MAX_VALUE_LEN].rstrip()
    return f"{head}… (+{len(value) - _MAX_VALUE_LEN} chars truncated)"


# Roles whose (role, title) we dedup window-wide. Chrome's AX tree renders
# the same toolbar elements (extension popups, address bar, New Tab button,
# tab strip) in multiple subtrees per window — without this dedup the
# pruned output is 3-4× longer than necessary.
_WINDOW_DEDUP_ROLES = frozenset({
    "Button", "PopUpButton", "RadioButton", "TextField",
})


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
        # window_seen tracks (role, normalized_title) pairs already rendered
        # in this window. Used to suppress Chrome's duplicated tab-list
        # rendering (the same RadioButton tabs appear twice in the AX tree).
        window_seen: set[tuple[str, str]] = set()
        for el in win.get("elements", []):
            _walk(el, lines, depth=0, ancestor_sigs=frozenset(),
                  window_seen=window_seen)
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
        buffer_dir = Path.home() / ".personalmem" / "capture-buffer"
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
        buffer_dir = Path.home() / ".personalmem" / "capture-buffer"
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


def _walk(
    el: dict[str, Any],
    lines: list[str],
    depth: int,
    ancestor_sigs: frozenset[tuple[str, str, str]] = frozenset(),
    window_seen: set[tuple[str, str]] | None = None,
) -> None:
    title = (el.get("title") or "").strip()
    value = (el.get("value") or "").strip()
    role = (el.get("role") or "").replace("AX", "")
    children = el.get("children") or []

    if role == "Button" and is_chrome_button(title):
        return

    # Drop the Bookmarks toolbar entirely — pure browser plumbing, large
    # subtree, no session signal.
    if role == "Toolbar" and title in _CHROME_DROP_TOOLBARS:
        return

    # Chrome-specific cleanups on the visible text before signature/dedup.
    if role == "RadioButton":
        title = _clean_chrome_tab_title(title)
    if role == "PopUpButton":
        # Chrome packs " - Has/Wants access to this site" into either the
        # title (with a literal \n separator) or the value, depending on
        # the extension. Clean both.
        if title:
            title = _clean_chrome_extension_value(title)
        if value:
            value = _clean_chrome_extension_value(value)

    # Truncate runaway value/title strings (Chrome address bar TextField at
    # 2 KB+, tabs without page titles falling back to full URLs).
    if value:
        value = _truncate_value(value)
    if title:
        title = _truncate_value(title)

    # Per-window dedup for interactive leaf roles. Chrome's AX tree renders
    # the same toolbar elements + tab list in multiple subtrees per window;
    # without this the toolbar appears 3-4 times.
    if window_seen is not None and role in _WINDOW_DEDUP_ROLES and title:
        key = (role, title)
        if key in window_seen:
            return
        window_seen.add(key)

    # Ancestor-duplicate guard: drop any node whose (role, title, value)
    # matches any ancestor in the current chain. Outlook's AX tree wraps
    # the same TextField recursively 100+ levels deep with identical
    # value (and the same set of sibling buttons at every level), so
    # rendering those subtrees produces the same buttons repeated 100+
    # times. Stop recursion entirely on ancestor-dup hits — the subtree
    # is guaranteed redundant with what we've already rendered or are
    # about to render at the parent's level.
    sig = (role, title, value)
    if sig in ancestor_sigs:
        return

    if len(children) == 1:
        child = children[0]
        child_title = (child.get("title") or "").strip()
        child_value = (child.get("value") or "").strip()
        if child_title == title and not value and not child_value:
            _walk(child, lines, depth, ancestor_sigs, window_seen)
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
        new_ancestors = ancestor_sigs | {sig}
        _walk_children(children, lines, depth + 1, new_ancestors, window_seen)
    elif children:
        _walk_children(children, lines, depth, ancestor_sigs, window_seen)


# Cap how many children of a single parent we render. Lists / tables
# (Outlook inbox, browser tab strip, file explorer) commonly have 50-200
# rows of similar shape — even after dedup, that's huge. We render the
# first N and append a one-line "(K more hidden)" so the LLM knows the
# total count without paying for every entry.
_MAX_CHILDREN_PER_PARENT = 30


def _walk_children(
    children: list[dict[str, Any]],
    lines: list[str],
    depth: int,
    ancestor_sigs: frozenset[tuple[str, str, str]],
    window_seen: set[tuple[str, str]] | None = None,
) -> None:
    rendered = 0
    skipped = 0
    for c in children:
        if rendered >= _MAX_CHILDREN_PER_PARENT:
            skipped += 1
            continue
        before = len(lines)
        _walk(c, lines, depth, ancestor_sigs, window_seen)
        if len(lines) > before:
            rendered += 1
    if skipped:
        lines.append("  " * depth + f"- … ({skipped} more children hidden)")
