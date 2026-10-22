#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oprava Würth produktov vo WooCommerce (idempotentné, šetrné k HQ).

Problém (2026-10-22): Würth cena je za BALENIE (napr. 11,90 € = 1 × 10 ks),
staré importy predávali „1 ks“ bez údaja o balení; popis mal šum
(„Certifikáty / …“, SAE blurb pri skrutke), tagy SKU/autodiely/Produkt/SMARTgaraz/Würth,
niektoré kategórie hádané (odvzdušňovacia skrutka → Brzdy).

Čo robí pre každý Würth produkt (_supplier_source=wurth):
  1. publish → draft (+ _sg_prev_status=publish, _sg_hold_reason) — kým nie je
     overené balenie (_sg_pack_verified=1) a pôvod (SK/DE).
  2. čistý slovenský popis (odstavce + odrážky, bez tabuliek), OE čísla →
     _sg_oe_numbers + sekcia v popise; SAE text len pri olejoch.
  3. tagy: zmaže SKU / autodiely / Produkt / SMARTgaraz / SmartGaráž / Würth;
     značka = len brand Würth.
  4. kategória: ak názov jasne patrí inam alebo je to „autodiel“ kategória bez
     zhody v názve → bez kategórie (koncept), _sg_prev_categories uložené.
  5. meta: _sg_pack_verified (0/1), _sg_pack_qty_hint, _sg_origin ("" = neznámy),
     _sg_wurth_fee_note (PZO 6,80 € pri objednávke do 100 € podľa VOP Würth).
  6. --refetch: skúsi Würth PDP (prihlásenie, pokojné tempo, žiadne obchádzanie);
     pri ochrannej stránke refetch vypne a pokračuje bez neho.

  python bot/repair_wurth_products.py --dry-run --limit 20
  python bot/repair_wurth_products.py            # všetko
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from suppliers.wurth_clean import (  # noqa: E402
    build_clean_html,
    clean_description_lines,
    name_pack_hint,
    pack_from_supplier_block,
    pack_title,
    parse_oe_numbers,
)

LOG = logging.getLogger("repair_wurth")
VERSION = "v1-2026-10-22"
JUNK_TAGS = {"autodiely", "produkt", "smartgaraz", "smartgaráž", "würth", "wurth", "smartgaraz.sk"}
# „Autodiel“ kategórie — Würth (dielenský sortiment) sem patrí len pri jasnej zhode v názve.
VEHICLE_PART_CATS = (
    "náhradné diely", "brzdy", "ložiská kolies", "brzdové hadice", "tesnenia motora",
    "teplotné & tlakové čidlá", "disky & pneumatiky", "chladenie", "podvozok", "výfuk",
    "odhlučnenie", "karoséria & svetlá",
)
FEE_NOTE = ("Würth VOP čl. 2.6/5.12: poplatok za spracovanie objednávky 6,80 € pri objednávke do 100 €; "
            "doprava DE vs SK podľa košíka Würth (neoverené).")


def wc_env():
    url = os.environ["WC_URL"].rstrip("/") + "/wp-json/wc/v3"
    auth = (os.environ["WC_CONSUMER_KEY"], os.environ["WC_CONSUMER_SECRET"])
    return url, auth


