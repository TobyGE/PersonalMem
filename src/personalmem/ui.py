"""Tiny terminal-UX helpers for guided flows (setup wizard, doctor, etc.).

Adapted from OpenSeer's setup_wizard. Just ANSI codes + a few prefix
printers — no external deps. Falls back gracefully when stdout isn't a
TTY (codes print as literals but everything still works).
"""
from __future__ import annotations

import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

_RESET = "\x1b[0m" if _USE_COLOR else ""
_BOLD = "\x1b[1m" if _USE_COLOR else ""
_DIM = "\x1b[2m" if _USE_COLOR else ""
_RED = "\x1b[31m" if _USE_COLOR else ""
_GRN = "\x1b[32m" if _USE_COLOR else ""
_YEL = "\x1b[33m" if _USE_COLOR else ""
_CYN = "\x1b[36m" if _USE_COLOR else ""
_MAG = "\x1b[35m" if _USE_COLOR else ""

# Public re-exports (mostly for callers that want to weave colors into
# their own f-strings rather than going through the helpers below).
RESET, BOLD, DIM = _RESET, _BOLD, _DIM
RED, GRN, YEL, CYN, MAG = _RED, _GRN, _YEL, _CYN, _MAG


def c(s: str, *codes: str) -> str:
    """Wrap ``s`` in the given ANSI codes."""
    if not codes:
        return s
    return "".join(codes) + s + _RESET


def step(idx: int, total: int, title: str) -> None:
    print(f"\n{c(f'[{idx}/{total}]', _BOLD, _CYN)} {c(title, _BOLD)}")


def ok(msg: str) -> None:
    print(f"  {c('✓', _GRN)} {msg}")


def warn(msg: str) -> None:
    print(f"  {c('⚠', _YEL)} {msg}")


def fail(msg: str) -> None:
    print(f"  {c('✗', _RED)} {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def ask(prompt: str) -> bool:
    """Return True if the user pressed Enter or 'y' / 'yes'."""
    try:
        ans = input(f"  {c('?', _MAG)} {prompt} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("", "y", "yes")
