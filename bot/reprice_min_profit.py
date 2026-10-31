#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Predaj pod nákupom s DPH × 1,20 zdvihni na calculate_retail. Stav nemení.

Pro-Tec bez `_sg_protec_parser=pcode-v1` sa nepreceňuje — uložený cost môže
byť z banneru P1241. Tie dorovná `protec-price-sync` po oprave parsera.

  python bot/reprice_min_profit.py            # náhľad
  python bot/reprice_min_profit.py --apply    # zápis, dávky, shop_guard
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pricing import (  # noqa: E402
    bad_cost_reason,
    calculate_retail,
    cost_with_vat_for,
    is_jlm_source,
    is_lkq_source,
    is_metabond_source,
    is_mopar_source,
    is_protec_source,
    is_wurth_source,
    _f,
)
from suppliers.protec import PARSER_VERSION  # noqa: E402

# Samuel 2026-10-22: 0500300002 predávame po 1 ks — cenu nemeniť.
KEEP_PRICE_SKUS = {"0500300002"}


def money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def meta_of(product: Dict[str, Any]) -> Dict[str, Any]:
    return {m.get("key"): m.get("value") for m in (product.get("meta_data") or []) if isinstance(m, dict)}


def effective_price(product: Dict[str, Any]) -> Optional[float]:
    sale = money(product.get("sale_price"))
    active = money(product.get("price"))
    regular = money(product.get("regular_price"))
    if sale is not None and sale > 0:
        return sale
    if active is not None and active > 0:
        return active
    if regular is not None and regular > 0:
        return regular
    return None


def supplier_bucket(supplier: str, name: str = "", brand: str = "") -> str:
    if is_wurth_source(supplier, brand, name):
        return "wurth"
    if is_lkq_source(supplier, brand, name):
        return "lkq"
    if is_protec_source(supplier, brand, name):
        return "protec"
    if is_metabond_source(supplier, brand, name):
        return "metabond"
    if is_jlm_source(supplier, brand, name):
        return "jlm"
    if is_mopar_source(supplier, brand, name):
        return "mopar"
    blob = (supplier or "").lower()
    if "ideo" in blob.split() or blob == "ideo":
        return "ideo"
    if blob == "tss" or blob.startswith("tss"):
        return "tss"
    if "szekop" in blob:
        return "szekop"
    return (supplier or "unknown").strip().lower() or "unknown"


@dataclass
class Decision:
    action: str
    reason: str
    supplier: str
    sku: str
    product_id: Any = None
    cost: Optional[float] = None
    price: Optional[float] = None
    new_price: Optional[float] = None
    update: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def decide(product: Dict[str, Any]) -> Decision:
    meta = meta_of(product)
    sku = str(product.get("sku") or "").strip()
    supplier = str(meta.get("_supplier_source") or meta.get("_sg_supplier") or "")
    name = str(product.get("name") or "")
    bucket = supplier_bucket(supplier, name)
    cost = money(meta.get("_cost_price"))
    price = effective_price(product)
    regular = money(product.get("regular_price"))
    sale = money(product.get("sale_price"))
    base = Decision("skip", "ok", bucket, sku, product.get("id"), cost, price)

    if sku in KEEP_PRICE_SKUS:
        base.action, base.reason = "keep", "keep_price"
        return base

    bad = bad_cost_reason(cost, price, supplier=supplier, name=name, sku=sku)
    if bad:
        base.action, base.reason = "flag", bad
        return base

    # Uložený Pro-Tec cost môže byť banner P1241. Najprv živý sync s novým parserom.
    if is_protec_source(supplier, name=name) and str(meta.get("_sg_protec_parser") or "") != PARSER_VERSION:
        base.action, base.reason = "skip", "protec_needs_resync"
        return base

    if cost is None or cost <= 0:
        base.action, base.reason = "flag", "missing_cost"
        return base

    gross = cost_with_vat_for(cost, supplier, name)
    threshold = gross * _f("MIN_PROFIT_FACTOR", 1.20)
    if price is None or gross <= 0 or price + 1e-9 >= threshold:
        return base

    hint = None
    if is_lkq_source(supplier) or is_protec_source(supplier) or is_metabond_source(supplier) or is_jlm_source(supplier):
        hint = regular if regular and regular > 0 else price
    result = calculate_retail(
        cost,
        supplier=supplier,
        name=name,
        sku=sku,
        retail_hint=hint,
        cost_has_vat=True,
        vat_rate=0.0,
    )
    if price + 1e-9 >= result.retail_price:
        return base
    update: Dict[str, Any] = {
        "id": product.get("id"),
        "regular_price": f"{result.retail_price:.2f}",
    }
    if sale and sale > 0 and sale + 1e-9 < result.retail_price:
        update["sale_price"] = ""
    base.action = "reprice"
    base.reason = "below_min_profit"
    base.new_price = result.retail_price
    base.update = update
    return base


