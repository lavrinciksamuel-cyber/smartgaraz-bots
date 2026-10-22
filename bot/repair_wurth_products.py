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
    build_description_v3,
    known_pack_qty,
    sale_title,
    sale_unit_lines,
    translations_sk,
    build_clean_html,
    clean_description_lines,
    name_pack_hint,
    pack_from_supplier_block,
    pack_title,
    parse_oe_numbers,
)

LOG = logging.getLogger("repair_wurth")
LOSS: List[Dict[str, Any]] = []
try:
    DESC_V3: Dict[str, Any] = json.loads((ROOT / "data" / "wurth_descriptions_v3.json").read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    DESC_V3 = {}
VERSION = "v3-2026-10-22"
# Samuel 2026-10-22: tieto SKU ostávajú publikované, predaj po 1 ks (cena sa nemení).
KEEP_PUBLISHED_SKUS = {"0500300002"}
# Samuel 2026-10-22: 0500300002 predávame po 1 ks za 16,90 € — cenu nemeniť.
KEEP_PRICE_SKUS = {"0500300002"}
# Ručne určené kategórie (OE BMW 1711/1712 = chladiaci systém).
CATEGORY_OVERRIDES = {"0500300002": 882}
# Ručne overené zlé kategórie (2026-10-22): rýchlospojka prívesu v „Tesnenia motora“,
# čistiaci prostriedok v „Tesnenia & Lepidlá“, montážna pasta v „Prevodové oleje“.
# Automatický odhad je len hlásenie (_sg_cat_review=1) — nehádame, nesťahujeme.
WRONG_CATEGORY_SKUS = {"088532", "5861900006", "0893850"}  # Náhradné diely › Chladenie & kúrenie
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
    fields = "id,name,sku,status,regular_price,sale_price,description,short_description,categories,tags,brands,meta_data"
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


REFETCH: Dict[str, int] = {"ok": 0, "fail": 0, "pack": 0}


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
                try:  # rovnaké loginy ako import shard (HQ → env)
                    from suppliers.hq_credentials import export_logins
                    export_logins("wurth")
                except Exception:  # noqa: BLE001
                    pass
                os.environ.setdefault("WURTH_B2B", "1")
                os.environ.setdefault("WURTH_CHALLENGE_PAUSE", "0")
                from suppliers import get_supplier
                self.sup = get_supplier("wurth")
            raw = self.sup.parse_product(url)
            if raw is None:
                self.fails += 1
                REFETCH["fail"] += 1
            else:
                self.fails = 0
                REFETCH["ok"] += 1
                ex = getattr(raw, "raw_extra", {}) or {}
                if ex.get("pack_qty"):
                    REFETCH["pack"] += 1
                if ex.get("origin"):
                    REFETCH["origin_" + str(ex["origin"])] = REFETCH.get("origin_" + str(ex["origin"]), 0) + 1
            if raw is None and getattr(self.sup, "_blocked", False) and self.sup.probe_blocked():
                LOG.warning("Würth refetch: web vracia challenge/403 — refetch hneď vypínam")
                REFETCH["blocked"] = 1
                self.enabled = False
            elif self.fails >= 3:
                LOG.warning("Würth refetch: 3× bez dát (ochrana webu?) — refetch vypínam")
                self.enabled = False
            return raw
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Würth refetch %s: %s", url, exc)
            self.fails += 1
            if self.fails >= 3:
                self.enabled = False
            return None


PRICE_LOG: List[Dict[str, Any]] = []


def price_floor_update(p: Dict[str, Any], m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Würth floor: (netto + 6,80 € poplatok) × 1,23 DPH + min. zisk, ,90. Len NAHOR.

    Bez falošných akcií: sale_price sa nenastavuje (a ak by bola pod floor, zmaže sa),
    takže „Najnižšia cena za 30 dní“ ostáva pravdivá.
    """
    from pricing import wurth_price_floor
    sku = (p.get("sku") or "").strip()
    if sku in KEEP_PRICE_SKUS:
        return None
    try:
        cost = float(str(m.get("_cost_price") or "").replace(",", "."))
        cur = float(p.get("regular_price") or 0)
    except (TypeError, ValueError):
        return None
    if cost <= 0 or cur <= 0:
        return None
    sale = 0.0
    try:
        sale = float(p.get("sale_price") or 0)
    except (TypeError, ValueError):
        sale = 0.0
    floor = wurth_price_floor(cost)
    eff = sale if 0 < sale < cur else cur
    if eff + 0.004 >= floor:
        return None
    new = max(floor, cur)
    PRICE_LOG.append({"id": p["id"], "sku": sku, "before": eff, "after": new, "net_cost": cost,
                      "status": p.get("status")})
    upd: Dict[str, Any] = {"id": p["id"], "regular_price": f"{new:.2f}", "meta_data": [
        {"key": "_sg_price_before_floor", "value": f"{eff:.2f}"},
        {"key": "_sg_price_rule", "value": "wurth_floor: (netto + 6,80) × 1,23 + min. zisk"},
    ]}
    if sale:
        upd["sale_price"] = ""
    return upd


def plan(p: Dict[str, Any], brand_id: Optional[int], ref: Refetcher, force: bool, cat_ids: Dict[str, int]) -> Optional[Dict[str, Any]]:
    m = meta_of(p)
    if m.get("_sg_wurth_repair") == VERSION and not force:
        # už opravené; znova len ak vieme dotiahnuť balenie z Würth PDP
        if not (ref.enabled and str(m.get("_sg_pack_verified") or "") != "1"):
            pu = price_floor_update(p, m)
            if pu:
                pu["_stats"] = {"repriced_floor": 1}
            return pu
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
        if not pack:
            pack = known_pack_qty(sku)
            pack_src = "wurth_accessory_table" if pack else ""
        if not pack and str(m.get("_sg_pack_verified") or "") == "1" and m.get("_sg_pack_qty"):
            try:
                pack = int(m["_sg_pack_qty"])
                pack_src = str(m.get("_sg_pack_source") or "stored")
            except (TypeError, ValueError):
                pack = None
    else:
        pack_src = "wurth_pdp"
    verified = bool(pack)
    origin = str(extra.get("origin") or "")

    # 1) v1 omylom stiahol publikované kvôli baleniu → vráť (Samuel: kvôli baleniu nesťahovať)
    if (p.get("status") == "draft" and m.get("_sg_prev_status") == "publish"
            and "balenie" in str(m.get("_sg_hold_reason") or "")):
        upd["status"] = "publish"
        meta += [{"key": "_sg_prev_status", "value": ""}, {"key": "_sg_hold_reason", "value": ""}]
        stats["republished"] = 1

    # 2) popis + OE
    oe = parse_oe_numbers(p.get("description") or "") or list(extra.get("oe_numbers") or [])
    src_desc = (raw.description if raw and raw.description else p.get("description") or "")
    lines = clean_description_lines(src_desc, name)
    per_piece = sku in KEEP_PRICE_SKUS
    v3 = DESC_V3.get(sku)
    if raw and raw.description:
        body = {"description": raw.description, "short_description": raw.short_description or ""}
        new_name = sale_title(raw.name, pack, per_piece)
    elif v3:
        body = {"description": v3["description"], "short_description": v3["short_description"]}
        new_name = sale_title(v3["title"], pack, per_piece)
        stats["desc_v3"] = 1
    else:
        body = build_description_v3(name, src_desc, sku=sku, ean=ean, supplier_url=str(m.get("supplier_url") or ""),
                                    pack_qty=pack, per_piece=per_piece, oe_numbers=oe, translations=translations_sk())
        new_name = sale_title(body["title"], pack, per_piece)
    # balenie v popise musí sedieť s aktuálnym stavom (v3 dáta mohli vzniknúť bez balenia)
    if pack and not per_piece and "overujeme" in body["description"]:
        sale_old = "".join(f"<li>{x}</li>" for x in sale_unit_lines(None))
        sale_new = "".join(f"<li>{x}</li>" for x in sale_unit_lines(pack))
        body["description"] = body["description"].replace(sale_old, sale_new)
        body["short_description"] = body["short_description"].replace(
            "Cena za celé originálne balenie Würth.",
            f"Minimálne balenie {pack} ks, cena za celé balenie." if pack > 1 else "Cena za 1 ks.")
    if new_name != name:
        meta.append({"key": "_sg_name_before", "value": name})
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

    if sku in KEEP_PUBLISHED_SKUS:
        upd["status"] = "publish"
        meta += [{"key": "_sg_prev_status", "value": ""}, {"key": "_sg_hold_reason", "value": ""},
                 {"key": "_sg_cat_review", "value": ""}]
        stats.pop("republished", None)
        if p.get("status") != "publish":
            stats["republished"] = 1

    # 4) kategória
    ov = CATEGORY_OVERRIDES.get(sku)
    if ov:
        want = [{"id": ov}]
        if [c.get("id") for c in p.get("categories") or []] != [ov]:
            meta.append({"key": "_sg_prev_categories", "value": json.dumps([c.get("id") for c in p.get("categories") or []])})
            upd["categories"] = want
            stats["category_fixed"] = 1
    elif sku not in KEEP_PUBLISHED_SKUS and category_verdict(p) == "clear" and sku not in WRONG_CATEGORY_SKUS:
        meta.append({"key": "_sg_cat_review", "value": "1"})
        stats["category_review_flag"] = 1
    elif sku in WRONG_CATEGORY_SKUS:
        meta += [{"key": "_sg_prev_categories", "value": json.dumps([c.get("id") for c in p.get("categories") or []])},
                 {"key": "_sg_cat_review", "value": "1"}]
        upd["categories"] = []
        if p.get("status") == "publish" or upd.get("status") == "publish":
            upd["status"] = "draft"
            meta += [{"key": "_sg_prev_status", "value": "publish"},
                     {"key": "_sg_hold_reason", "value": "Würth: nejasná/zlá kategória — triedenie"}]
            stats["unpublished_wrong_category"] = 1
            stats.pop("republished", None)
        stats["category_cleared"] = 1

    # 5) balenie / pôvod / cena
    hint = name_pack_hint(name)
    verified = bool(pack)
    meta.append({"key": "_sg_sale_unit", "value": "piece" if per_piece else ("pack" if pack else "wurth_pack_unknown")})
    if verified:
        stats["pack_known"] = 1
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
    if False and raw and extra.get("pack_price"):  # predajný model čaká na Samuela
        try:
            from pricing import calculate_retail
            cost = float(extra["pack_price"])
            meta.append({"key": "_cost_price", "value": f"{cost:.2f}"})
            res = calculate_retail(cost, supplier="wurth Würth", name=new_name, brand="Würth", sku=sku)
            upd["regular_price"] = f"{res.retail_price:.2f}"
            stats["repriced"] = 1
        except Exception as exc:  # noqa: BLE001
            LOG.warning("reprice %s: %s", sku, exc)
    try:
        retail = float(p.get("regular_price") or 0)
        cost = float(str(m.get("_cost_price") or "").replace(",", "."))
        q = pack
        if q and q > 1 and retail > 0 and cost > 0:
            unit_cost = cost / q
            meta.append({"key": "_sg_unit_cost", "value": f"{unit_cost:.4f}"})
            # Ak by sme predávali po 1 ks za túto cenu, zarábame? (cena WC je teraz za 1 „kus“ WC)
        if (p.get("status") == "publish" or upd.get("status") == "publish") and retail > 0 and cost > 0:
            # strata pri predaji 1 ks, ak WC cena ≈ cena za kus, ale Würth účtuje celé balenie
            if q and q > 1 and retail < cost:
                stats["loss_per_piece"] = 1
                LOSS.append({"id": p["id"], "sku": sku, "name": name, "retail": retail, "pack_cost": cost, "pack": q})
    except (TypeError, ValueError):
        pass
    pu = price_floor_update(p, m)
    if pu and "regular_price" not in upd:
        upd["regular_price"] = pu["regular_price"]
        if "sale_price" in pu:
            upd["sale_price"] = ""
        meta += pu["meta_data"]
        stats["repriced_floor"] = 1
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
    cat_ids: Dict[str, int] = {}
    try:
        r = req("GET", "/products/categories", params={"search": "Chladenie", "per_page": 20})
        for c in (r.json() if r.ok else []):
            cat_ids.setdefault(html.unescape(c.get("name") or ""), int(c["id"]))
    except Exception:  # noqa: BLE001
        pass
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

    from account_split import filter_products as _acct_filter
    prods = _acct_filter(prods)
    LOG.info("account split → %d Würth produktov pre tento účet", len(prods))
    for p in prods:
        u = plan(p, brand_id, ref, a.force, cat_ids)
        if u:
            pending.append(u)
        if len(pending) >= a.batch:
            flush()
    flush()
    if PRICE_LOG:
        import statistics
        incs = [round(x["after"] - x["before"], 2) for x in PRICE_LOG]
        print("SG_PRICE_SUMMARY " + json.dumps({"repriced": len(PRICE_LOG), "min": min(incs),
              "median": statistics.median(incs), "max": max(incs)}, ensure_ascii=False), flush=True)
        for x in PRICE_LOG[:5]:
            print("SG_PRICE " + json.dumps(x, ensure_ascii=False), flush=True)
    if a.refetch:
        print("SG_REFETCH " + json.dumps({**REFETCH, "logged_in": bool(getattr(ref.sup, "_logged_in", False))}), flush=True)
    for x in LOSS:
        print("SG_LOSS " + json.dumps(x, ensure_ascii=False), flush=True)
    print("SG_RESULT " + json.dumps({"ok": True, "dry_run": a.dry_run, **totals}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
