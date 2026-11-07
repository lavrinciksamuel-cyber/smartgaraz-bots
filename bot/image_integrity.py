"""Completeness checks for product images.

Flags truncated files, decoder errors, a short body, and a large flat
mid-gray band along the bottom. Callers decide whether to retry or skip.
"""
from __future__ import annotations

import os
import time
from io import BytesIO
from typing import Any, Optional

# Reasons that mean the bytes themselves are not a usable image.
CORRUPT_REASONS = frozenset({
    "empty",
    "content_length",
    "jpeg_eoi",
    "png_iend",
    "decode",
    "gray_fill",
})

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_BRIDGE_PATH = "/wp-json/smartgaraz/v1/upload-image"


def image_magic(data: bytes) -> str:
    if data.startswith(_JPEG_SOI):
        return "jpeg"
    if data.startswith(_PNG_SIG):
        return "png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "webp"
    return ""


def _jpeg_eoi_missing(data: bytes) -> bool:
    tail = data[-32:] if len(data) > 32 else data
    return _JPEG_EOI not in tail


def _png_iend_missing(data: bytes) -> bool:
    tail = data[-32:] if len(data) > 32 else data
    return b"IEND" not in tail


def _declared_length(resp: Any) -> Optional[int]:
    headers = getattr(resp, "headers", None) or {}
    encoding = str(headers.get("Content-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    raw = str(headers.get("Content-Length") or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _decode(data: bytes):
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        return None
    return img


def _row_is_flat_midgray(px, y: int, width: int) -> bool:
    """True when every pixel in the row is a near-constant ~(128, 128, 128)."""
    anchor = None
    for x in range(width):
        r, g, b = px[x, y][:3]
        if abs(r - 128) > 2 or abs(g - 128) > 2 or abs(b - 128) > 2:
            return False
        if anchor is None:
            anchor = (r, g, b)
            continue
        if abs(r - anchor[0]) > 2 or abs(g - anchor[1]) > 2 or abs(b - anchor[2]) > 2:
            return False
    return anchor is not None


def has_gray_fill(img) -> bool:
    """Large contiguous bottom band of flat mid-gray.

    A short strip or a light/white studio background does not match.
    The band must cover at least 20% of the height and 24 rows.
    """
    rgb = img.convert("RGB")
    width, height = rgb.size
    if width < 48 or height < 48:
        return False
    px = rgb.load()
    band = 0
    for y in range(height - 1, -1, -1):
        if not _row_is_flat_midgray(px, y, width):
            break
        band += 1
    need = max(24, (height * 20 + 99) // 100)
    return band >= need


def inspect_bytes(data: bytes, *, declared_length: Optional[int] = None) -> Optional[str]:
    """Return a reason code, or None when the bytes look complete."""
    if not data:
        return "empty"
    if declared_length is not None and declared_length != len(data):
        return "content_length"
    kind = image_magic(data)
    if kind == "jpeg" and _jpeg_eoi_missing(data):
        return "jpeg_eoi"
    if kind == "png" and _png_iend_missing(data):
        return "png_iend"
    if not kind:
        return "decode"
    img = _decode(data)
    if img is None:
        return "decode"
    if has_gray_fill(img):
        return "gray_fill"
    return None


def fetch_verified(
    session,
    url: str,
    *,
    timeout: float = 30,
    pause: float = 1.0,
    verify: bool = False,
    retries: int = 1,
) -> tuple[Optional[bytes], str]:
    """Download once and retry once when the body is incomplete.

    403 and 429 are returned immediately. Success is ``(body, "")``.
    """
    last_data: Optional[bytes] = None
    last = "download"
    attempts = max(1, int(retries) + 1)
    for attempt in range(attempts):
        if attempt and pause > 0:
            time.sleep(pause)
        try:
            resp = session.get(url, timeout=timeout, verify=verify)
        except Exception as exc:
            last = type(exc).__name__
            continue
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (403, 429):
            return None, f"http_{status}"
        if status != 200:
            last = f"http_{status}"
            if status >= 500 and attempt + 1 < attempts:
                continue
            return None, last
        data = resp.content or b""
        last_data = data
        reason = inspect_bytes(data, declared_length=_declared_length(resp))
        if not reason:
            return data, ""
        last = reason
    return last_data, last


def upload_media(data: bytes, filename: str, *, timeout: float = 60) -> tuple[Optional[int], Optional[str], int]:
    """Upload verified bytes through the existing media endpoint.

    Returns ``(attachment id or None, url or None, http_status)``.
    Status 0 means the request was not sent.
    """
    reason = inspect_bytes(data)
    if reason or not data:
        return None, None, 0
    base = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY") or ""
    secret = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not base or not key or not secret:
        return None, None, 0
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    ctype = "image/png" if image_magic(data) == "png" else "image/jpeg"
    try:
        resp = requests.post(
            base + _BRIDGE_PATH,
            params={"filename": filename},
            headers={
                "X-SG-Consumer-Key": key,
                "X-SG-Consumer-Secret": secret,
                "Content-Type": ctype,
                "User-Agent": "smartgaraz-bot",
            },
            data=data,
            timeout=timeout,
            verify=False,
        )
    except Exception:
        return None, None, 0
    status = int(resp.status_code or 0)
    if status >= 400:
        return None, None, status
    try:
        body = resp.json() or {}
    except Exception:
        body = {}
    raw_id = body.get("id")
    try:
        media_id = int(raw_id) if raw_id else None
    except (TypeError, ValueError):
        media_id = None
    url = body.get("url") or body.get("source_url") or None
    return media_id, (str(url) if url else None), status


def upload_jpeg(data: bytes, filename: str, *, timeout: float = 60) -> tuple[Optional[str], int]:
    """Upload verified bytes. Returns ``(url, http_status)``."""
    _media_id, url, status = upload_media(data, filename, timeout=timeout)
    return url, status
