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
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

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


# ─── Loading + cross-capture merging ────────────────────────────────────────


def load_ocr_text(
    capture_id: str,
    *,
    buffer_dir: Path | None = None,
) -> str:
    """Read the capture's vision_ocr.text field. Empty if missing/malformed."""
    if buffer_dir is None:
        buffer_dir = Path.home() / ".personalmem" / "capture-buffer"
    json_path = buffer_dir / f"{capture_id}.json"
    if not json_path.exists():
        return ""
    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    ocr = data.get("vision_ocr") or {}
    return (ocr.get("text") or "").strip()


# Patterns that vary frame-to-frame within a single phase but identify
# the same content. Replacing with a placeholder before dedup lets us
# collapse "Naval 2.8K views • 2 hours ago" / "... • 3 hours ago" and
# the playback position "1:23 / 5:00" / "1:24 / 5:00" without losing
# stable durations like "29:37" or "1:43:13" (no "/" → not matched).
_VOLATILE_RE = re.compile(
    r"\b\d+\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\b\d{1,3}\s*%\b",
    re.I,
)
_EDGE_NOISE_RE = re.compile(r"^[^\w一-鿿]+|[^\w一-鿿]+$")
_WS_RE = re.compile(r"\s+")


def _normalize(line: str) -> str:
    s = line.lower()
    s = _VOLATILE_RE.sub("§", s)
    s = _EDGE_NOISE_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _is_near_dup(norm: str, kept_norms: list[str], threshold: float) -> bool:
    """Cheap fuzzy match: skip wildly different lengths, then SequenceMatcher."""
    if len(norm) < 6:
        return False
    for k in kept_norms:
        ml = max(len(norm), len(k))
        if ml == 0 or abs(len(norm) - len(k)) / ml > 0.4:
            continue
        if SequenceMatcher(None, norm, k).ratio() >= threshold:
            return True
    return False


def merge_ocr_texts(
    capture_ids: Iterable[str],
    *,
    buffer_dir: Path | None = None,
    fuzzy_threshold: float = 0.82,
) -> str:
    """Union OCR lines across multiple captures with line-level dedup.

    Two-stage:
      1. Exact dedup on a normalized form (lowercased, edge punctuation
         stripped, volatile time/percentage patterns replaced with §).
         Catches obvious repeats like "• Tools" appearing in 5 frames.
      2. Fuzzy dedup via SequenceMatcher (>= ``fuzzy_threshold``) against
         already-kept lines. Catches OCR jitter on the same UI element
         (e.g. "All Bookmarks" / "I All Bookmarks", "SILICON VALLEY" /
         "SILIGON VALLEY").

    Order is the input order with first-seen-kept semantics. Empty
    input or no OCR data → empty string.
    """
    seen_norm: set[str] = set()
    kept_norms: list[str] = []
    kept_lines: list[str] = []
    for cid in capture_ids:
        text = load_ocr_text(cid, buffer_dir=buffer_dir)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            norm = _normalize(line)
            if not norm or norm in seen_norm:
                continue
            if _is_near_dup(norm, kept_norms, fuzzy_threshold):
                continue
            seen_norm.add(norm)
            kept_norms.append(norm)
            kept_lines.append(line)
    return "\n".join(kept_lines)
