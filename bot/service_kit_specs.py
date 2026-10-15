#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extrakcia špecifikácií motorového oleja (viskozita, VW/Toyota approvals).

Ukladá sa do `_sg_oil_*` meta — Lavro IP, žiadne kType.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_VISC_RE = re.compile(
    r"\b(0W-?16|0W-?20|0W-?30|0W-?40|5W-?20|5W-?30|5W-?40|10W-?40|10W-?60|15W-?40)\b",
    re.I,
)
_VW_RE = re.compile(
    r"\b(?:VW|Audi)?\s*(50[24]\.?00|507\.?00|508\.?00|509\.?00|502\.?00|505\.?00|505\.?01)\b",
    re.I,
)
_TOYOTA_HINT = re.compile(
    r"\b(toyota|lexus|hybrid|0W-?16|0W-?20|API\s*SP|ILSAC\s*GF-?6)\b",
    re.I,
)
_PACK_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*[lL]\b")


def _norm_visc(raw: str) -> str:
    s = raw.upper().replace(" ", "")
    s = re.sub(r"(\d)W(\d)", r"\1W-\2", s)
    return s


def _norm_vw(raw: str) -> str:
    s = re.sub(r"[^0-9]", "", raw)
    if len(s) == 5:
        return f"{s[:3]}.{s[3:]}"
    if len(s) == 3:
        return f"{s}.00"
    return raw.strip()


def extract_oil_specs(product: Dict[str, Any]) -> Dict[str, Any]:
    blob_parts = [
        product.get("name"),
        product.get("title"),
        product.get("description"),
        product.get("sku"),
        product.get("brand"),
    ]
    attrs = product.get("attributes") or product.get("specs") or {}
    if isinstance(attrs, dict):
        blob_parts.extend(str(v) for v in attrs.values())
    elif isinstance(attrs, list):
        for a in attrs:
            if isinstance(a, dict):
                blob_parts.append(str(a.get("name") or ""))
                blob_parts.append(str(a.get("value") or a.get("options") or ""))
    blob = " ".join(str(p or "") for p in blob_parts)

    out: Dict[str, Any] = {}
    vm = _VISC_RE.search(blob)
    if vm:
        out["viscosity"] = _norm_visc(vm.group(1))
    approvals: List[str] = []
    for m in _VW_RE.finditer(blob):
        a = _norm_vw(m.group(1))
        if a and a not in approvals:
            approvals.append(a)
    if approvals:
        out["approvals"] = approvals
    if _TOYOTA_HINT.search(blob) and out.get("viscosity") in ("0W-16", "0W-20"):
        out["platform"] = "toyota_hybrid"
    if any(x.startswith("50") for x in approvals):
        out["platform"] = out.get("platform") or "vag"
    pm = _PACK_RE.search(blob)
    if pm:
        try:
            out["pack_l"] = float(pm.group(1).replace(",", "."))
        except ValueError:
            pass
    return out


def oil_specs_meta(product: Dict[str, Any]) -> List[Dict[str, str]]:
    specs = extract_oil_specs(product)
    rows: List[Dict[str, str]] = []
    if specs.get("viscosity"):
        rows.append({"key": "_sg_oil_viscosity", "value": str(specs["viscosity"])})
    if specs.get("approvals"):
        rows.append(
            {
                "key": "_sg_oil_approvals",
                "value": "|".join(str(a) for a in specs["approvals"]),
            }
        )
    if specs.get("platform"):
        rows.append({"key": "_sg_oil_platform", "value": str(specs["platform"])})
    if specs.get("pack_l") is not None:
        rows.append({"key": "_sg_oil_pack_l", "value": str(specs["pack_l"])})
    return rows


def oil_matches_platform(
    product_or_name: Any,
    *,
    platform: str,
) -> bool:
    """True ak olej sedí na VAG 507/509 alebo Toyota hybrid 0W-16/0W-20."""
    if isinstance(product_or_name, dict):
        specs = extract_oil_specs(product_or_name)
        name = str(product_or_name.get("name") or "")
    else:
        specs = extract_oil_specs({"name": str(product_or_name or "")})
        name = str(product_or_name or "")
    plat = (platform or "").lower()
    visc = str(specs.get("viscosity") or "")
    approvals = [str(a) for a in (specs.get("approvals") or [])]
    if plat in ("vag", "vw", "skoda", "škoda", "octavia", "audi"):
        if any(a in ("507.00", "509.00", "504.00", "508.00") for a in approvals):
            return True
        if visc in ("5W-30", "0W-30") and re.search(
            r"507|509|504|508|esp|long.?life|ll-?04", name, re.I
        ):
            return True
        return False
    if plat in ("toyota", "toyota_hybrid", "hybrid"):
        return visc in ("0W-16", "0W-20")
    return bool(visc)
