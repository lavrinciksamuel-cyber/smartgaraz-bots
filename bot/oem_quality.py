#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OEM / OEM-kvalita gate pre záruku (Lavro s. r. o. — nie TecDoc).

Autá v záruke: len OE (výrobca) alebo OES/OEM-kvalita (Mann, Bosch, Mahle…).
Aftermarket no-name (DACO, …) sa pri ``under_warranty`` / ``warranty`` vynechá.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Sequence

# Výrobca / oficálne značky (OE)
OEM_BRANDS = {
    "mopar",
    "toyota",
    "genuine",
    "original",
    "originální",
    "originalni",
    "vw",
    "volkswagen",
    "škoda",
    "skoda",
    "audi",
    "seat",
    "cupra",
    "bmw",
    "mercedes",
    "motul factory",
}

# OES / prémiová kvalita (bežne OE dodávatelia)
OEM_QUALITY_BRANDS = {
    "mann",
    "mann-filter",
    "mann filter",
    "bosch",
    "mahle",
    "knecht",
    "filtron",
    "purflux",
    "valeo",
    "fram",
    "wix",
    "hengst",
    "sogefi",
    "purflux",
    "castrol",
    "mobil",
    "mobil 1",
    "shell",
    "total",
    "elf",
    "motul",
    "ravenol",
    "liqui moly",
    "liquimoly",
    "fuchs",
    "pentosin",
    "mpm",  # schválené špecifikácie VW/Toyota v katalógu
    "febi",
    "febi bilstein",
    "meyle",
    "vaico",  # AT/DSG filtre — bežný OES dodávateľ (Batch 5)
}

# Explicit aftermarket / low-tier — blok pri záruke
AFTERMARKET_BLOCKLIST = {
    "daco",
    "daco germany",
    "jp group",
    "maxgear",
    "stark",
    "ridex",
}


def _blob(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts).lower()


def quality_tier(brand: str = "", name: str = "", sku: str = "") -> str:
    """Vráť ``oem`` | ``oem_quality`` | ``aftermarket``."""
    b = _blob(brand, name, sku)
    for needle in OEM_BRANDS:
        if needle in b:
            # „Originální díl RENAULT“ atď.
            if needle in ("original", "originální", "originalni", "genuine", "toyota", "mopar", "vw", "škoda", "skoda"):
                return "oem"
            if any(x in b for x in ("genuine", "original", "originál", "oem ")):
                return "oem"
    for needle in sorted(OEM_QUALITY_BRANDS, key=len, reverse=True):
        if needle in b:
            return "oem_quality"
    for needle in AFTERMARKET_BLOCKLIST:
        if needle in b:
            return "aftermarket"
    # default: unknown → treat as aftermarket under warranty
    return "aftermarket"


def warranty_quality_ok(
    brand: str = "",
    name: str = "",
    sku: str = "",
    *,
    under_warranty: bool = False,
    quality_meta: str = "",
) -> bool:
    """Pri záruke len oem / oem_quality."""
    if not under_warranty:
        return True
    tier = (quality_meta or "").strip().lower() or quality_tier(brand, name, sku)
    return tier in ("oem", "oem_quality")


def infer_under_warranty(
    *,
    flag: Any = None,
    year: Any = None,
    reference_year: int = 2026,
    warranty_years: int = 3,
) -> bool:
    """Explicit flag alebo rok výroby v záruke (default 3 roky)."""
    if flag is True or str(flag or "").strip().lower() in ("1", "true", "yes", "ano", "áno"):
        return True
    if flag is False or str(flag or "").strip().lower() in ("0", "false", "no", "nie"):
        return False
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False
    if y < 1990 or y > reference_year + 1:
        return False
    return (reference_year - y) < int(warranty_years)


def quality_meta_rows(product: Dict[str, Any]) -> list:
    """Meta ``_sg_quality_tier`` (+ voliteľne ``_sg_image_source``) pre Woo."""
    brand = str(product.get("brand") or product.get("manufacturer") or "")
    name = str(product.get("name") or product.get("title") or "")
    sku = str(product.get("sku") or "")
    tier = quality_tier(brand, name, sku)
    rows = [{"key": "_sg_quality_tier", "value": tier}]
    img_src = str(
        product.get("image_source")
        or product.get("_sg_image_source")
        or product.get("image_src")
        or ""
    ).strip()
    if not img_src:
        # Heuristika: supplier feed vs manuál vs OE pack
        src = str(product.get("supplier_source") or product.get("_supplier_source") or "").lower()
        if src:
            img_src = f"supplier:{src}"
        elif product.get("image_url") or product.get("images"):
            img_src = "catalog"
    if img_src:
        rows.append({"key": "_sg_image_source", "value": img_src[:64]})
    return rows


def real_care_meta_rows(
    *,
    under_warranty: bool = False,
    service_mode: str = "",
) -> list:
    """Parametre pre Real-Care / Warranty Guard na produkte (voliteľné)."""
    mode = (service_mode or "").strip().lower().replace("-", "_")
    rows = []
    if mode in ("real_care", "fleet", "severe"):
        rows.append({"key": "_sg_service_mode", "value": "real_care"})
    if under_warranty:
        rows.append({"key": "_sg_warranty_guard", "value": "1"})
    return rows


def prefer_oem_sorted(rows: Sequence[Dict[str, Any]]) -> list:
    """Zoradiť kandidátov: oem → oem_quality → ostatné."""
    rank = {"oem": 0, "oem_quality": 1, "aftermarket": 2}

    def key(r: Dict[str, Any]) -> tuple:
        t = quality_tier(
            str(r.get("brand") or ""),
            str(r.get("name") or ""),
            str(r.get("sku") or ""),
        )
        return (rank.get(t, 9), str(r.get("name") or ""))

    return sorted(list(rows), key=key)
