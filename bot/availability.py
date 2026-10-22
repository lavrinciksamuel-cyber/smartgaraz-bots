#!/usr/bin/env python3
"""Dostupnosť / pôvod skladu pre každý produkt (Samuel 22.10.2026).

Statusy (meta `_sg_avail`):
  sr       „Skladom v SR“  — 2–3 pracovné dni
  eu       „Sklad v EÚ“    — 5–10 pracovných dní
  na       „Nedostupné“    — stock_status=outofstock (nedá sa objednať, na koniec výpisov)
  unknown  neutrálny („Na objednávku“) — pôvod reálne nevieme, nehádame

Ako zisťujeme pôvod:
  LKQ (lkq.sk B2B, ProductList API): StorageStockAmount > 0 (+ IsStorageStock, IsOrderable)
      = kusy na sklade LKQ Slovensko → sr. Bez zásoby, ale s dodacou lehotou
      „bežne do 48h/96h/5 dní“ = dodávka z centrálneho skladu / dodávateľa mimo SR → eu.
      Bez zásoby a bez lehoty („na tel. otázku“) alebo IsOrderable=false → na.
  Szekop (szekopsk.nextis.cz, `.flex-stocks`): sklad „BA“ (Bratislava) > 0 → sr;
      BA = 0 a „Distribučný sklad“ > 0 → eu; oba 0 → na.
  Würth (eshop.wurth.sk PDP, `_sg_origin` z textu „Dodávané z …“): SK → sr, DE → eu;
      Würth nedostupné → na; bez údaja → unknown.

Backfill existujúcich produktov:  python bot/availability.py backfill [--dry-run] [--szekop-refetch N]
Importy: ai_engine._apply_pricing_and_stock volá classify_raw() → meta + stock_status.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import zlib
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("availability")

LABELS = {
    "sr": ("Skladom v SR", "2–3 pracovné dni"),
    "eu": ("Sklad v EÚ", "5–10 pracovných dní"),
    "na": ("Nedostupné", ""),
    "unknown": ("Na objednávku", "5–10 pracovných dní"),
}

_LEAD_RE = re.compile(r"bežne\s+do\s+\d+\s*(?:h|hod|dn|dní|dni|pracovn)", re.I)
_NO_LEAD_RE = re.compile(r"na\s+tel|nedostupn|vypredan|nedodáva|ukončen", re.I)


def _f(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _result(avail: str, src: str) -> Dict[str, str]:
    return {"avail": avail, "src": src[:190]}


# ---------------------------------------------------------------- supplier data
def classify_lkq_item(item: Dict[str, Any]) -> Dict[str, str]:
    qty = _f(item.get("StorageStockAmount"))
    orderable = bool(item.get("IsOrderable", True)) and not item.get("IsUserOrderForbidden")
    disp = item.get("Disponibility") or {}
    text = str(disp.get("Text") if isinstance(disp, dict) else disp or "").strip()
    if not orderable:
        return _result("na", "LKQ: neobjednateľné")
    if qty is not None and qty > 0:
        return _result("sr", f"LKQ sklad SK: {qty:g} ks")
    if _LEAD_RE.search(text):
        return _result("eu", f"LKQ: bez zásoby v SR, dodávka {text}")
    if qty is not None and qty <= 0:
        return _result("na", f"LKQ: 0 ks, {text or 'bez dodacej lehoty'}")
    return _result("unknown", f"LKQ: {text or 'bez údaja o sklade'}")


def classify_szekop(ba: Optional[float], dist: Optional[float], text: str = "") -> Dict[str, str]:
    if ba is None and dist is None:
        return _result("unknown", f"Szekop: {text or 'bez údaja o sklade'}")
    if (ba or 0) > 0:
        return _result("sr", f"Szekop sklad BA: {ba:g} ks")
    if (dist or 0) > 0:
        return _result("eu", f"Szekop distribučný sklad: {dist:g} ks (BA 0)")
    return _result("na", "Szekop: BA 0 ks, distribučný 0 ks")


def classify_wurth(origin: str, stock_status: str = "instock") -> Dict[str, str]:
    if str(stock_status).lower() == "outofstock":
        return _result("na", "Würth: nedostupné")
    o = str(origin or "").upper()
    if o == "SK":
        return _result("sr", "Würth: dodávané zo Slovenska")
    if o == "DE":
        return _result("eu", "Würth: dodávané z Nemecka")
    return _result("unknown", "Würth: pôvod neuvedený")


def parse_szekop_stocks(soup) -> Tuple[Optional[float], Optional[float], str]:
    """`.flex-stocks` → (BA ks, distribučný ks, text)."""
    block = soup.select_one(".flex-stocks") if soup is not None else None
    if not block:
        return None, None, ""

    def qty(sel: str) -> Optional[float]:
        el = block.select_one(sel + " .flex-items-count")
        if not el:
            return None
        m = re.search(r"(\d+(?:[.,]\d+)?)", el.get_text(" ", strip=True))
        return float(m.group(1).replace(",", ".")) if m else None

    return qty(".flex-main-stock"), qty(".flex-distribution-stock"), block.get_text(" ", strip=True)[:120]


# ---------------------------------------------------------------- import path
def classify_raw(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """ProductRaw dict (import) → status, alebo None (iný dodávateľ)."""
    src = str(raw.get("source") or "").lower()
    extra = raw.get("raw_extra") if isinstance(raw.get("raw_extra"), dict) else {}
    if src == "lkq":
        item = extra.get("lkq_item")
        if isinstance(item, dict) and item:
            return classify_lkq_item(item)
        qty = _f(raw.get("stock_qty"))
        return classify_lkq_item({"StorageStockAmount": qty, "Disponibility": {"Text": raw.get("delivery_text")},
                                  "IsOrderable": str(raw.get("stock_status")) != "outofstock" or qty is None})
    if src == "szekop":
        if "stock_ba" in extra or "stock_dist" in extra:
            return classify_szekop(_f(extra.get("stock_ba")), _f(extra.get("stock_dist")), str(extra.get("stock_text") or ""))
        return _result("unknown", "Szekop: bez rozpisu skladov")
    if src == "ideo":
        st = str(raw.get("stock_status") or "")
        if st == "instock":
            return _result("sr", "ideo Bratislava: skladom (2–3 prac. dni)")
        if st == "onbackorder":
            return _result("eu", "ideo: na objednávku")
        return _result("na", "ideo: nedostupné")
    if src in ("wurth", "würth"):
        return classify_wurth(str(extra.get("origin") or ""), str(raw.get("stock_status") or "instock"))
    return None


def apply_to_payload(payload: Dict[str, Any], raw: Dict[str, Any]) -> Optional[str]:
    """Doplní meta + stock_status do WC payloadu (import full aj fast path)."""
    av = classify_raw(raw)
    if not av:
        return None
    meta = payload.setdefault("meta_data", [])
    meta.extend(meta_rows(av))
    stock_payload(payload, av["avail"])
    return av["avail"]


def meta_rows(av: Dict[str, str]) -> List[Dict[str, str]]:
    return [
        {"key": "_sg_avail", "value": av["avail"]},
        {"key": "_sg_avail_src", "value": av["src"]},
        {"key": "_sg_avail_checked", "value": time.strftime("%Y-%m-%d %H:%M")},
    ]


def stock_payload(payload: Dict[str, Any], avail: str, current: Optional[Dict[str, Any]] = None) -> None:
    if avail == "na":
        payload["stock_status"] = "outofstock"
        payload["manage_stock"] = False
        payload.pop("stock_quantity", None)
        payload.setdefault("meta_data", []).append({"key": "_sg_avail_oos", "value": "1"})
    elif avail == "eu":
        # EÚ sklad: v SR 0 ks, ale dodávateľ dodá → objednateľné (WC by pri qty 0 dal outofstock)
        payload["manage_stock"] = False
        payload.pop("stock_quantity", None)
        payload["stock_status"] = "instock"
        payload.setdefault("meta_data", []).append({"key": "_sg_avail_oos", "value": ""})
    elif avail == "sr":
        if current is not None and current.get("stock_status") == "outofstock" and not current.get("manage_stock"):
            payload["stock_status"] = "instock"
        payload.setdefault("meta_data", []).append({"key": "_sg_avail_oos", "value": ""})


# ---------------------------------------------------------------- backfill
def meta_of(p: Dict[str, Any]) -> Dict[str, Any]:
    return {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}


def classify_existing(p: Dict[str, Any], m: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Existujúci WC produkt (bez nového fetchu u dodávateľa)."""
    src = str(m.get("_supplier_source") or m.get("_sg_supplier") or "").lower()
    if src.startswith("wurth") or src.startswith("würth"):
        st = p.get("stock_status")
        if st == "outofstock" and m.get("_sg_avail_oos") == "1":
            st = "instock"  # outofstock sme dali my, nie Würth
        return classify_wurth(str(m.get("_sg_origin") or ""), st)
    qty = p.get("stock_quantity") if p.get("manage_stock") else None
    if qty is None:
        qty = _f(m.get("_supplier_stock"))
    text = str(m.get("_delivery_text") or "")
    if src == "lkq":
        # Nedostupné, ktoré sme dali my (stock/qty sme pritom vynulovali) — drž, kým
        # import nedonesie novší údaj od LKQ (_last_b2b_sync po poslednej klasifikácii).
        fresher = str(m.get("_last_b2b_sync") or "")[:16] > str(m.get("_sg_avail_checked") or "")[:16]
        if m.get("_sg_avail") == "na" and m.get("_sg_avail_oos") == "1" and not fresher:
            return {"avail": "na", "src": str(m.get("_sg_avail_src") or "LKQ: nedostupné")}
        # Oprava behu 22.10. (39826277965): nedostupné omylom → unknown s prázdnym _sg_avail_oos
        if (m.get("_sg_avail") == "unknown" and "_sg_avail_oos" in m and m.get("_sg_avail_oos") == ""
                and not fresher and qty in (None, 0)):
            return _result("na", f"LKQ: 0 ks, {text or 'bez dodacej lehoty'}")
        if p.get("stock_status") == "outofstock" and qty in (None, 0) and m.get("_sg_avail_oos") != "1":
            return classify_lkq_item({"StorageStockAmount": 0, "Disponibility": {"Text": text}})
        return classify_lkq_item({"StorageStockAmount": qty, "Disponibility": {"Text": text}})
    if src == "szekop":
        if m.get("_sg_avail") in ("sr", "eu", "na") and m.get("_sg_avail_src", "").startswith("Szekop"):
            return {"avail": m["_sg_avail"], "src": m.get("_sg_avail_src", "")}
        if (qty is not None and qty <= 0) or (p.get("stock_status") == "outofstock" and m.get("_sg_avail_oos") != "1"):
            return _result("na", "Szekop: 0 ks (BA aj distribučný)")
        return _result("unknown", "Szekop: rozpis BA/distribučný zatiaľ neoverený")
    return None


