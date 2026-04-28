"""Anthropic API client using Claude.com OAuth tokens (subscription auth).

Tokens come from ``personalmem onboard`` and live in
``~/.personalmem/oauth-tokens.json`` (same JSON shape used by GuardClaw, so
``~/.guardclaw/oauth-tokens.json`` is also accepted as a fallback for users
who already authenticated through GuardClaw).

Auto-refreshes on 401 and makes direct HTTP calls to
``https://api.anthropic.com/v1/messages`` with the OAuth bearer header —
litellm doesn't natively support Anthropic's bearer flow.

Returns a litellm-shaped response so the rest of the pipeline
(``extract_text``) keeps working unchanged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urllib.error
import urllib.parse
import urllib.request


_PRIMARY_TOKEN_FILE = Path.home() / ".personalmem" / "oauth-tokens.json"
_FALLBACK_TOKEN_FILE = Path.home() / ".guardclaw" / "oauth-tokens.json"
_BASE_URL = "https://api.anthropic.com/v1"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _token_file() -> Path:
    if _PRIMARY_TOKEN_FILE.exists():
        return _PRIMARY_TOKEN_FILE
    if _FALLBACK_TOKEN_FILE.exists():
        return _FALLBACK_TOKEN_FILE
    return _PRIMARY_TOKEN_FILE  # for the "not found" error message below


def _load_tokens() -> dict[str, Any]:
    path = _token_file()
    if not path.exists():
        raise RuntimeError(
            f"OAuth token file not found at {_PRIMARY_TOKEN_FILE}. "
            "Run `personalmem onboard` and pick the Anthropic OAuth option."
        )
    return json.loads(path.read_text())


def _save_tokens(tokens: dict[str, Any]) -> None:
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _is_expired(tok: dict[str, Any]) -> bool:
    saved_at_ms = tok.get("savedAt") or 0
    expires_in_s = tok.get("expires_in") or 0
    if not saved_at_ms or not expires_in_s:
        return False
    expires_at_ms = saved_at_ms + expires_in_s * 1000
    # Refresh 60s early to avoid in-flight expiration races
    return (time.time() * 1000) > (expires_at_ms - 60_000)


def _refresh(refresh_token: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get_access_token(provider: str = "claude") -> str:
    tokens = _load_tokens()
    tok = tokens.get(provider) or {}
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    if not access:
        raise RuntimeError(f"No access_token in {_TOKEN_FILE} for provider={provider}")

    if _is_expired(tok) and refresh:
        refreshed = _refresh(refresh)
        merged = {**tok, **refreshed, "savedAt": int(time.time() * 1000)}
        tokens[provider] = merged
        _save_tokens(tokens)
        access = merged["access_token"]
    return access


def _force_refresh(provider: str = "claude") -> str:
    tokens = _load_tokens()
    tok = tokens.get(provider) or {}
    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError("Cannot refresh: no refresh_token stored")
    refreshed = _refresh(refresh)
    merged = {**tok, **refreshed, "savedAt": int(time.time() * 1000)}
    tokens[provider] = merged
    _save_tokens(tokens)
    return merged["access_token"]


# ─── litellm-shaped response wrappers ────────────────────────────────────────

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


def _extract_system_and_user(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic API takes ``system`` separately from ``messages``."""
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content") or "")
        else:
            chat.append(m)
    return "\n\n".join(p for p in system_parts if p), chat


def call_anthropic_oauth(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
) -> _Resp:
    """Call Anthropic's /v1/messages with OAuth bearer auth.

    Auto-refreshes the token once on 401, then retries.
    """
    system, chat = _extract_system_and_user(messages)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": chat,
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload).encode()

    def _make_request(token: str):
        req = urllib.request.Request(
            f"{_BASE_URL}/messages", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "Authorization": f"Bearer {token}",
            },
        )
        return urllib.request.urlopen(req, timeout=120)

    access = _get_access_token()
    try:
        resp = _make_request(access)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            access = _force_refresh()
            resp = _make_request(access)
        else:
            raise

    data = json.loads(resp.read().decode())
    text = ""
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text += block.get("text") or ""
    return _Resp(choices=[_Choice(message=_Msg(content=_strip_code_fences(text)))])


def _strip_code_fences(text: str) -> str:
    """Anthropic models often wrap JSON output in ```json ... ``` fences
    despite explicit instructions. Strip a leading/trailing fence so the
    downstream ``json.loads`` succeeds.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the opening ``` (with optional language tag like ```json) and the
    # closing ```. Be tolerant of whitespace.
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].rstrip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