def summarize(decisions: List[Decision]) -> Dict[str, Any]:
    repriced: Counter = Counter()
    flagged = []
    waiting = Counter()
    for d in decisions:
        if d.action == "reprice":
            repriced[d.supplier] += 1
        elif d.action == "flag":
            flagged.append({
                "sku": d.sku,
                "id": d.product_id,
                "supplier": d.supplier,
                "reason": d.reason,
                "cost": d.cost,
                "price": d.price,
            })
        elif d.reason == "protec_needs_resync":
            waiting[d.supplier] += 1
    return {
        "repriced_by_supplier": dict(repriced),
        "repriced_total": sum(repriced.values()),
        "flagged": flagged,
        "protec_waiting_resync": waiting.get("protec", 0),
    }


def _wc():
    url = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY") or ""
    secret = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not (url and key and secret):
        return None
    return url, (key, secret)


def _call(fn, what: str, waits=(0, 30, 90)):
    """HTTP volanie s opakovaním pri prerušenom spojení (ChunkedEncodingError, RemoteDisconnected,
    timeout) a 5xx/429. None = ani posledný pokus neprešiel (volajúci pokračuje ďalej)."""
    import requests
    last = None
    for n, wait in enumerate(waits, 1):
        if wait:
            time.sleep(wait)
        try:
            r = fn()
            if r.status_code in (429, 500, 502, 503, 504, 508) and n < len(waits):
                print(f"[reprice] {what}: HTTP {r.status_code}, pokus {n}/{len(waits)}", flush=True)
                last = r
                continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as exc:
            print(f"[reprice] {what}: {type(exc).__name__}, pokus {n}/{len(waits)}", flush=True)
    return last


def main() -> int:
    logging_setup()
    ap = argparse.ArgumentParser(description="Zdvihni ceny pod 20 % z nákupu s DPH")
    ap.add_argument("--apply", action="store_true", help="Zapíš do WooCommerce (inak len náhľad)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=10)
    args = ap.parse_args()

    creds = _wc()
    if not creds:
        print("SG_RESULT " + json.dumps({
            "ok": False,
            "msg": "Chýba WC_URL / kľúče — živý prepočet sa nespustil, ceny ostávajú.",
            "applied": False,
        }, ensure_ascii=False))
        return 2

    import requests
    from account_split import mine
    from shop_guard import wait_if_slow

    url, auth = creds
    session = requests.Session()
    decisions: List[Decision] = []
    page = 1
    seen = 0
    page_errors = 0
    failed_batches = 0
    while True:
        wait_if_slow()
        r = _call(lambda: session.get(
            f"{url}/wp-json/wc/v3/products",
            auth=auth,
            params={
                "status": "any", "per_page": 100, "page": page,
                "_fields": "id,sku,name,status,price,regular_price,sale_price,meta_data",
            },
            timeout=120,
        ), f"WC page {page}")
        if r is None:
            # Stránka ani po 3 pokusoch neprišla → preskoč ju (týždenný beh ju dorobí), nepadni.
            page_errors += 1
            if page_errors >= 5:
                print("[reprice] 5 stránok zlyhalo — končím čítanie, zapíšem, čo mám", flush=True)
                break
            page += 1
            continue
        if not r.ok:
            print(f"[reprice] WC page {page} HTTP {r.status_code}", flush=True)
            break
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            break
        for product in rows:
            sku = str(product.get("sku") or product.get("id") or "")
            if not mine(sku):
                continue
            decisions.append(decide(product))
            seen += 1
            if args.limit and seen >= args.limit:
                break
        if args.limit and seen >= args.limit or len(rows) < 100:
            break
        page += 1

    summary = summarize(decisions)
    updates = [d.update for d in decisions if d.action == "reprice" and d.update]
    written = 0
    if args.apply:
        for i in range(0, len(updates), max(1, args.batch)):
            chunk = updates[i:i + args.batch]
            wait_if_slow()
            resp = _call(lambda: session.post(
                f"{url}/wp-json/wc/v3/products/batch",
                auth=auth, json={"update": chunk}, timeout=180,
            ), f"batch {i // max(1, args.batch) + 1}")
            if resp is not None and resp.ok:
                try:
                    written += len((resp.json() or {}).get("update") or [])
                except ValueError:
                    written += len(chunk)
            else:
                # Spojenie spadlo / HTTP chyba: zápis je idempotentný (rovnaká cena) → ďalší beh to dorovná.
                failed_batches += 1
                print(f"[reprice] batch zlyhal ({resp.status_code if resp is not None else 'spojenie'})", flush=True)
            time.sleep(2)
    summary["applied"] = bool(args.apply)
    summary["written"] = written
    summary["failed_batches"] = failed_batches
    summary["page_errors"] = page_errors
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("SG_RESULT " + json.dumps({
        "ok": True,
        "msg": f"Pod 20 %: {summary['repriced_total']} na úpravu, zapísaných {written}. "
               f"Pro-Tec čaká na sync: {summary['protec_waiting_resync']}. "
               f"Podozrivý náklad: {len(summary['flagged'])}. "
               f"Zlyhané dávky: {failed_batches}, preskočené stránky: {page_errors}.",
        **{k: summary[k] for k in ("repriced_by_supplier", "repriced_total", "protec_waiting_resync")},
        "flagged_count": len(summary["flagged"]),
    }, ensure_ascii=False))
    return 0


def logging_setup() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")


if __name__ == "__main__":
    sys.exit(main())
