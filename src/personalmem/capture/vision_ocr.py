"""OCR via the bundled ``mac-vision-ocr`` Swift binary (Apple Vision).

Used as a fallback signal source when AX text is sparse — videos, canvas
apps, PDF readers etc. — to keep the routing/summarizer pipeline from
going blind. Output is plain text, joined newline-separated, filtered by
a per-block confidence threshold so stylized-font garbage doesn't slip
through (YouTube thumbnail Chinese tends to mis-decode at 0.3 conf).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("personalmem.capture")


def _resolve_binary_path() -> Path | None:
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("PERSONALMEM_VISION_OCR")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("PERSONALMEM_VISION_OCR set but not executable: %s", p)

    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("personalmem").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-vision-ocr")
    except (ModuleNotFoundError, ValueError):
        pass

    dev_root = Path(__file__).resolve().parents[3]
    candidates.append(dev_root / "resources" / "mac-vision-ocr")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        if swift_path.is_file():
            from .ax_capture import _maybe_compile  # lazy: avoid import cycle
            _maybe_compile(swift_path, binary_path)
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    return None


def run_ocr(
    image_bytes: bytes,
    *,
    min_confidence: float = 0.5,
    timeout: float = 8.0,
) -> dict[str, Any] | None:
    """Run OCR on a JPEG/PNG byte string. Returns None on any failure.

    Output dict shape::

        {
          "text": "block1\\nblock2\\n...",   # confidence-filtered, newline-joined
          "block_count": 72,                 # blocks above threshold
          "block_count_total": 77,           # before filtering
        }
    """
    binary = _resolve_binary_path()
    if binary is None:
        logger.warning(
            "mac-vision-ocr not found. Build it: bash resources/build-mac-vision-ocr.sh"
        )
        return None

    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
        f.write(image_bytes)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [str(binary), tmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("mac-vision-ocr timed out after %.1fs", timeout)
        return None
    except OSError as exc:
        logger.warning("mac-vision-ocr exec failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if result.returncode != 0:
        logger.warning(
            "mac-vision-ocr rc=%d: %s",
            result.returncode, (result.stderr or "").strip()[:200],
        )
        return None

    try:
        data = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        logger.warning("mac-vision-ocr produced bad JSON: %s", exc)
        return None

    blocks = data.get("blocks") or []
    kept = [b for b in blocks if (b.get("confidence") or 0.0) >= min_confidence]
    text = "\n".join((b.get("text") or "") for b in kept).strip()
    return {
        "text": text,
        "block_count": len(kept),
        "block_count_total": len(blocks),
    }
