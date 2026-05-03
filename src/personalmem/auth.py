"""Authentication helpers — read & inspect the LLM auth state PersonalMem
relies on.

We support two OAuth-style providers that don't need an API key:

  - **Codex CLI** (ChatGPT Plus/Pro/Team subscription): tokens live in
    ``~/.codex/auth.json``, written by ``codex login``. PersonalMem just
    reads them and uses ``access_token`` as a Bearer for the
    chatgpt.com Responses API.

  - **Anthropic Claude.com OAuth**: tokens live in
    ``~/.personalmem/oauth-tokens.json`` (with a fallback to
    ``~/.guardclaw/oauth-tokens.json`` for users who already
    authenticated through GuardClaw). Written by
    ``personalmem onboard`` → option [2].

This module reports state for both. Login/logout for the Codex side
delegate to the ``codex`` binary; the Anthropic side is handled by the
PKCE flow in ``onboard.py``.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
ANTHROPIC_TOKEN_FILE = Path.home() / ".personalmem" / "oauth-tokens.json"
ANTHROPIC_TOKEN_FALLBACK = Path.home() / ".guardclaw" / "oauth-tokens.json"


@dataclass
class TokenStatus:
    provider: str                      # 'codex' | 'anthropic-oauth'
    has_file: bool
    auth_mode: Optional[str] = None
    plan_type: Optional[str] = None    # codex: free/plus/pro/team
    account_id: Optional[str] = None
    expires_at: Optional[int] = None   # epoch seconds
    expired: bool = False
    error: Optional[str] = None
    file_path: Optional[Path] = None

    def summary(self) -> str:
        if self.error:
            return f"{self.provider}: ERROR — {self.error}"
        if not self.has_file:
            return f"{self.provider}: not logged in"
        bits = []
        if self.auth_mode:
            bits.append(f"mode={self.auth_mode}")
        if self.plan_type:
            bits.append(f"plan={self.plan_type}")
        if self.account_id:
            bits.append(f"account={self.account_id[:8]}…")
        if self.expires_at:
            remain = self.expires_at - int(time.time())
            if remain > 0:
                hrs = remain / 3600
                bits.append(f"expires_in={hrs:.1f}h")
            else:
                bits.append("EXPIRED")
        flag = "EXPIRED" if self.expired else "ok"
        return f"{self.provider}: {flag} ({' '.join(bits) or 'no detail'})"


# ─── Codex CLI ──────────────────────────────────────────────────────────────


def _decode_jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def codex_cli_path() -> Optional[str]:
    return shutil.which("codex")


def load_codex_tokens() -> dict:
    """Read ~/.codex/auth.json. Raises FileNotFoundError if missing."""
    if not CODEX_AUTH_FILE.exists():
        raise FileNotFoundError(
            f"{CODEX_AUTH_FILE} not found. Install Codex CLI "
            "(`npm install -g @openai/codex`) and run `codex login`."
        )
    return json.loads(CODEX_AUTH_FILE.read_text())


def codex_token_status() -> TokenStatus:
    if not CODEX_AUTH_FILE.exists():
        return TokenStatus(provider="codex", has_file=False, file_path=CODEX_AUTH_FILE)
    try:
        auth = json.loads(CODEX_AUTH_FILE.read_text())
    except Exception as e:
        return TokenStatus(provider="codex", has_file=True, file_path=CODEX_AUTH_FILE,
                           error=f"unreadable: {e}")
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") or ""
    payload = _decode_jwt_payload(access) if access.count(".") == 2 else {}
    auth_block = payload.get("https://api.openai.com/auth") or {}
    exp = payload.get("exp")
    expired = bool(exp and exp < time.time())
    return TokenStatus(
        provider="codex",
        has_file=True,
        file_path=CODEX_AUTH_FILE,
        auth_mode=auth.get("auth_mode"),
        plan_type=auth_block.get("chatgpt_plan_type"),
        account_id=tokens.get("account_id"),
        expires_at=exp,
        expired=expired,
    )


def run_codex_login() -> int:
    """Spawn `codex login` in the foreground. Returns exit code."""
    bin_ = codex_cli_path()
    if not bin_:
        print(
            "Codex CLI not found on PATH.\n\n"
            "Install with:\n"
            "    npm install -g @openai/codex\n"
        )
        return 127
    return subprocess.run([bin_, "login"]).returncode


def run_codex_logout() -> int:
    bin_ = codex_cli_path()
    if not bin_:
        if CODEX_AUTH_FILE.exists():
            CODEX_AUTH_FILE.unlink()
            print(f"removed {CODEX_AUTH_FILE} (codex CLI not installed)")
            return 0
        return 0
    return subprocess.run([bin_, "logout"]).returncode


# ─── Anthropic Claude.com OAuth ─────────────────────────────────────────────


def _anthropic_token_file() -> Optional[Path]:
    if ANTHROPIC_TOKEN_FILE.exists():
        return ANTHROPIC_TOKEN_FILE
    if ANTHROPIC_TOKEN_FALLBACK.exists():
        return ANTHROPIC_TOKEN_FALLBACK
    return None


def anthropic_token_status() -> TokenStatus:
    path = _anthropic_token_file()
    if path is None:
        return TokenStatus(provider="anthropic-oauth", has_file=False,
                           file_path=ANTHROPIC_TOKEN_FILE)
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        return TokenStatus(provider="anthropic-oauth", has_file=True,
                           file_path=path, error=f"unreadable: {e}")
    tok = data.get("claude") or {}
    saved_at = tok.get("savedAt") or 0
    expires_in = tok.get("expires_in") or 0
    expires_at = int((saved_at + expires_in * 1000) / 1000) if saved_at else None
    expired = bool(expires_at and expires_at < int(time.time()))
    return TokenStatus(
        provider="anthropic-oauth",
        has_file=True,
        file_path=path,
        expires_at=expires_at,
        expired=expired,
    )
