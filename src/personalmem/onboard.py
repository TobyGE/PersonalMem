"""Interactive onboarding: pick an LLM provider on first run.

Four options, two local + two OAuth:

- **Ollama** — local, free. Probes ``http://localhost:11434/api/tags``.
- **LM Studio** — local, free, MLX-friendly. Probes
  ``http://localhost:1234/v1/models`` (OpenAI-compatible server).
- **Anthropic OAuth** — Claude.com Pro/Max subscription via PKCE.
  Tokens land in ``~/.personalmem/oauth-tokens.json`` (mode 0600); the
  LLM dispatcher auto-detects them when a Claude-family model has no
  ``api_key`` set.
- **Codex OAuth** — ChatGPT Plus/Pro subscription. Delegates to the
  ``codex login`` browser flow; tokens live in ``~/.codex/auth.json``.
  Routes via ``llm/codex_oauth.py``.

The chosen provider is written into ``[models.default]`` of
``~/.personalmem/config.toml`` (the rest of the file is preserved). A
sentinel ``~/.personalmem/.onboarded`` marks the flow as done so future
``personalmem start`` invocations skip it.

Re-run anytime with ``personalmem setup`` (alias: ``personalmem onboard``).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from . import paths


# Public client_id used by Claude Code; safe to reuse for any subscriber.
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_AUTH_URL = "https://claude.com/cai/oauth/authorize"
_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_OAUTH_SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"
_CALLBACK_PORT = 54321
_CALLBACK_PATH = "/callback"
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"

_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"


# ─── Public entry points ─────────────────────────────────────────────────────


def needs_onboarding() -> bool:
    return not (paths.root() / ".onboarded").exists()


def run_onboarding(*, force: bool = False) -> bool:
    """Returns True if onboarding ran to completion, False if skipped."""
    flag = paths.root() / ".onboarded"
    if flag.exists() and not force:
        return False

    if not sys.stdin.isatty():
        # Non-interactive shell (launchd, pipe, etc.) — skip silently and
        # let the user run `personalmem onboard` later.
        return False

    paths.ensure_dirs()
    print()
    print("─" * 60)
    print("  Welcome to PersonalMem")
    print("─" * 60)
    print()
    print("Pick an LLM provider for routing + summarization:")
    print()
    print("  [1] Ollama          local, free  (probes localhost:11434)")
    print("  [2] LM Studio       local, free  (probes localhost:1234, MLX-friendly)")
    print("  [3] Anthropic OAuth Claude.com Pro/Max subscription (PKCE)")
    print("  [4] Codex OAuth     ChatGPT Plus/Pro subscription (via codex CLI)")
    print()

    while True:
        choice = (input("Choice [1-4, default 1]: ").strip() or "1")
        if choice in {"1", "2", "3", "4"}:
            break
        print("  please enter 1-4")

    if choice == "1":
        block = _onboard_ollama()
    elif choice == "2":
        block = _onboard_lm_studio()
    elif choice == "3":
        block = _onboard_anthropic_oauth()
    else:
        block = _onboard_codex_oauth()

    _write_models_default(block)
    flag.touch()

    print()
    print("✓ Onboarding complete. Config: " + str(paths.config_file()))
    print()
    return True


# ─── Provider: Ollama ────────────────────────────────────────────────────────


def _onboard_ollama() -> dict[str, Any]:
    print()
    print("[Ollama] checking http://localhost:11434 ...")
    models = _ollama_list_models()
    if models is None:
        print("  Ollama isn't running. To install:")
        print("    brew install ollama")
        print("    ollama serve &")
        print(f"    ollama pull {_DEFAULT_OLLAMA_MODEL}")
        print(f"  Defaulting to '{_DEFAULT_OLLAMA_MODEL}' — pull it before running.")
        chosen = _DEFAULT_OLLAMA_MODEL
    elif not models:
        print("  Ollama is running but has no models installed.")
        print(f"    ollama pull {_DEFAULT_OLLAMA_MODEL}")
        chosen = _DEFAULT_OLLAMA_MODEL
    else:
        print(f"  Found {len(models)} installed model(s):")
        for i, m in enumerate(models, 1):
            print(f"    [{i}] {m}")
        while True:
            raw = input(f"  Pick [1-{len(models)}, default 1]: ").strip() or "1"
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(models):
                    chosen = models[idx]
                    break
            except ValueError:
                pass
            print("  invalid selection")

    return {
        "model": f"ollama/{chosen}",
        "num_ctx": 32768,
        "max_tokens": 2048,
    }


def _ollama_list_models() -> list[str] | None:
    """Return list of installed model names, or None if Ollama is unreachable."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return [m.get("name", "") for m in data.get("models") or [] if m.get("name")]


