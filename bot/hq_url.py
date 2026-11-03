"""HQ base URL from the environment. No built-in host."""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

_masked: set[str] = set()


def mask_value(value: str) -> None:
    text = (value or "").strip()
    if len(text) < 4 or text in _masked:
        return
    _masked.add(text)
    print(f"::add-mask::{text}", file=sys.stderr)


def hq_url() -> str:
    """Return HQ_URL. Raises RuntimeError when it is missing."""
    raw = (os.environ.get("HQ_URL") or "").strip().rstrip("/")
    if not raw:
        raise RuntimeError("HQ_URL is not set")
    if "://" not in raw:
        raw = "https://" + raw
    mask_value(raw)
    host = urlparse(raw).hostname or ""
    if host:
        mask_value(host)
    return raw
