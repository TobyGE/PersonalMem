"""LLM provider that talks to ChatGPT via Codex CLI's stored OAuth tokens.

A ChatGPT Plus / Pro / Team subscriber can use PersonalMem without ever
creating an OpenAI API key — the OAuth login is handled by the official
``codex`` CLI (``codex login``), and we just read the access_token it
saves to ``~/.codex/auth.json`` and POST to the ChatGPT-account
Responses endpoint.

Adapted from OpenSeer's openai_chatgpt.py.

Constraints discovered by probing the chatgpt.com Responses backend:
  - the public ``api.openai.com`` rejects this token (missing
    ``model.request`` scope) — must use ``chatgpt.com/backend-api``
  - ``store=false``, ``stream=true`` required
  - allowed models: gpt-5.5, gpt-5.3-codex (others rejected)

The wrapper returns a litellm-shaped response so the rest of the
pipeline (``extract_text`` / ``extract_json_text``) keeps working.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import auth as auth_mod


_URL = "https://chatgpt.com/backend-api/codex/responses"
_DEFAULT_MODEL = "gpt-5.5"


# ─── litellm-shaped response wrappers (same shape as anthropic_oauth) ───────

@dataclass
class _Msg:
    content: str
    tool_calls: Any = None


@dataclass
class _Choice:
    message: _Msg
    finish_reason: str = "stop"


@dataclass
class _Resp:
    choices: list[_Choice]


def _extract_system_and_user(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """The Responses API takes ``instructions`` (system) and ``input``
    (user) separately. Collapse our chat-style messages into those two
    strings — concatenate by role.
    """
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            user_parts.append(content)
        elif role == "assistant":
            # Convert prior turns into a tagged block in the user input
            # so the model sees the dialogue context.
            user_parts.append(f"[assistant prior turn]\n{content}")
    return "\n\n".join(p for p in system_parts if p), "\n\n".join(p for p in user_parts if p)


def call_codex_oauth(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
) -> _Resp:
    """Call the chatgpt.com codex Responses endpoint with OAuth bearer.

    Streams SSE; reassembles ``response.output_text.delta`` events into
    the final text. Retries 429/5xx with exponential backoff.
    """
    auth = auth_mod.load_codex_tokens()
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access or not account_id:
        raise RuntimeError(
            f"{auth_mod.CODEX_AUTH_FILE} is missing access_token / account_id. "
            "Run `codex login` to refresh."
        )

    instructions, user_input = _extract_system_and_user(messages)
    payload = {
        "model": model or _DEFAULT_MODEL,
        "instructions": instructions or "You are a helpful assistant.",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": user_input},
        ]}],
        "stream": True,
        "store": False,
        # NOTE: chatgpt.com Responses backend rejects max_output_tokens
        # ("Unsupported parameter") — the parameter is silently ignored.
        # We accept ``max_tokens`` in the signature for API symmetry with
        # other providers but it currently has no effect here.
    }
    body = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "chatgpt-account-id": account_id,
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=v1",
    }

    def _do_one() -> str:
        req = urllib.request.Request(_URL, data=body, method="POST", headers=headers)
        text = ""
        with urllib.request.urlopen(req, timeout=180) as r:
            for line in r:
                s = line.decode().rstrip()
                if not s.startswith("data: "):
                    continue
                d = json.loads(s[6:])
                t = d.get("type")
                if t == "response.output_text.delta":
                    text += d.get("delta", "")
                elif t == "response.completed":
                    break
                elif t == "response.failed":
                    raise RuntimeError(f"response.failed: {d}")
        return text

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            text = _do_one()
            return _Resp(choices=[_Choice(message=_Msg(content=text))])
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                pass
            retryable = (e.code == 429) or (500 <= e.code < 600)
            if retryable and attempt < 2:
                time.sleep(2 ** attempt)  # 1, 2 s
                last_exc = e
                continue
            raise RuntimeError(f"Codex OAuth HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                last_exc = e
                continue
            raise

    raise RuntimeError(f"Codex OAuth: all retries exhausted") from last_exc
