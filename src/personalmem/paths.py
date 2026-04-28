"""Single source of truth for on-disk locations under ~/.personalmem/."""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    override = os.environ.get("PERSONALMEM_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".personalmem"


def memory_dir() -> Path:
    return root() / "memory"


def capture_buffer_dir() -> Path:
    return root() / "capture-buffer"


def logs_dir() -> Path:
    return root() / "logs"


def config_file() -> Path:
    return root() / "config.toml"


def index_db() -> Path:
    return root() / "index.db"


def pid_file() -> Path:
    return root() / ".pid"


def paused_flag() -> Path:
    return root() / ".paused"


def writer_state() -> Path:
    """Tracks last-commit timestamp and processed capture files."""
    return root() / ".writer-state.json"


def ensure_dirs() -> None:
    for d in (root(), memory_dir(), capture_buffer_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)
