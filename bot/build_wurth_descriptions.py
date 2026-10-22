#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vygeneruj čisté SK popisy Würth (v3) zo snapshotu pôvodných WC dát.

Vstup: JSON dump WooCommerce produktov (pôvodné popisy z importu, pred repair v2)
  python bot/build_wurth_descriptions.py /cesta/wurth_dump.json

Výstup (commitnuté, aplikuje repair_wurth_products.py cez GitHub Actions):
  bot/data/wurth_pack_qty.json      — SKU → ks v balení (z Würth tabuliek „Počet / Balenie / Cena za balenie“)
  bot/data/wurth_descriptions_v3.json — SKU → {title, description, short_description}
Preklady anglických viet: bot/data/wurth_translations_sk.json (ručne overené).
"""
from __future__ import annotations

import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from suppliers.wurth_clean import build_description_v3, parse_oe_numbers  # noqa: E402

PER_PIECE_SKUS = {"0500300002"}  # Samuel: predaj po kusoch


def meta(p, k):
    for x in p.get("meta_data") or []:
        if x.get("key") == k:
            return x.get("value")
    return None


def pack_map(products) -> dict:
    """Würth tabuľka príslušenstva: „Obj. č. 0891 152 212 … x 50 ks“ → {SKU: 50}."""
    found = defaultdict(set)
    for p in products:
        t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ",
                   (p.get("description") or "") + " " + (p.get("short_description") or ""))))
        for m in re.finditer(r"Obj\.?\s*č\.?\s*([\d ]{6,16}?)\s+(?:(?!Obj\.).){0,220}?\bx\s*(\d{1,6})\s*ks\b", t):
            found[m.group(1).replace(" ", "")].add(int(m.group(2)))
    return {k: sorted(v)[0] for k, v in found.items() if len(v) == 1}


def main() -> int:
    src = json.load(open(sys.argv[1], encoding="utf-8"))
    tr = json.loads((ROOT / "data" / "wurth_translations_sk.json").read_text(encoding="utf-8"))
    packs = pack_map(src)
    out = {}
    for p in src:
        if meta(p, "_supplier_source") != "wurth":
            continue
        sku = (p.get("sku") or "").strip()
        pk = packs.get(sku)
        r = build_description_v3(
            html.unescape(p.get("name") or ""), p.get("description") or "", sku=sku,
            ean=str(meta(p, "_ean") or ""), supplier_url=str(meta(p, "supplier_url") or ""),
            pack_qty=pk, per_piece=sku in PER_PIECE_SKUS,
            oe_numbers=parse_oe_numbers(p.get("description") or ""), translations=tr,
        )
        out[sku] = {"title": r["title"], "description": r["description"],
                    "short_description": r["short_description"], "has_text": r["has_text"]}
    (ROOT / "data" / "wurth_pack_qty.json").write_text(
        json.dumps(dict(sorted(packs.items())), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    (ROOT / "data" / "wurth_descriptions_v3.json").write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"packs={len(packs)} descriptions={len(out)} ours_with_pack={sum(1 for s in out if s in packs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
