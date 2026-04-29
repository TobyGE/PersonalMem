"""Minimal TOML config loader for PersonalMem.

Reads ``~/.personalmem/config.toml`` (or whichever path is passed in). Only
the fields the v14 pipeline actually uses are modeled — no legacy session /
classifier / mcp config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ModelConfig:
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    max_tokens: int | None = None
    num_ctx: int | None = None       # ollama-only: input context window


@dataclass
class CaptureConfig:
    """Live AX-capture daemon knobs."""
    event_driven: bool = True
    heartbeat_minutes: int = 10
    debounce_seconds: float = 3.0
    min_capture_gap_seconds: float = 2.0
    dedup_interval_seconds: float = 1.0
    same_window_dedup_seconds: float = 5.0
    interval_minutes: int = 10
    buffer_retention_hours: int = 168
    screenshot_retention_hours: int = 24
    buffer_max_mb: int = 2000
    # Screenshot capture mode:
    #   "auto"   (default) — only when AX text is sparse (videos / canvas
    #                        apps / Figma / PDF readers etc.) where the
    #                        AX tree alone tells us nothing useful.
    #   "always"           — every capture (overkill for most apps; AX is
    #                        usually enough on its own).
    #   "never"            — disable screenshots entirely.
    screenshot_mode: str = "auto"
    # In "auto" mode, screenshot fires when the pruned AX text is shorter
    # than this many characters. 200 is roughly the threshold between
    # "AX rendered enough text to route on" and "AX is essentially empty".
    screenshot_ax_sparse_threshold: int = 200
    screenshot_max_width: int = 1280
    screenshot_jpeg_quality: int = 80
    # Run Apple Vision OCR on each captured screenshot (only when a
    # screenshot was taken — i.e. when AX was sparse). The extracted
    # text lands in capture["vision_ocr"]["text"] for routing/summarizer
    # consumption. Cheap (~50-100ms per shot) and entirely local.
    ocr_enabled: bool = True
    ocr_min_confidence: float = 0.5
    ax_depth: int = 100
    ax_timeout_seconds: int = 3


@dataclass
class CoalesceConfig:
    gap_seconds: int = 60            # sliding-window dedup horizon


@dataclass
class RouterConfig:
    top_k: int = 10                  # top-K open threads shown to router


@dataclass
class CaptureSourceConfig:
    """Where to read raw AX captures from. Default = OpenChronicle's daemon."""
    index_db: str = "~/.openchronicle/index.db"
    capture_buffer_dir: str = "~/.openchronicle/capture-buffer"


@dataclass
class StorageConfig:
    """Where PersonalMem writes its own state."""
    threads_db: str = "~/.personalmem/threads.db"
    out_dir: str = "~/.personalmem/threads"


@dataclass
class Config:
    models: dict[str, ModelConfig] = field(default_factory=dict)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    coalesce: CoalesceConfig = field(default_factory=CoalesceConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    source: CaptureSourceConfig = field(default_factory=CaptureSourceConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def model_for(self, stage: str) -> ModelConfig:
        return self.models.get(stage) or self.models.get("default") or ModelConfig()


def resolve_api_key(cfg: ModelConfig) -> str | None:
    if cfg.api_key:
        return cfg.api_key
    if cfg.api_key_env:
        return os.environ.get(cfg.api_key_env)
    return None


def _as_dict(section: Any) -> dict:
    return section if isinstance(section, dict) else {}


def _build_models(raw: dict) -> dict[str, ModelConfig]:
    default_data = _as_dict(raw.get("default", {}))
    default_allowed = {k: v for k, v in default_data.items() if k in ModelConfig.__dataclass_fields__}
    default = ModelConfig(**default_allowed)
    models = {"default": default}
    for name, section in raw.items():
        if name == "default":
            continue
        data = _as_dict(section)
        allowed = {k: v for k, v in data.items() if k in ModelConfig.__dataclass_fields__}
        models[name] = ModelConfig(**{**default.__dict__, **allowed})
    return models


def _build(cls, raw: dict):
    allowed = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    return cls(**allowed)


def default_config_path() -> Path:
    return Path.home() / ".personalmem" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    return Config(
        models=_build_models(_as_dict(raw.get("models"))),
        capture=_build(CaptureConfig, _as_dict(raw.get("capture"))),
        coalesce=_build(CoalesceConfig, _as_dict(raw.get("coalesce"))),
        router=_build(RouterConfig, _as_dict(raw.get("router"))),
        source=_build(CaptureSourceConfig, _as_dict(raw.get("source"))),
        storage=_build(StorageConfig, _as_dict(raw.get("storage"))),
    )


def expand_path(p: str) -> Path:
    return Path(p).expanduser()


DEFAULT_CONFIG_TEMPLATE = """\
# PersonalMem configuration

# ─── Models ─────────────────────────────────────────────────────────────────
# Any provider litellm supports works. Examples:
#
# Local Ollama (free, runs on your machine):
[models.default]
model = "ollama/qwen2.5:14b"
num_ctx = 32768
max_tokens = 2048
#
# Anthropic API (paid):
# [models.default]
# model = "anthropic/claude-haiku-4-5-20251001"
# api_key_env = "ANTHROPIC_API_KEY"
# max_tokens = 2048
#
# OpenAI:
# [models.default]
# model = "gpt-4o-mini"
# api_key_env = "OPENAI_API_KEY"
# max_tokens = 2048

# Stage overrides — uncomment to use different models per stage.
# [models.thread_router]
# model = "ollama/qwen2.5:7b"
# num_ctx = 32768
#
# [models.thread_summarizer]
# model = "ollama/qwen2.5:14b"
# num_ctx = 32768

# ─── Capture daemon ────────────────────────────────────────────────────────
[capture]
event_driven = true
heartbeat_minutes = 10
debounce_seconds = 3.0
min_capture_gap_seconds = 2.0
dedup_interval_seconds = 1.0
same_window_dedup_seconds = 5.0
buffer_retention_hours = 168
screenshot_retention_hours = 24
buffer_max_mb = 2000
# Screenshot mode:
#   "auto"   — fire only when AX text is sparse (videos/canvas/Figma/PDF
#              etc.) where AX alone gives no useful signal.
#   "always" — every capture.
#   "never"  — never.
screenshot_mode = "auto"
screenshot_ax_sparse_threshold = 200
screenshot_max_width = 1280
screenshot_jpeg_quality = 80
# OCR each captured screenshot via Apple Vision (mac-vision-ocr binary).
ocr_enabled = true
ocr_min_confidence = 0.5
ax_depth = 100
ax_timeout_seconds = 3

# ─── Pipeline knobs ────────────────────────────────────────────────────────
[coalesce]
gap_seconds = 60

[router]
top_k = 10

# ─── Data source: where raw AX captures live ──────────────────────────────
# By default reads from PersonalMem's own daemon. Point at another
# OpenChronicle install if you want to re-process its captures.
[source]
index_db = "~/.personalmem/index.db"
capture_buffer_dir = "~/.personalmem/capture-buffer"

# ─── Storage: where PersonalMem writes thread state and outputs ────────────
[storage]
threads_db = "~/.personalmem/threads.db"
out_dir = "~/.personalmem/threads"
"""
