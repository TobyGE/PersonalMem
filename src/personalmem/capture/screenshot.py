"""Screenshot capture via mss + PIL. Extracted from Einsia-Partner capture_service.py."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from ..logger import get

logger = get("personalmem.capture")


@dataclass
class Screenshot:
    image_base64: str
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0


def grab(
    max_width: int = 1920,
    jpeg_quality: int = 80,
    *,
    crop_to: tuple[int, int, int, int] | None = None,
) -> Screenshot | None:
    """Capture the primary monitor and return a base64-encoded JPEG.

    ``crop_to``: optional (x, y, width, height) in logical points (the same
    coordinate system the macOS AppleScript ``position`` / ``size`` of a
    window returns). When provided, the captured frame is cropped to that
    rectangle so the resulting image is just the active window — much
    less noise for downstream LLM consumption than a full-screen frame
    cluttered with other apps and the menu bar.
    """
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        logger.warning("mss/Pillow not installed: %s", exc)
        return None

    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            if len(monitors) < 2:
                logger.warning("No monitors reported by mss")
                return None
            mon = monitors[1]  # index 0 is the "all monitors" virtual screen
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.rgb)
    except Exception as exc:  # noqa: BLE001 — mss can raise a variety of OS errors
        logger.warning("Screenshot grab failed: %s", exc)
        return None

    if crop_to is not None:
        img = _crop_to_window(img, mon, crop_to)
        if img is None:
            return None

    if max_width and img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return Screenshot(image_base64=encoded, width=img.width, height=img.height)


def _crop_to_window(img, mon: dict, crop_to: tuple[int, int, int, int]):
    """Crop an mss-captured image to a window rect given in logical points.

    mss returns physical pixels; AppleScript window bounds are logical points.
    On Retina displays these differ by the display's backing scale (typically
    2x). We infer scale from the ratio of the captured image's pixel width to
    the monitor's logical width as reported by mss.
    """
    from PIL import Image  # already imported but keep module-local

    x_pt, y_pt, w_pt, h_pt = crop_to
    if w_pt <= 0 or h_pt <= 0:
        logger.warning("crop_to had non-positive size %s; returning full frame", crop_to)
        return img

    mon_width = mon.get("width") or img.width
    mon_height = mon.get("height") or img.height
    # mss's "left"/"top" for monitor[1] are 0-based for primary on macOS;
    # window position is also relative to the same origin.
    mon_left = mon.get("left", 0)
    mon_top = mon.get("top", 0)

    # Scale from logical points → physical pixels. If mss already returns
    # logical (some setups), scale will be ~1.
    scale_x = img.width / mon_width if mon_width else 1
    scale_y = img.height / mon_height if mon_height else 1

    px = int(round((x_pt - mon_left) * scale_x))
    py = int(round((y_pt - mon_top) * scale_y))
    pw = int(round(w_pt * scale_x))
    ph = int(round(h_pt * scale_y))

    # Clamp to image bounds
    px = max(0, px)
    py = max(0, py)
    right = min(img.width, px + pw)
    bottom = min(img.height, py + ph)
    if right <= px or bottom <= py:
        logger.warning(
            "crop rect (%d,%d,%d,%d) outside image %dx%d; falling back to full frame",
            px, py, pw, ph, img.width, img.height,
        )
        return img

    return img.crop((px, py, right, bottom))
