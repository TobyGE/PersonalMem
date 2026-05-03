"""Guided one-time onboarding: ``personalmem setup``.

Walks the user through 5 steps with active verification at each:

  1. Swift binaries built (mac-ax-watcher / mac-ax-helper / mac-frontcap /
     mac-vision-ocr) — compiles them on demand if sources are present.
  2. macOS Accessibility permission — actively probes by calling
     mac-ax-helper and checking it returns a non-empty AX tree.
  3. macOS Screen Recording permission — actively probes by calling
     mac-frontcap and checking the resulting JPEG is non-zero / non-black.
  4. LLM provider configured — delegates to the existing onboard.py
     picker (Ollama / Anthropic OAuth / Codex OAuth / API keys).
  5. Smoke test — does one full LLM round-trip with the configured
     provider to confirm the routing/summarize pipeline can actually
     reach the model.

Each step prints status, fixes what it can, and pauses for manual
steps the OS won't let us automate (the two privacy permissions).

Re-runnable: every check returns the same answer the second time, so
``personalmem setup`` is idempotent until something changes upstream.

Adapted from OpenSeer's setup_wizard.
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod
from . import config as oc_config
from . import onboard
from . import paths
from . import ui


# ─── Step 1: Swift binaries ──────────────────────────────────────────────────


_SWIFT_BINARIES = ("mac-ax-watcher", "mac-ax-helper", "mac-frontcap", "mac-vision-ocr")


def _resources_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "resources"


def check_swift_binaries() -> bool:
    """Verify all 4 bundled Swift binaries exist and are executable.
    On macOS, attempt to build any that are missing using the bundled
    build scripts.
    """
    res = _resources_dir()
    if not res.is_dir():
        ui.fail(f"resources dir not found: {res}")
        return False

    missing: list[str] = []
    for name in _SWIFT_BINARIES:
        bin_ = res / name
        if not (bin_.is_file() and os.access(bin_, os.X_OK)):
            missing.append(name)

    if not missing:
        ui.ok(f"all 4 Swift binaries present in {res}")
        return True

    ui.warn(f"missing: {', '.join(missing)}")
    if sys.platform != "darwin":
        ui.fail("not on macOS — can't build Swift binaries")
        return False

    if not ui.ask("build them now? [Y/n]"):
        return False

    for name in missing:
        script = res / f"build-{name}.sh"
        if not script.is_file():
            ui.fail(f"build script not found: {script}")
            return False
        ui.info(f"running {script.name}...")
        rc = subprocess.run(["bash", str(script)]).returncode
        if rc != 0:
            ui.fail(f"{script.name} exited {rc}")
            return False
        bin_ = res / name
        if not bin_.is_file():
            ui.fail(f"build claimed success but {bin_} still missing")
            return False
        ui.ok(f"built {name}")
    return True


# ─── Step 2: Accessibility permission ────────────────────────────────────────


def check_accessibility() -> bool:
    """Run mac-ax-helper once and check we get a real AX tree back.
    macOS denies AX queries silently when the perm is missing — the
    helper exits 0 but the tree's ``apps`` list is empty.
    """
    if sys.platform != "darwin":
        ui.warn("not on macOS — skipping AX check")
        return True

    ui.info("PersonalMem needs Accessibility permission to read AX trees.")
    ui.info("System Settings → Privacy & Security → Accessibility, add and")
    ui.info(f"enable the terminal you're running from (e.g. {ui.c('iTerm', ui.CYN)})")
    ui.info(f"or {ui.c('Terminal', ui.CYN)}. After granting, fully Cmd+Q the terminal")
    ui.info("and reopen.")
    print()
    if not ui.ask("Granted? Press Enter to test."):
        return False

    from .capture import ax_capture
    provider = ax_capture.create_provider(depth=1, timeout=3)
    if not provider.available:
        ui.fail("ax_capture provider unavailable (helper binary missing?)")
        return False

    result = provider.capture_frontmost(focused_window_only=True)
    if result is None:
        ui.fail("AX capture returned None — perm likely denied or helper crashed")
        return False

    apps = (result.raw_json or {}).get("apps") or []
    if not apps:
        ui.fail("AX tree has no apps — perm denied (silent macOS failure mode)")
        return False

    front = next((a for a in apps if a.get("is_frontmost")), apps[0])
    ui.ok(f"AX read OK — frontmost app: {front.get('name', '?')}")
    return True


# ─── Step 3: Screen Recording permission ─────────────────────────────────────


def check_screen_recording() -> bool:
    """Run mac-frontcap and verify the JPEG is non-empty + non-black.
    macOS returns a black image when the perm is missing.
    """
    if sys.platform != "darwin":
        ui.warn("not on macOS — skipping Screen Recording check")
        return True

    ui.info("PersonalMem needs Screen Recording permission to grab the active")
    ui.info("window when AX text is sparse (videos, canvas apps).")
    ui.info("System Settings → Privacy & Security → Screen Recording — add the")
    ui.info("same terminal there too. After granting, restart the terminal.")
    print()
    if not ui.ask("Granted? Press Enter to test."):
        return False

    from .capture import screenshot
    shot = screenshot.grab(max_width=640, jpeg_quality=70)
    if shot is None:
        ui.fail("screenshot.grab returned None — perm likely denied")
        return False

    try:
        from PIL import Image
    except ImportError:
        ui.warn("Pillow not installed; can't verify image content")
        ui.ok(f"got JPEG of size {shot.width}×{shot.height}")
        return True

    img = Image.open(io.BytesIO(base64.b64decode(shot.image_base64)))
    if img.size[0] < 100 or img.size[1] < 100:
        ui.fail(f"capture {img.size[0]}×{img.size[1]} is suspiciously small")
        return False
    extrema = img.getextrema()
    if all(lo == hi == 0 for lo, hi in extrema if isinstance(lo, int)):
        ui.fail("capture is all-black — perm denied")
        return False
    ui.ok(f"captured {img.size[0]}×{img.size[1]} active window — Screen Recording OK")
    return True


# ─── Step 4: LLM provider configured ─────────────────────────────────────────


def check_llm_configured() -> bool:
    """Pick + verify an LLM provider. If config already has a non-default
    provider, keep it; otherwise launch the picker.
    """
    cfg_path = paths.config_file()
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(oc_config.DEFAULT_CONFIG_TEMPLATE)

    cfg = oc_config.load(cfg_path)
    default = cfg.models.get("default")
    has_provider = bool(default and default.model)
    flag = paths.root() / ".onboarded"

    if has_provider and flag.exists():
        ui.ok(f"LLM provider configured: {default.model}")
        if not ui.ask_no("Re-pick? [y/N]"):
            return True

    # Run the picker (writes to config.toml + drops .onboarded sentinel).
    # Provider sub-flows (LM Studio not running, codex CLI not installed,
    # PKCE timeout etc.) raise RuntimeError with their own user-facing
    # message; surface those as a friendly fail rather than a traceback.
    try:
        ran = onboard.run_onboarding(force=True)
    except (KeyboardInterrupt, EOFError):
        ui.fail("onboarding cancelled")
        return False
    except RuntimeError as e:
        ui.fail(f"onboarding aborted: {e}")
        return False
    if not ran:
        ui.fail("onboarding skipped (non-interactive shell?)")
        return False
    cfg = oc_config.load(cfg_path)
    default = cfg.models.get("default")
    if not (default and default.model):
        ui.fail("config has no default model after onboarding")
        return False
    ui.ok(f"LLM provider configured: {default.model}")
    return True


# ─── Step 5: Smoke test ──────────────────────────────────────────────────────


def smoke_test() -> bool:
    """Round-trip a tiny prompt through the configured LLM."""
    cfg = oc_config.load()
    from .llm import call_llm, extract_text

    ui.info("pinging the configured model with a 1-line prompt...")
    t0 = time.monotonic()
    try:
        resp = call_llm(
            cfg, "default",
            messages=[
                {"role": "system", "content": "Reply with exactly one word: ok"},
                {"role": "user", "content": "ping"},
            ],
            json_mode=False,
        )
    except Exception as e:  # noqa: BLE001
        ui.fail(f"LLM call failed: {e}")
        return False
    text = extract_text(resp).strip().lower()
    dt = time.monotonic() - t0
    if not text:
        ui.fail(f"LLM returned empty content (took {dt:.1f}s)")
        return False
    ui.ok(f"model responded in {dt:.1f}s: {text[:60]!r}")
    return True


# ─── Main ────────────────────────────────────────────────────────────────────


def run_setup() -> int:
    print(ui.c("\nPersonalMem setup", ui.BOLD)
          + " " + ui.c("— let's get you running", ui.DIM))

    ui.step(1, 5, "Swift binaries built")
    if not check_swift_binaries():
        print(ui.c("\nSetup paused — fix the binaries and re-run.", ui.YEL))
        return 1

    ui.step(2, 5, "macOS Accessibility permission")
    if not check_accessibility():
        print(ui.c("\nSetup paused — fix Accessibility and re-run.", ui.YEL))
        return 2

    ui.step(3, 5, "macOS Screen Recording permission")
    if not check_screen_recording():
        print(ui.c("\nSetup paused — fix Screen Recording and re-run.", ui.YEL))
        return 3

    ui.step(4, 5, "LLM provider configured")
    if not check_llm_configured():
        print(ui.c("\nSetup paused — re-run when an LLM provider is ready.", ui.YEL))
        return 4

    ui.step(5, 5, "Smoke test (model ping)")
    if not smoke_test():
        print(ui.c("\nSetup paused — model call failed; check network or token.", ui.YEL))
        return 5

    print(ui.c("\nAll set.", ui.GRN, ui.BOLD)
          + " " + ui.c("Run", ui.DIM) + " "
          + ui.c("personalmem start", ui.CYN, ui.BOLD)
          + " " + ui.c("to launch the capture daemon.\n", ui.DIM))
    return 0


# ─── Doctor (read-only diagnostic) ───────────────────────────────────────────


def run_doctor() -> int:
    """Read-only equivalent of run_setup: report status of every step
    without prompting or running side effects. Useful in scripts and
    when something starts misbehaving.
    """
    print(ui.c("PersonalMem doctor", ui.BOLD) + " " + ui.c("— diagnostic", ui.DIM))
    overall_ok = True

    # Binaries
    res = _resources_dir()
    print()
    print(ui.c("Swift binaries", ui.BOLD))
    for name in _SWIFT_BINARIES:
        bin_ = res / name
        if bin_.is_file() and os.access(bin_, os.X_OK):
            ui.ok(name)
        else:
            ui.fail(f"{name} missing or not executable")
            overall_ok = False

    # AX provider availability (no perm prompt — just whether the
    # helper resolves)
    print()
    print(ui.c("Capture daemon", ui.BOLD))
    from .capture import ax_capture, screenshot
    provider = ax_capture.create_provider(depth=1, timeout=2)
    if provider.available:
        ui.ok("ax_capture provider resolved")
    else:
        ui.fail("ax_capture unavailable (helper missing or non-macOS)")
        overall_ok = False
    if screenshot._resolve_frontcap_path() is not None:
        ui.ok("mac-frontcap binary resolved")
    else:
        ui.warn("mac-frontcap not resolved (screenshots disabled)")

    # Auth
    print()
    print(ui.c("Auth", ui.BOLD))
    cs = auth_mod.codex_token_status()
    if cs.error:
        ui.fail(cs.summary())
        overall_ok = False
    elif cs.has_file and not cs.expired:
        ui.ok(cs.summary())
    elif cs.has_file:
        ui.warn(cs.summary())
    else:
        ui.info(cs.summary())
    as_ = auth_mod.anthropic_token_status()
    if as_.error:
        ui.fail(as_.summary())
        overall_ok = False
    elif as_.has_file and not as_.expired:
        ui.ok(as_.summary())
    elif as_.has_file:
        ui.warn(as_.summary())
    else:
        ui.info(as_.summary())

    # Config
    print()
    print(ui.c("Config", ui.BOLD))
    cfg_path = paths.config_file()
    if cfg_path.exists():
        ui.ok(f"config: {cfg_path}")
        cfg = oc_config.load(cfg_path)
        default = cfg.models.get("default")
        if default and default.model:
            ui.info(f"default model: {default.model}")
        else:
            ui.fail("no default model in [models.default]")
            overall_ok = False
    else:
        ui.fail(f"no config at {cfg_path} — run `personalmem setup`")
        overall_ok = False

    # Storage paths
    print()
    print(ui.c("Storage", ui.BOLD))
    for label, p in [
        ("root", paths.root()),
        ("index_db", paths.index_db()),
        ("capture-buffer", paths.capture_buffer_dir()),
        ("logs", paths.logs_dir()),
    ]:
        exists = p.exists()
        marker = ui.ok if exists else ui.warn
        marker(f"{label}: {p} {'(exists)' if exists else '(will be created)'}")

    print()
    if overall_ok:
        ui.ok("all checks passed")
        return 0
    ui.fail("some checks failed — see above")
    return 1