def req(method: str, path: str, **kw) -> requests.Response:
    from shop_guard import wait_if_slow
    url, auth = wc_env()
    last = None
    for wait in (0, 15, 45, 120):
        if wait:
            time.sleep(wait)
        wait_if_slow()
        try:
            r = requests.request(method, url + path, auth=auth, timeout=120, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            last = r
        except Exception as exc:  # noqa: BLE001
            LOG.warning("WC %s %s: %s", method, path, exc)
    if last is not None:
        return last
    raise RuntimeError(f"WC {method} {path} zlyhalo")


def meta_of(p: Dict[str, Any]) -> Dict[str, Any]:
    return {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}


def fetch_wurth(limit: int = 0) -> List[Dict[str, Any]]:
    out, page = [], 1
    fields = "id,name,sku,status,regular_price,description,short_description,categories,tags,brands,meta_data"
    while True:
        r = req("GET", "/products", params={"search": "Würth", "status": "any", "per_page": 50,
                                            "page": page, "_fields": fields})
        if not r.ok:
            LOG.error("WC list HTTP %s", r.status_code)
            break
        rows = r.json()
        for p in rows:
            m = meta_of(p)
            if m.get("_supplier_source") == "wurth" or "eshop.wurth.sk" in str(m.get("supplier_url") or ""):
                out.append(p)
        if len(rows) < 50 or (limit and len(out) >= limit):
            break
        page += 1
        time.sleep(0.5)
    return out[:limit] if limit else out


def wurth_brand_id() -> Optional[int]:
    try:
        r = req("GET", "/products/brands", params={"search": "Würth", "per_page": 20})
        for b in r.json() if r.ok else []:
            if str(b.get("slug")) in ("wurth", "wurth-2") or html.unescape(b.get("name", "")) == "Würth":
                return int(b["id"])
    except Exception:  # noqa: BLE001
        pass
    return None


def category_verdict(p: Dict[str, Any]) -> str:
    """'keep' | 'clear' (hádaná / zjavne zlá kategória)."""
    try:
        from taxonomy import match_keyword_category
    except Exception:  # noqa: BLE001
        return "keep"
    cur = [html.unescape(c.get("name") or "") for c in (p.get("categories") or [])]
    if not cur:
        return "keep"
    guess = match_keyword_category({"name": p.get("name") or "", "sku": "", "brand": ""})
    if guess and guess[-1] not in cur:
        return "clear"
    if not guess:
        blob = " | ".join(cur).lower()
        if any(k in blob for k in VEHICLE_PART_CATS):
            return "clear"
    return "keep"


class Refetcher:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.sup = None
        self.fails = 0

    def get(self, url: str):
        if not self.enabled or not url:
            return None
        try:
            if self.sup is None:
                from suppliers import get_supplier
                self.sup = get_supplier("wurth")
            raw = self.sup.parse_product(url)
            if raw is None:
                self.fails += 1
            else:
                self.fails = 0
            if self.fails >= 3:
                LOG.warning("Würth refetch: 3× bez dát (ochrana webu?) — refetch vypínam")
                self.enabled = False
            return raw
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Würth refetch %s: %s", url, exc)
            self.fails += 1
            if self.fails >= 3:
                self.enabled = False
            return None


def plan(p: Dict[str, Any], brand_id: Optional[int], ref: Refetcher, force: bool) -> Optional[Dict[str, Any]]:
    m = meta_of(p)
    if m.get("_sg_wurth_repair") == VERSION and not force:
        # už opravené; znova len ak vieme dotiahnuť balenie z Würth PDP
        if not (ref.enabled and str(m.get("_sg_pack_verified") or "") != "1"):
            return None
    name = html.unescape(p.get("name") or "")
    sku = (p.get("sku") or "").strip()
    ean = str(m.get("_ean") or "")
    upd: Dict[str, Any] = {"id": p["id"]}
    meta: List[Dict[str, Any]] = []
    stats: Dict[str, int] = {}

    raw = ref.get(str(m.get("supplier_url") or ""))
    extra = (getattr(raw, "raw_extra", None) or {}) if raw else {}
    pack = extra.get("pack_qty") if extra.get("pack_verified") else None
    if pack is None:
        pack = pack_from_supplier_block(p.get("description") or "", sku)
        pack_src = "wurth_desc_block" if pack else ""
    else:
        pack_src = "wurth_pdp"
    verified = bool(pack)
    origin = str(extra.get("origin") or "")

    # 1) publish → draft
    if p.get("status") == "publish" and not (verified and origin):
        upd["status"] = "draft"
        meta += [{"key": "_sg_prev_status", "value": "publish"},
                 {"key": "_sg_hold_reason", "value": "Würth: overiť balenie (ks) a pôvod SK/DE pred predajom"}]
        stats["unpublished"] = 1

    # 2) popis + OE
    oe = parse_oe_numbers(p.get("description") or "") or list(extra.get("oe_numbers") or [])
    src_desc = (raw.description if raw and raw.description else p.get("description") or "")
    lines = clean_description_lines(src_desc, name)
    new_name = pack_title(name, pack) if verified else name
    body = build_clean_html(new_name, lines, sku=sku, ean=ean, pack_qty=pack if verified else None, oe_numbers=oe)
    upd["description"] = body["description"]
    upd["short_description"] = body["short_description"]
    if new_name != name:
        upd["name"] = new_name
        stats["renamed"] = 1
    if oe:
        meta.append({"key": "_sg_oe_numbers", "value": "|".join(oe)})
        stats["oe"] = 1
    focus = re.sub(r"^Würth\s+", "", re.sub(r"\s*\(.*?\)\s*$", "", new_name)).strip()
    meta += [{"key": "rank_math_focus_keyword", "value": focus},
             {"key": "rank_math_description", "value": re.sub(r"<[^>]+>", "", body["short_description"])[:155]},
             {"key": "_ai_search_terms", "value": ", ".join(x for x in (new_name, focus, sku, ean) if x)}]

    # 3) tagy + značka
    tags = p.get("tags") or []
    keep = [{"id": t["id"]} for t in tags
            if html.unescape(t.get("name") or "").strip().lower() not in JUNK_TAGS
            and html.unescape(t.get("name") or "").strip() != sku]
    if len(keep) != len(tags):
        upd["tags"] = keep
        stats["tags_cleaned"] = 1
    if brand_id and [b.get("id") for b in (p.get("brands") or [])] != [brand_id]:
        upd["brands"] = [{"id": brand_id}]
        stats["brand_fixed"] = 1

    # 4) kategória
    if category_verdict(p) == "clear":
        meta += [{"key": "_sg_prev_categories", "value": json.dumps([c.get("id") for c in p.get("categories") or []])},
                 {"key": "_sg_cat_review", "value": "1"}]
        upd["categories"] = []
        if p.get("status") == "publish":
            upd["status"] = "draft"
        stats["category_cleared"] = 1

    # 5) balenie / pôvod / cena
    hint = name_pack_hint(name)
    meta += [{"key": "_sg_pack_verified", "value": "1" if verified else "0"},
             {"key": "_sg_pack_source", "value": pack_src},
             {"key": "_sg_origin", "value": origin},
             {"key": "_sg_origin_text", "value": str(extra.get("origin_text") or "")},
             {"key": "_sg_wurth_fee_note", "value": FEE_NOTE},
             {"key": "_sg_wurth_repair", "value": VERSION}]
    if hint:
        meta.append({"key": "_sg_pack_qty_hint", "value": str(hint)})
    if verified:
        meta.append({"key": "_sg_pack_qty", "value": str(pack)})
        stats["pack_verified"] = 1
        try:
            cost = float(str(extra.get("pack_price") or m.get("_cost_price") or "").replace(",", "."))
            meta += [{"key": "_sg_pack_cost", "value": f"{cost:.4f}"},
                     {"key": "_sg_unit_cost", "value": f"{cost / pack:.4f}"}]
        except (TypeError, ValueError):
            pass
    if raw and extra.get("pack_price"):
        try:
            from pricing import calculate_retail
            cost = float(extra["pack_price"])
            meta.append({"key": "_cost_price", "value": f"{cost:.2f}"})
            res = calculate_retail(cost, supplier="wurth Würth", name=new_name, brand="Würth", sku=sku)
            upd["regular_price"] = f"{res.retail_price:.2f}"
            stats["repriced"] = 1
        except Exception as exc:  # noqa: BLE001
            LOG.warning("reprice %s: %s", sku, exc)
    upd["meta_data"] = meta
    upd["_stats"] = stats
    return upd


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="Skús Würth PDP (balenie/pôvod/cena)")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--ids", default="", help="Len tieto WC ID (čiarka)")
    a = ap.parse_args()

    if a.ids:
        prods = []
        for i in [x.strip() for x in a.ids.split(",") if x.strip()]:
            r = req("GET", f"/products/{i}")
            if r.ok:
                prods.append(r.json())
    else:
        prods = fetch_wurth(a.limit)
    LOG.info("Würth produktov: %d (publish=%d)", len(prods), sum(p.get("status") == "publish" for p in prods))
    brand_id = wurth_brand_id()
    ref = Refetcher(a.refetch)
    totals: Dict[str, int] = {"products": len(prods), "updated": 0, "errors": 0}
    pending: List[Dict[str, Any]] = []

    def flush():
        if not pending:
            return
        body = [{k: v for k, v in u.items() if k != "_stats"} for u in pending]
        if a.dry_run:
            for u in body[:3]:
                LOG.info("DRY %s", json.dumps({k: (v if k != "description" else v[:300]) for k, v in u.items()},
                                              ensure_ascii=False)[:1500])
        else:
            r = req("POST", "/products/batch", json={"update": body})
            if not r.ok:
                LOG.error("batch HTTP %s: %s", r.status_code, r.text[:300])
                totals["errors"] += len(body)
            else:
                res = r.json().get("update") or []
                bad = [x for x in res if x.get("error")]
                totals["errors"] += len(bad)
                for x in bad[:3]:
                    LOG.warning("batch item error: %s", x.get("error"))
            time.sleep(1.0)
        for u in pending:
            totals["updated"] += 1
            for k, v in u["_stats"].items():
                totals[k] = totals.get(k, 0) + v
        pending.clear()

    for p in prods:
        u = plan(p, brand_id, ref, a.force)
        if u:
            pending.append(u)
        if len(pending) >= a.batch:
            flush()
    flush()
    print("SG_RESULT " + json.dumps({"ok": True, "dry_run": a.dry_run, **totals}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
