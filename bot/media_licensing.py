#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Médium / fotky — licencia + proveniencia (nie TecDoc vehicle DB).

Politika: ``bot/data/CATALOG_LICENSING.md`` § Médiá / § Würth.

- B2B fotka z LKQ/Szekop/Würth = zmluvné / autorské riziko (iné než TecDoc kType).
- Rehost do WP Media = fyzická kópia → vždy zapíš provenienciu.
- Watermark / logo dodávateľa na fotke → zahodiť (Cease & Desist dôkaz).
- Würth studio/CDN (witglobal): len ak ``WURTH_MEDIA_OK=1`` (písomný/B2B súhlas).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# URL / path hinty, že fotka nesie branding dodávateľa (nie čistý packshot).
_SUPPLIER_WATERMARK_RE = re.compile(
    r"("
    r"watermark|vodoznak|water[\-_]?mark|"
    r"lkq[\-_]?logo|autokelly[\-_]?logo|autokelly[\-_]?wm|"
    r"szekop[\-_]?logo|szekop[\-_]?wm|"
    r"wurth[\-_]?logo|wuerth[\-_]?logo|würth[\-_]?logo|"
    r"inter[\-_]?cars[\-_]?logo|"
    r"with[\-_]?logo|branded[\-_]?overlay|supplier[\-_]?badge"
    r")",
    re.I,
)

# Hostitelia, kde je fotka typicky B2B katalóg (OK s provenienciou + aktívnym odberom).
_B2B_IMAGE_HOST_HINTS = (
    "lkq.",
    "lkqcdn",
    "autokelly",
    "nextis.",
    "szekop",
    "witglobal.net",  # Würth media CDN
    "wurth.",
    "myshoptet.com",
    "shoptet.",
)

_WURTH_MEDIA_HOST_HINTS = (
    "witglobal.net",
    "wurth.sk",
    "wurth.cz",
    "eshop.wurth",
)


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower().rstrip(".")


def wurth_media_allowed() -> bool:
    """True len pri explicitnom súhlase na rehost Würth médií (env)."""
    return os.getenv("WURTH_MEDIA_OK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_wurth_media_url(url: str) -> bool:
    host = host_of(url)
    blob = (url or "").lower()
    if any(h in host for h in _WURTH_MEDIA_HOST_HINTS):
        return True
    if "witglobal.net" in blob or "eshop.wurth" in blob:
        return True
    return False


def looks_like_supplier_watermark(url: str, title: str = "") -> bool:
    """True ak URL/title naznačuje vodotlač / logo veľkoobchodu na fotke."""
    blob = f"{url or ''} {title or ''}".lower()
    if not blob.strip():
        return False
    if _SUPPLIER_WATERMARK_RE.search(blob):
        return True
    path = urlparse(url if "://" in (url or "") else f"https://x/{url}").path.lower()
    if re.search(r"/(logo|brand|badge)[-_/]", path) and any(
        h in blob for h in ("lkq", "szekop", "autokelly", "nextis", "wurth", "wuerth")
    ):
        return True
    return False


def infer_image_source(url: str, supplier_source: str = "") -> str:
    """Krátky kód pôvodu pre `_sg_image_source`."""
    host = host_of(url)
    src = (supplier_source or "").strip().lower()
    if "smartgaraz.sk" in host or "/wp-content/uploads/" in (url or "").lower():
        return "smartgaraz_media"
    if any(h in host for h in ("lkq.", "autokelly")) or src == "lkq":
        return "lkq_b2b"
    if any(h in host for h in ("nextis.", "szekop")) or src == "szekop":
        return "szekop_b2b"
    if is_wurth_media_url(url) or src in ("wurth", "würth", "wuerth"):
        return "wurth_b2b"
    if src in ("pro-tec", "protec", "metabond", "efautoparts"):
        return f"{src.replace('-', '')}_b2b" if src else "supplier_b2b"
    if src:
        return f"{re.sub(r'[^a-z0-9]+', '', src)}_b2b"
    if any(h in host for h in _B2B_IMAGE_HOST_HINTS):
        return "supplier_b2b"
    if host:
        return "web_fallback"
    return "unknown"


def filter_usable_supplier_images(
    urls: List[str],
    *,
    titles: Optional[List[str]] = None,
    supplier_source: str = "",
) -> List[str]:
    """Vyhodí watermarkované / Würth-bez-súhlasu / prázdne URL pred rehostom."""
    out: List[str] = []
    titles = titles or []
    src = (supplier_source or "").strip().lower()
    allow_wurth = wurth_media_allowed()
    for i, u in enumerate(urls or []):
        u = (u or "").strip()
        if not u:
            continue
        # Relatívne / placeholder fotky (LKQ EmptyImage_S.gif) — WC 400 pri uploade
        low = u.lower()
        if u.startswith("/") or "://" not in u:
            continue
        if "emptyimage" in low or low.endswith("emptyimage_s.gif"):
            continue
        title = titles[i] if i < len(titles) else ""
        if looks_like_supplier_watermark(u, title):
            continue
        if (is_wurth_media_url(u) or src in ("wurth", "würth", "wuerth")) and not allow_wurth:
            continue
        out.append(u)
    return out


def image_provenance_meta(
    *,
    original_url: str,
    supplier_source: str = "",
    fetched_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """WooCommerce ``meta_data`` riadky — pôvod fotky pre audit / C&D purge."""
    ts = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = infer_image_source(original_url, supplier_source)
    rows = [
        {"key": "_sg_image_source", "value": source},
        {"key": "_sg_image_original_url", "value": (original_url or "")[:500]},
        {"key": "_sg_image_fetched_at", "value": ts},
    ]
    if source == "wurth_b2b":
        rows.append(
            {
                "key": "_sg_image_rights_note",
                "value": "B2B customer media — require active Würth account + WURTH_MEDIA_OK",
            }
        )
    return rows


def merge_image_provenance_into_meta(
    meta: List[Dict[str, Any]],
    *,
    original_url: str,
    supplier_source: str = "",
) -> List[Dict[str, Any]]:
    """Nahradí / doplní provenienčné kľúče v existujúcom meta zozname."""
    keys = {
        "_sg_image_source",
        "_sg_image_original_url",
        "_sg_image_fetched_at",
        "_sg_image_rights_note",
    }
    kept = [m for m in (meta or []) if isinstance(m, dict) and m.get("key") not in keys]
    kept.extend(
        image_provenance_meta(original_url=original_url, supplier_source=supplier_source)
    )
    return kept