def _szekop_refetch(url: str, http) -> Optional[Dict[str, str]]:
    from bs4 import BeautifulSoup
    try:
        r = http.get(url)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("szekop fetch %s: %s", url, exc)
        return None
    if getattr(r, "status_code", 200) != 200:
        return None
    ba, dist, text = parse_szekop_stocks(BeautifulSoup(r.text, "html.parser"))
    if ba is None and dist is None:
        return None
    return classify_szekop(ba, dist, text)


def account_split() -> Tuple[int, int]:
    """(index, count) z SG_ACCOUNT_INDEX / SG_ACCOUNT_COUNT (2. repo na Lavro2); default (0, 1)."""
    try:
        n = int(os.environ.get("SG_ACCOUNT_COUNT") or 1)
        i = int(os.environ.get("SG_ACCOUNT_INDEX") or 0)
    except ValueError:
        return 0, 1
    if n < 1 or not 0 <= i < n:
        return 0, 1
    return i, n


def mine(p: Dict[str, Any], idx: int, n: int) -> bool:
    """crc32(SKU) % SG_ACCOUNT_COUNT == SG_ACCOUNT_INDEX (bez SKU → ID), rovnako ako catalog_shard.shard_of."""
    if n <= 1:
        return True
    key = str(p.get("sku") or "").strip() or str(p.get("id"))
    return zlib.crc32(key.encode("utf-8")) % n == idx


