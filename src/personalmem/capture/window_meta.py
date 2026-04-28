"""Foreground app / window metadata via osascript. macOS only.

Extended from OpenChronicle's basic version to also fetch the active
window's geometry (position + size in logical points), which the scheduler
then forwards to the screenshot grabber so screenshots can be cropped to
just the active window.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

from ..logger import get

logger = get("personalmem.capture")

_SCRIPT = """
tell application "System Events"
    set frontProc to first application process whose frontmost is true
    set appName to name of frontProc
    try
        set bundleId to bundle identifier of frontProc
    on error
        set bundleId to ""
    end try
    try
        set winTitle to name of front window of frontProc
    on error
        set winTitle to ""
    end try
    try
        set winPos to position of front window of frontProc
        set winSize to size of front window of frontProc
        set posX to (item 1 of winPos as string)
        set posY to (item 2 of winPos as string)
        set sizeW to (item 1 of winSize as string)
        set sizeH to (item 2 of winSize as string)
    on error
        set posX to ""
        set posY to ""
        set sizeW to ""
        set sizeH to ""
    end try
    return appName & "\\n" & winTitle & "\\n" & bundleId & "\\n" & posX & "\\n" & posY & "\\n" & sizeW & "\\n" & sizeH
end tell
"""


@dataclass
class WindowMeta:
    app_name: str = ""
    title: str = ""
    bundle_id: str = ""
    # Active window geometry in logical points (None if unavailable).
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None

    @property
    def has_bounds(self) -> bool:
        return None not in (self.x, self.y, self.width, self.height)


def _to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _bounds_via_quartz(app_name: str) -> tuple[int, int, int, int] | None:
    """Look up the frontmost on-screen window for ``app_name`` via Quartz's
    ``CGWindowListCopyWindowInfo``. This works for apps where AppleScript's
    ``front window`` query fails (Electron / iTerm2 / many web-based apps),
    because Quartz queries the WindowServer directly without needing AX
    enumeration permission.
    """
    if not app_name:
        return None
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListExcludeDesktopElements,
        )
    except ImportError:
        return None

    options = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    try:
        window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None

    # The list is ordered front-to-back; pick the topmost window owned by
    # the named app that has a non-trivial size (filters out tiny tooltips).
    for w in window_list:
        if w.get("kCGWindowOwnerName") != app_name:
            continue
        bounds = w.get("kCGWindowBounds") or {}
        x = bounds.get("X")
        y = bounds.get("Y")
        width = bounds.get("Width")
        height = bounds.get("Height")
        if None in (x, y, width, height):
            continue
        if width < 100 or height < 100:
            continue  # likely a tooltip / floating popover, not the main window
        return int(x), int(y), int(width), int(height)
    return None


def active_window() -> WindowMeta:
    if platform.system() != "Darwin":
        return WindowMeta()
    try:
        proc = subprocess.run(
            ["osascript", "-e", _SCRIPT], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("osascript failed: %s", exc)
        return WindowMeta()

    if proc.returncode != 0:
        logger.debug("osascript rc=%d stderr=%s", proc.returncode, proc.stderr.strip()[:200])
        return WindowMeta()

    parts = proc.stdout.strip().split("\n")
    while len(parts) < 7:
        parts.append("")
    meta = WindowMeta(
        app_name=parts[0],
        title=parts[1],
        bundle_id=parts[2],
        x=_to_int(parts[3]),
        y=_to_int(parts[4]),
        width=_to_int(parts[5]),
        height=_to_int(parts[6]),
    )
    # Fallback: many apps (Electron, iTerm2, browsers using non-standard
    # window roles) raise "Invalid index" on `front window` queries via
    # System Events. Use Quartz's CGWindowList directly, which doesn't
    # need AX enumeration permission and reports the actual on-screen
    # rect for any owned window.
    if not meta.has_bounds:
        bounds = _bounds_via_quartz(meta.app_name)
        if bounds is not None:
            meta.x, meta.y, meta.width, meta.height = bounds
    return meta
