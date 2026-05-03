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

    # Auto-detect Anthropic OAuth: if the model is Claude-family AND no
    # api_key is configured, try the local subscription-auth helper.
    if (
        "claude-" in model_cfg.model.lower()
        and not resolve_api_key(model_cfg)
    ):
        try:
            from . import anthropic_oauth
        except ImportError:
            anthropic_oauth = None
        if anthropic_oauth is not None:
            model_name = model_cfg.model.split("/", 1)[-1]  # strip optional "anthropic/" prefix
            return anthropic_oauth.call_anthropic_oauth(
                model=model_name,
                messages=messages,
                max_tokens=model_cfg.max_tokens or 4096,
            )

    # Auto-detect Codex OAuth: if the model is a ChatGPT-Codex-only one
    # (gpt-5.5 / gpt-5.3-codex) AND no api_key is configured, route via
    # the chatgpt.com Responses endpoint using the user's Codex CLI
    # tokens (read from ~/.codex/auth.json by codex_oauth).
    is_codex_model = (
        "gpt-5.5" in model_cfg.model.lower()
        or "gpt-5.3-codex" in model_cfg.model.lower()
    )
    if is_codex_model and not resolve_api_key(model_cfg):
        from . import codex_oauth
        model_name = model_cfg.model.split("/", 1)[-1]
        return codex_oauth.call_codex_oauth(
            model=model_name,
            messages=messages,
            max_tokens=model_cfg.max_tokens or 4096,
        )

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
        # OpenAI accepts {"type":"json_object"}; Anthropic/litellm tolerates
        # it; LM Studio rejects it (wants json_schema or text). Skip the
        # kwarg when talking to local OpenAI-compatible endpoints — our
        # extract_json_text() already handles arbitrary wrappers.
        is_local = bool(model_cfg.base_url) and (
            "localhost" in model_cfg.base_url or "127.0.0.1" in model_cfg.base_url
        )
        if not is_local:
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


def extract_reasoning(response: Any) -> str:
    """LM Studio (and some other endpoints) put the model's chain-of-thought
    in a separate ``message.reasoning_content`` field instead of inlining
    <think>...</think> blocks in content. Surface it for debug logs.
    """
    try:
        msg = response.choices[0].message
    except (AttributeError, IndexError):
        return ""
    # Try common field names across providers
    for field in ("reasoning_content", "reasoning"):
        val = getattr(msg, field, None)
        if val:
            return val
    # litellm sometimes exposes provider-specific fields via dict access
    try:
        return msg.get("reasoning_content") or msg.get("reasoning") or ""
    except (AttributeError, TypeError):
        return ""


def extract_full_text(response: Any) -> str:
    """Combined view of reasoning + answer for debug logging.

    Format: ``<think>{reasoning}</think>\\n{content}`` when reasoning is
    present in a side channel; otherwise just content (which may already
    contain <think> tags inline for models that emit them in band).
    """
    content = extract_text(response)
    reasoning = extract_reasoning(response)
    if reasoning and "<think>" not in content:
        return f"<think>\n{reasoning}\n</think>\n\n{content}"
    return content


# Reasoning models (Qwen3, DeepSeek-R1, GLM, ...) emit a <think>...</think>
# block before the actual answer. Anthropic/Gemini sometimes wrap JSON in
# ```json fences. Models also occasionally prepend prose like "Here's the
# JSON:". extract_json_text() peels those layers so json.loads() succeeds.
import re as _re

_THINK_TAG_RE = _re.compile(r"<think\b[^>]*>.*?</think>", _re.DOTALL | _re.IGNORECASE)
_OPEN_THINK_RE = _re.compile(r"^.*?</think>", _re.DOTALL | _re.IGNORECASE)


def extract_json_text(response: Any) -> str:
    """Strip reasoning/code-fence wrappers and isolate a JSON object.

    Layered cleanup:
      1. Drop any <think>...</think> block (chain-of-thought from Qwen3,
         DeepSeek-R1, GLM-4 etc.). Also handles unbalanced cases where
         the closing tag exists but the opener was lost in truncation.
      2. Drop a leading ```json / ``` fence and its trailing ```.
      3. If the result still has a JSON object inside, extract the
         outermost {...} so leading/trailing prose is ignored.
    """
    text = extract_text(response).strip()
    if not text:
        return ""

    # 1. Reasoning blocks
    text = _THINK_TAG_RE.sub("", text)
    if "</think>" in text and "<think>" not in text:
        # Truncated: only the close tag survived
        text = _OPEN_THINK_RE.sub("", text, count=1)
    text = text.strip()

    # 2. Code fences
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].rstrip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 3. Outermost JSON object — scan for first { then walk to matching }
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    # Unbalanced — return what we have starting at first { for json.loads
    # to surface a clearer error
    return text[start:]


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