# ─── Provider: LM Studio ────────────────────────────────────────────────────


def _onboard_lm_studio() -> dict[str, Any]:
    print()
    print("[LM Studio] checking http://localhost:1234 ...")
    models = _lm_studio_list_models()
    if models is None:
        print("  LM Studio server isn't running. Open the LM Studio app, go to")
        print("  the Developer tab (left sidebar), toggle 'Status: Running on")
        print("  port 1234', then re-run `personalmem setup` and pick this option.")
        raise RuntimeError("LM Studio server not reachable on localhost:1234")
    if not models:
        print("  LM Studio is running but has no model loaded.")
        print("  Pick a model in My Models → Load, then re-run.")
        raise RuntimeError("LM Studio has no model loaded")
    print(f"  Found {len(models)} loaded model(s):")
    for i, m in enumerate(models, 1):
        print(f"    [{i}] {m}")
    while True:
        raw = input(f"  Pick [1-{len(models)}, default 1]: ").strip() or "1"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                chosen = models[idx]
                break
        except ValueError:
            pass
        print("  invalid selection")

    return {
        # litellm routes openai/* with explicit base_url through its
        # OpenAI-compatible client — what LM Studio's server speaks.
        "model": f"openai/{chosen}",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",   # placeholder, LM Studio ignores it
        "max_tokens": 4096,
    }


def _lm_studio_list_models() -> list[str] | None:
    """Return loaded model IDs, or None if the server isn't reachable.
    LM Studio uses the OpenAI /v1/models shape: {"data": [{"id": ...}]}.
    """
    try:
        with urllib.request.urlopen("http://localhost:1234/v1/models", timeout=2) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    out = []
    for m in data.get("data") or []:
        mid = m.get("id") or ""
        # Skip embedding models — they're not chat-capable
        if "embed" in mid.lower():
            continue
        if mid:
            out.append(mid)
    return out


# ─── Provider: Codex OAuth (ChatGPT Plus/Pro via codex CLI) ─────────────────


def _onboard_codex_oauth() -> dict[str, Any]:
    """Use Codex CLI's stored OAuth tokens to talk to chatgpt.com.

    No PKCE flow on our side — we delegate ``codex login`` to the
    official binary, which handles the browser dance and writes
    ``~/.codex/auth.json``. PersonalMem then reads that file at LLM
    call time (via ``llm/codex_oauth.py``).
    """
    from . import auth as auth_mod

    print()
    print("[Codex OAuth — ChatGPT Plus/Pro subscription via codex CLI]")
    print()

    # Codex CLI must be installed
    if auth_mod.codex_cli_path() is None:
        print("  Codex CLI not found on PATH. Install with:")
        print("    npm install -g @openai/codex")
        print()
        print("  After installation, re-run `personalmem onboard` and pick this option.")
        raise RuntimeError("codex CLI not installed")

    # Already logged in?
    st = auth_mod.codex_token_status()
    if st.has_file and not st.expired and not st.error:
        print(f"  ✓ already logged in: {st.summary()}")
    else:
        if st.expired:
            print("  ⚠ token expired; running `codex login`...")
        else:
            print("  Running `codex login` (browser flow)...")
        rc = auth_mod.run_codex_login()
        if rc != 0:
            raise RuntimeError(f"codex login exited {rc}")
        st = auth_mod.codex_token_status()
        if not st.has_file or st.expired:
            raise RuntimeError("codex login completed but token still missing/expired")
        print(f"  ✓ logged in: {st.summary()}")

    return {
        "model": "gpt-5.5",   # the only Codex-account-allowed model family
        "max_tokens": 4096,
    }


