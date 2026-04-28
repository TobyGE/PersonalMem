"""LLM dispatcher: thin wrapper around litellm.

``call_llm(cfg, stage, messages=...)`` returns a litellm response object
(``.choices[0].message.content``). Use any provider litellm supports —
ollama, openai, anthropic (api key), gemini, etc. — by setting
``model``, ``api_key`` / ``api_key_env``, and ``base_url`` in config.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..config import Config, resolve_api_key

logger = logging.getLogger("personalmem.llm")


def call_llm(
    cfg: Config,
    stage: str,
    *,
    messages: list[dict[str, Any]],
    json_mode: bool = False,
) -> Any:
    """Invoke the configured model for a stage. Returns litellm-shaped response.

    Mock mode (for tests): ``PERSONALMEM_LLM_MOCK=1`` returns a stub.
    """
    if os.environ.get("PERSONALMEM_LLM_MOCK") == "1":
        return _mock_response()

    model_cfg = cfg.model_for(stage)

    import litellm  # lazy
    kwargs: dict[str, Any] = {
        "model": model_cfg.model,
        "messages": messages,
    }
    if model_cfg.base_url:
        kwargs["api_base"] = model_cfg.base_url
    api_key = resolve_api_key(model_cfg)
    if api_key:
        kwargs["api_key"] = api_key
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if model_cfg.max_tokens:
        kwargs["max_tokens"] = model_cfg.max_tokens
    if model_cfg.num_ctx:
        kwargs["num_ctx"] = model_cfg.num_ctx

    logger.debug("llm call stage=%s model=%s", stage, model_cfg.model)
    return litellm.completion(**kwargs)


def extract_text(response: Any) -> str:
    try:
        return response.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


def _mock_response():
    override = os.environ.get("PERSONALMEM_LLM_MOCK_JSON")
    content = override if override else '{"action": "new", "new_title": "mock"}'

    class _Msg:
        def __init__(self, c):
            self.content = c
            self.tool_calls = None

    class _Choice:
        def __init__(self, m):
            self.message = m
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, choices):
            self.choices = choices

    return _Resp([_Choice(_Msg(content))])
