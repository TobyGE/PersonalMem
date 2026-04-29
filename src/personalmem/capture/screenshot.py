"""Screenshot capture via the bundled ``mac-frontcap`` Swift binary.

The binary uses ``ScreenCaptureKit`` (macOS 12.3+) to grab the frontmost
window directly — no full-screen mss grab + Quartz-bounds crop math, no
Retina scale dance, and no popup-picker mis-targeting (which is why the
old mss-based path was disabled by default).

The capture path is:

    mac-frontcap <tmpdir> <maxSide>   # writes one PNG, prints its path
    → re-encode PNG → JPEG (Pillow)
    → base64-encode for embedding in the capture JSON
"""

from __future__ import annotations

import base64
import io
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..logger import get

logger = get("personalmem.capture")


@dataclass
class Screenshot:
    image_base64: str
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0


def _resolve_frontcap_path() -> Path | None:
    """Find or build the mac-frontcap binary.

    Search order mirrors the AX-binary helpers:
      1. PERSONALMEM_FRONTCAP env override
      2. Packaged resource (_bundled/) shipped with a wheel
      3. Dev source tree (../../../resources/)
    """
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("PERSONALMEM_FRONTCAP")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("PERSONALMEM_FRONTCAP set but not executable: %s", p)

    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("personalmem").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-frontcap")
    except (ModuleNotFoundError, ValueError):
        pass

    dev_root = Path(__file__).resolve().parents[3]
    candidates.append(dev_root / "resources" / "mac-frontcap")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        if swift_path.is_file():
            from .ax_capture import _maybe_compile  # lazy: avoid import cycle
            _maybe_compile(swift_path, binary_path)
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    return None


def grab(
    max_width: int = 1280,
    jpeg_quality: int = 80,
) -> Screenshot | None:
    """Capture the frontmost window and return a base64-encoded JPEG.

    ``max_width`` is passed to mac-frontcap as ``maxSide`` — the longer
    side of the captured window is downscaled to fit. We then re-encode
    to JPEG for storage compactness (PNGs of UI screenshots are 5-10×
    larger than visually equivalent JPEGs).
    """
    binary = _resolve_frontcap_path()
    if binary is None:
        logger.warning(
            "mac-frontcap not found. Build it: bash resources/build-mac-frontcap.sh"
        )
        return None

    try:
        from PIL import Image
    except ImportError as exc:
        logger.warning("Pillow not installed: %s", exc)
        return None

    with tempfile.TemporaryDirectory(prefix="personalmem-shot-") as tmpdir:
        try:
            result = subprocess.run(
                [str(binary), tmpdir, str(max_width)],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            logger.warning("mac-frontcap timed out")
            return None
        except OSError as exc:
            logger.warning("mac-frontcap exec failed: %s", exc)
            return None

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning("mac-frontcap rc=%d: %s", result.returncode, stderr)
            return None

        png_path = Path((result.stdout or "").strip())
        if not png_path.is_file():
            logger.warning("mac-frontcap returned bad path: %r", result.stdout)
            return None

        try:
            img = Image.open(png_path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read PNG from mac-frontcap: %s", exc)
            return None

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return Screenshot(image_base64=encoded, width=img.width, height=img.height)
