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
    auth_type: str = ""              # "" → litellm; "anthropic_oauth" → direct OAuth


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
# Default model used by stages that don't specify their own.
[models.default]
model = "claude-haiku-4-5-20251001"
auth_type = "anthropic_oauth"   # uses GuardClaw's stored Claude OAuth token
max_tokens = 2048

# Stage overrides — uncomment to use different models per stage.
# [models.thread_router]
# model = "claude-haiku-4-5-20251001"
# auth_type = "anthropic_oauth"
# max_tokens = 2048

# [models.thread_summarizer]
# model = "claude-haiku-4-5-20251001"
# auth_type = "anthropic_oauth"
# max_tokens = 2048

# Or use Ollama locally:
# [models.thread_router]
# model = "ollama/qwen2.5:14b"
# num_ctx = 32768

# ─── Pipeline knobs ────────────────────────────────────────────────────────
[coalesce]
gap_seconds = 60

[router]
top_k = 10

# ─── Data source: where raw AX captures live ──────────────────────────────
# Default points at OpenChronicle's daemon output. PersonalMem only reads.
[source]
index_db = "~/.openchronicle/index.db"
capture_buffer_dir = "~/.openchronicle/capture-buffer"

# ─── Storage: where PersonalMem writes thread state and outputs ────────────
[storage]
threads_db = "~/.personalmem/threads.db"
out_dir = "~/.personalmem/threads"
"""
