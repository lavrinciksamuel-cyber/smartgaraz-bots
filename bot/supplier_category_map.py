#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crawl needles (LKQ / Szekop) → náš SmartGaráž taxonomy path.

Dodávateľský názov skupiny = len seed pre Heijunka URL.
Woo kategória = vždy náš strom (`taxonomy.CATEGORY_TREE` + `PART_LEAVES`).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict, List, Optional

_DATA = os.path.join(os.path.dirname(__file__), "data", "supplier_category_map.json")


@lru_cache(maxsize=1)
def _load() -> Dict:
    with open(_DATA, encoding="utf-8") as fh:
        return json.load(fh)


def lkq_needles() -> Dict[str, List[str]]:
    return dict(_load().get("lkq_needles") or {})


def resolve_lkq_needle(needle: str) -> Optional[List[str]]:
    """Najlepší match: exact → substring (dlhší needle vyhrá)."""
    n = (needle or "").strip().lower()
    if not n:
        return None
    mapping = lkq_needles()
    if n in mapping:
        return list(mapping[n])
    hits = [(k, v) for k, v in mapping.items() if k in n or n in k]
    if not hits:
        return None
    hits.sort(key=lambda kv: len(kv[0]), reverse=True)
    return list(hits[0][1])
