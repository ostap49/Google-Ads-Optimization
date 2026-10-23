"""Image loading + validation for PMax / Demand Gen image assets (local path or URL)."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

# field type -> (aspect ratio, min width, min height)
IMAGE_SPECS = {
    "MARKETING_IMAGE": (1.91, 600, 314),
    "SQUARE_MARKETING_IMAGE": (1.0, 300, 300),
    "PORTRAIT_MARKETING_IMAGE": (0.8, 480, 600),
    "TALL_PORTRAIT_MARKETING_IMAGE": (0.5625, 600, 1067),
    "LOGO": (1.0, 128, 128),
    "LANDSCAPE_LOGO": (4.0, 512, 128),
}
MAX_BYTES = 5 * 1024 * 1024


def fetch_bytes(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        import httpx
        resp = httpx.get(src, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        return resp.content
    p = Path(src).expanduser()
    if not p.is_file():
        raise ValueError(f"Image not found: {src}")
    return p.read_bytes()


def inspect_image(src: str, field_type: str) -> dict[str, Any]:
    from PIL import Image

    data = fetch_bytes(src)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{src}: {len(data) / 1e6:.1f} MB > 5 MB limit")
    try:
        img = Image.open(io.BytesIO(data))
        w, h, fmt = img.width, img.height, (img.format or "").upper()
    except Exception as ex:  # noqa: BLE001
        raise ValueError(f"{src}: not a readable image ({ex})") from None
    if fmt not in ("JPEG", "PNG", "GIF", "WEBP"):
        raise ValueError(f"{src}: format {fmt} not supported (use JPG/PNG)")
    ratio, min_w, min_h = IMAGE_SPECS[field_type]
    actual = w / h
    if abs(actual - ratio) / ratio > 0.02:
        raise ValueError(f"{src}: {w}x{h} has aspect {actual:.2f}, {field_type} needs {ratio:g}:1")
    if w < min_w or h < min_h:
        raise ValueError(f"{src}: {w}x{h} below minimum {min_w}x{min_h} for {field_type}")
    return {"src": src, "field_type": field_type, "width": w, "height": h, "format": fmt, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def load_verified(meta: dict[str, Any]) -> bytes:
    """Re-read at build/apply time and make sure it is the same image that was validated."""
    data = fetch_bytes(meta["src"])
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise ValueError(f"{meta['src']} changed since the proposal was created — propose again.")
    return data