def backfill(args) -> int:
    from repair_wurth_products import req  # WC REST + shop_guard (HQ load guard)
    acc_i, acc_n = account_split()
    if acc_n > 1:
        LOG.info("Účet %d/%d: spracúvam len crc32(SKU) %% %d == %d", acc_i, acc_n, acc_n, acc_i)
    counts: Dict[str, Counter] = defaultdict(Counter)
    examples: Dict[str, Dict[str, str]] = defaultdict(dict)
    pending: List[Dict[str, Any]] = []
    changed = refetched = 0
    szekop_http = None
    if args.szekop_refetch:
        from suppliers import get_supplier
        szekop_http = get_supplier("szekop").http

    def flush():
        nonlocal pending
        if not pending or args.dry_run:
            pending = []
            return
        r = req("POST", "/products/batch", json={"update": pending})
        if r.status_code >= 300:
            LOG.error("batch HTTP %s: %s", r.status_code, r.text[:300])
        else:
            for x in (r.json().get("update") or []):
                if x.get("error"):
                    LOG.warning("batch item %s: %s", x.get("id"), x.get("error"))
        pending = []
        time.sleep(args.sleep)

    page = 1
    while True:
        r = req("GET", "/products", params={"status": "any", "per_page": 100, "page": page, "orderby": "id", "order": "asc",
                                            "_fields": "id,sku,status,permalink,stock_status,manage_stock,stock_quantity,meta_data"})
        if r.status_code != 200:
            LOG.error("list HTTP %s page %s", r.status_code, page)
            break
        rows = r.json()
        if not rows:
            break
        for p in rows:
            if not mine(p, acc_i, acc_n):
                continue
            m = meta_of(p)
            sup = str(m.get("_supplier_source") or m.get("_sg_supplier") or "?").lower()
            sup = "wurth" if sup.startswith("wurth") or sup.startswith("würth") else sup
            av = classify_existing(p, m)
            if (av and av["avail"] == "unknown" and sup == "szekop" and szekop_http is not None
                    and (p.get("status") == "publish" or args.refetch_drafts)
                    and refetched < args.szekop_refetch and "nextis.cz" in str(m.get("supplier_url") or "")
                    and re.search(r"/\d{5,}/?$", str(m.get("supplier_url") or ""))):
                refetched += 1
                got = _szekop_refetch(m["supplier_url"], szekop_http)
                time.sleep(args.szekop_delay)
                if got:
                    av = got
            if not av:
                counts[sup]["(iný dodávateľ)"] += 1
                continue
            counts[sup][av["avail"]] += 1
            if p.get("status") == "publish" and av["avail"] not in examples[sup]:
                examples[sup][av["avail"]] = p.get("permalink", "")
            if m.get("_sg_avail") == av["avail"] and m.get("_sg_avail_src") == av["src"] and not args.force:
                continue
            body: Dict[str, Any] = {"id": p["id"], "meta_data": meta_rows(av)}
            stock_payload(body, av["avail"], p)
            if av["avail"] == "sr" and m.get("_sg_avail_oos") == "1" and p.get("stock_status") == "outofstock":
                body["stock_status"] = "instock"
            if av["avail"] in ("unknown",) and m.get("_sg_avail_oos") == "1":
                body["stock_status"] = "instock"
                body["meta_data"].append({"key": "_sg_avail_oos", "value": ""})
            pending.append(body)
            changed += 1
            if len(pending) >= args.batch:
                flush()
        LOG.info("page %d: zmien %d, szekop refetch %d, %s", page, changed, refetched,
                 {k: dict(v) for k, v in counts.items()})
        if args.limit and page * 100 >= args.limit:
            break
        page += 1
    flush()
    report = {"account": f"{acc_i}/{acc_n}", "changed": changed, "szekop_refetched": refetched,
              "counts": {k: dict(v) for k, v in counts.items()}, "examples": examples, "dry_run": args.dry_run}
    print("REPORT " + json.dumps(report, ensure_ascii=False))
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write("## Dostupnosť (SR / EÚ / nedostupné)\n\n| Dodávateľ | sr | eu | na | unknown |\n|---|---|---|---|---|\n")
            for k, v in sorted(counts.items()):
                fh.write(f"| {k} | {v.get('sr',0)} | {v.get('eu',0)} | {v.get('na',0)} | {v.get('unknown',0)} |\n")
            fh.write(f"\nZmenených: {changed}, Szekop refetch: {refetched}\n")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--force", action="store_true")
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--batch", type=int, default=50)
    b.add_argument("--sleep", type=float, default=2.0)
    b.add_argument("--szekop-refetch", type=int, default=0, help="max. Szekop PDP na overenie BA/distribučný")
    b.add_argument("--szekop-delay", type=float, default=1.5)
    b.add_argument("--refetch-drafts", action="store_true", help="Szekop refetch aj pre koncepty (default len publikované)")
    a = ap.parse_args()
    if a.cmd == "backfill":
        return backfill(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