# ─── Provider: Anthropic OAuth ───────────────────────────────────────────────


def _onboard_anthropic_oauth() -> dict[str, Any]:
    print()
    print("[Anthropic OAuth — Claude.com subscription]")
    verifier, challenge = _generate_pkce()
    state = secrets.token_hex(16)

    params = {
        "response_type": "code",
        "client_id": _OAUTH_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "scope": _OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = _OAUTH_AUTH_URL + "?" + urllib.parse.urlencode(params)

    print()
    print("  Opening Claude.com in your browser to authorize...")
    print(f"  If the browser doesn't open, visit:\n    {auth_url}\n")

    server, result = _start_callback_server()
    try:
        webbrowser.open(auth_url)
        deadline = time.time() + 120
        while time.time() < deadline and result["code"] is None and result["error"] is None:
            time.sleep(0.2)
        if result["code"] is None:
            raise RuntimeError(result["error"] or "OAuth timeout (2 min)")
        if result["state"] != state:
            raise RuntimeError("OAuth state mismatch (possible CSRF)")
    finally:
        server.shutdown()

    print("  Callback received, exchanging code for token...")
    tokens = _exchange_code(result["code"], verifier, state)
    tokens["savedAt"] = int(time.time() * 1000)

    token_file = paths.root() / "oauth-tokens.json"
    existing: dict[str, Any] = {}
    if token_file.exists():
        try:
            existing = json.loads(token_file.read_text())
        except json.JSONDecodeError:
            existing = {}
    existing["claude"] = tokens
    token_file.write_text(json.dumps(existing, indent=2))
    token_file.chmod(0o600)

    print(f"  ✓ Tokens saved to {token_file}")

    return {
        "model": _DEFAULT_ANTHROPIC_MODEL,  # no "anthropic/" prefix → OAuth path
        "max_tokens": 2048,
    }


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _start_callback_server() -> tuple[socketserver.TCPServer, dict[str, Any]]:
    """Spin up a one-shot HTTP server on _CALLBACK_PORT to catch the redirect."""
    result: dict[str, Any] = {"code": None, "state": None, "error": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default logging
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != _CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            result["code"] = (qs.get("code") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            err = (qs.get("error") or [None])[0]
            if err:
                result["error"] = f"OAuth error: {err}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            ok = err is None
            body = (
                "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
                f"<h2>{'✓ Connected!' if ok else '✗ Authorization failed'}</h2>"
                f"<p>{'You can close this tab and return to PersonalMem.' if ok else err}</p>"
                "<script>window.close()</script></body></html>"
            )
            self.wfile.write(body.encode())

    server = socketserver.TCPServer(("127.0.0.1", _CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, result


def _exchange_code(code: str, verifier: str, state: str) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "client_id": _OAUTH_CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
    }
    req = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Token exchange failed ({e.code}): {body}") from e


# ─── Config rewrite ──────────────────────────────────────────────────────────


_MODELS_DEFAULT_RE = re.compile(
    r"(?m)^\[models\.default\][^\n]*\n(?:^(?!\[)[^\n]*\n)*"
)


def _format_block(values: dict[str, Any]) -> str:
    lines = ["[models.default]"]
    # Stable key order for readability.
    for key in ("model", "api_key", "api_key_env", "base_url", "num_ctx", "max_tokens"):
        if key not in values:
            continue
        v = values[key]
        if isinstance(v, str):
            lines.append(f'{key} = "{v}"')
        else:
            lines.append(f"{key} = {v}")
    return "\n".join(lines) + "\n\n"


def _write_models_default(values: dict[str, Any]) -> None:
    from . import config as oc_config

    cfg_path = paths.config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(oc_config.DEFAULT_CONFIG_TEMPLATE)

    text = cfg_path.read_text()
    block = _format_block(values)

    if _MODELS_DEFAULT_RE.search(text):
        text = _MODELS_DEFAULT_RE.sub(block, text, count=1)
    else:
        text = block + "\n" + text

    cfg_path.write_text(text)
    # api_key may be present in cleartext; tighten perms.
    if "api_key" in values:
        cfg_path.chmod(0o600)
