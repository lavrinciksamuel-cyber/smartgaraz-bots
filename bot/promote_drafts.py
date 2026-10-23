#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zverejni koncepty, ktoré spĺňajú Samuelove pravidlá kvality; ostatné ostávajú koncept.

Pravidlá (všetky musia platiť):
  * konkrétna kategória (nie „Auto chémia“ dump / Nezaradené / len „Náhradné diely“ / [orphan])
    a sedí s typom dielu (taxonomy.match_keyword_category, ak typ vieme určiť);
  * hlavný autodiel má OE číslo (oe_publish_rule; drobné komponenty, chémia, náradie výnimka);
  * cena > 0 a nie pod nákupnou cenou (ceny sa NEMENIA);
  * slovenský názov — CZ názvy sa najprv preložia existujúcou logikou (sk_part_names);
    ak po preklade ostanú české slová, produkt ostáva koncept;
  * stav skladu nastavený (Skladom v SR / Sklad v EÚ / Nedostupné = stock_status);
  * Würth len s overeným balením + pôvodom (rovnaké pravidlo ako publish_ready_drafts);
  * TSS ostáva koncept (nový dodávateľ, ceny sa ešte overujú); ručné zadržanie (_sg_manual_hold) sa rešpektuje.
Fotky NIE sú podmienka (fáza 2).

  python bot/promote_drafts.py --dry-run
  python bot/promote_drafts.py --batch 100
  python bot/promote_drafts.py --from-file drafts.jsonl --dry-run   # offline vyhodnotenie
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = logging.getLogger("promote_drafts")

FIELDS = "id,name,sku,status,price,regular_price,categories,stock_status,meta_data"
# Dump / fallback kategórie — produkt v nich = bez kategórie.
GENERIC_CATS = {"auto chémia", "nezaradené", "uncategorized", "náhradné diely", "ostatné", "iné"}
VALID_STOCK = {"instock", "outofstock", "onbackorder"}
# České znaky / slová, ktoré v slovenčine nie sú (po preklade sk_part_names).
CZ_CHARS = re.compile(r"[řůěŘŮĚ]")
CZ_WORDS = re.compile(
    r"\b(filtr|sada\s+filtr\w*|kotouč\w*|tlumič\w*|čidlo|snímač\s+otáček|světl\w*|převod\w*|"
    r"vzduchový\s+filtr|olejový\s+filtr|palivový\s+filtr|brzdové\s+destičky|destičk\w*|"
    r"těsnění|ložisko\s+kola|zapalovací|svíčk\w*|žhavicí|chladič\s+motoru|motoru|"
    r"vodní|čerpadlo|spojka\s+kompletní|lambda\s+sonda\s+zadní|řemen\w*|klínov\w*|"
    r"částic|sazov\w*|kola\b|přední|zadní|levý|pravý|levé|pravé|horní|dolní)\b",
    re.I,
)
BULK_RE = re.compile(r"\b(20|25|55|58|60|200|205|208|1000)\s*l\b", re.I)  # sudy / IBC — nepublikovať
OIL_VISC = re.compile(r"^(\d+W-\d+|Ostatné viskozity|Motorové oleje)$")
JUNK = re.compile(
    r"^(szekop\s+)?(filtre|1\.9\s*tdi|octavia\s+ii)\b|placeholder|lorem|test product|untitled|"
    r"originální\s+díl\s+\w+\s+náhradný\s+diel|originálny\s+náhradný\s+diel",
    re.I,
)


def _meta(p: dict) -> dict:
    return {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}


def _f(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def sk_name(name: str) -> str:
    """Existujúci CZ→SK preklad názvov (rovnaký ako pri importe)."""
    try:
        from ai_engine import normalize_sk_product_name

        return normalize_sk_product_name(name)
    except Exception:
        try:
            from sk_part_names import normalize_part_name

            return normalize_part_name(name)
        except Exception:
            return name


def is_czech(name: str) -> bool:
    return bool(CZ_CHARS.search(name or "") or CZ_WORDS.search(name or ""))


def evaluate(p: dict) -> tuple[list[str], str | None]:
    """→ (dôvody prečo nie, nový_názov_ak_treba_preložiť)."""
    meta = _meta(p)
    name = (p.get("name") or "").strip()
    sku = (p.get("sku") or "").strip()
    src = str(meta.get("_supplier_source") or meta.get("_sg_supplier") or "").lower()
    reasons: list[str] = []

    if str(meta.get("_sg_manual_hold") or "").strip() not in ("", "0"):
        return ["manual_hold"], None
    if src.startswith("tss") or sku.upper().startswith("TSS-"):
        return ["tss_hold"], None

    # cena
    price = _f(p.get("regular_price")) or _f(p.get("price")) or 0.0
    cost = _f(meta.get("_cost_price"))
    if price <= 0:
        reasons.append("no_price")
    elif cost and cost > 0 and price + 0.009 < cost:
        reasons.append("below_cost")
    elif cost and cost > 0 and price > 0:
        from pricing import bad_cost_reason
        if bad_cost_reason(cost, price, supplier=src, name=name, sku=sku) == "cost_outlier":
            reasons.append("cost_outlier")

    # kategória
    cats = [(c.get("name") or "").replace("&amp;", "&").strip() for c in (p.get("categories") or [])]
    specific = [c for c in cats if c.lower() not in GENERIC_CATS and not c.lower().startswith("[orphan]")]
    if not specific:
        reasons.append("no_category")
    else:
        try:
            from taxonomy import match_keyword_category

            raw = {"name": name, "sku": sku, "brand": "", "description": ""}
            expected = None if src == "ideo" else match_keyword_category(raw)
            ok = not expected or any(
                (node in cats) or any(node in c or c in node for c in cats) for node in expected
            )
            # Oleje: iná viskozita/„Ostatné viskozity“ v rámci Motorových olejov = OK.
            if not ok and expected[0].startswith("Motorové oleje") and any(OIL_VISC.match(c) for c in cats):
                ok = True
            # Batérie: taxonómia ich hádže k štartérom, „Autobatérie“ je správne.
            if not ok and "Autobatérie" in cats and re.search(r"batéri|akumul", name, re.I):
                ok = True
            if not ok:
                reasons.append("wrong_category")
        except Exception:
            pass

    # OE číslo pre hlavné diely
    try:
        from oe_publish_rule import has_oe_numbers, needs_oe

        if needs_oe(name, meta) and not has_oe_numbers(meta, sku):
            reasons.append("no_oe")
    except Exception:
        pass

    # názov
    new_name = None
    if BULK_RE.search(name):
        reasons.append("bulk_pack")
    if len(name) < 6 or JUNK.search(name):
        reasons.append("bad_name")
    elif is_czech(name):
        fixed = sk_name(name)
        if fixed and fixed != name and not is_czech(fixed):
            new_name = fixed
        else:
            reasons.append("czech_name")

    # sklad
    if str(p.get("stock_status") or "").lower() not in VALID_STOCK:
        reasons.append("no_stock_status")

    # Würth balenie / pôvod (cena je za balenie)
    if src == "wurth" and not (str(meta.get("_sg_pack_verified") or "") == "1" and meta.get("_sg_origin")):
        reasons.append("wurth_pack_unverified")

    if not sku:
        reasons.append("no_sku")
    return reasons, new_name


def _wc():
    url = (os.environ.get("WC_URL") or "").rstrip("/")
    ck = os.environ.get("WC_CONSUMER_KEY") or ""
    cs = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not (url and ck and cs):
        raise SystemExit("Chýba WC_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET")
    return url, (ck, cs)


def iter_drafts(url, auth, session, sleep: float):
    from shop_guard import wait_if_slow

    page = 1
    while True:
        wait_if_slow()
        t = time.monotonic()
        r = None
        for attempt in range(4):
            try:
                r = session.get(
                    f"{url}/wp-json/wc/v3/products",
                    auth=auth,
                    params={"status": "draft", "per_page": 100, "page": page, "orderby": "id", "order": "asc", "_fields": FIELDS},
                    timeout=120,
                )
                if r.ok:
                    break
            except Exception as exc:
                LOG.warning("GET page %d: %s", page, exc)
            time.sleep(30 * (attempt + 1))
        if r is None or not r.ok:
            LOG.error("Stránka %d sa nenačítala — koniec listovania", page)
            return
        batch = r.json() or []
        dt = time.monotonic() - t
        for p in batch:
            yield p
        if len(batch) < 100:
            return
        page += 1
        time.sleep(sleep if dt < 8 else 30)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--max", type=int, default=0, help="max zverejnených za beh (0 = bez limitu)")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--from-file", default="", help="JSONL konceptov (offline, bez zápisu)")
    ap.add_argument("--out", default="", help="zapíš JSON so zoznamom ready id + dôvodmi")
    a = ap.parse_args()

    import requests

    session = requests.Session()
    if a.from_file:
        a.dry_run = True
        src_iter = (json.loads(l) for l in open(a.from_file, encoding="utf-8") if l.strip())
        url = auth = None
    else:
        url, auth = _wc()
        src_iter = iter_drafts(url, auth, session, a.sleep)

    from shop_guard import wait_if_slow

    seen = set()
    reasons = Counter()
    primary = Counter()
    by_src = Counter()
    ready: list[dict] = []
    held: dict = {}
    published = failed = renamed = 0

    def flush(chunk):
        nonlocal published, failed
        if not chunk or a.dry_run:
            return
        wait_if_slow()
        t = time.monotonic()
        try:
            r = session.post(f"{url}/wp-json/wc/v3/products/batch", auth=auth, json={"update": chunk}, timeout=180)
            upd = (r.json() or {}).get("update") or [] if r.ok else []
        except Exception as exc:
            LOG.warning("batch výnimka: %s", exc)
            upd = []
        ok = sum(1 for u in upd if (u or {}).get("status") == "publish")
        published += ok
        failed += len(chunk) - ok
        dt = time.monotonic() - t
        LOG.info("batch %d → publish %d (%.1fs), spolu %d", len(chunk), ok, dt, published)
        time.sleep(30 if dt > 8 else a.sleep)

    pending: list[dict] = []
    total = 0
    for p in src_iter:
        pid = int(p.get("id") or 0)
        if pid in seen or (p.get("status") or "draft") != "draft":
            continue
        seen.add(pid)
        total += 1
        if total % 2000 == 0:
            LOG.info("prejdených %d konceptov, zatiaľ ready %d", total, len(pending))
        rs, new_name = evaluate(p)
        meta = _meta(p)
        src = str(meta.get("_supplier_source") or meta.get("_sg_supplier") or "?").lower()
        if rs:
            for r_ in rs:
                reasons[r_] += 1
            key = rs[0]
            primary[key] += 1
            held.setdefault(key, [])
            if len(held[key]) < 15:
                held[key].append(f"#{pid} {(p.get('name') or '')[:70]}")
            continue
        by_src[src] += 1
        upd = {"id": pid, "status": "publish"}
        if new_name:
            upd["name"] = new_name
            renamed += 1
        ready.append({"id": pid, "name": new_name or p.get("name"), "src": src})
        pending.append(upd)
        if a.max and len(pending) >= a.max:
            break
    # Zverejňuj až po prejdení všetkých konceptov — inak by sa pri stránkovaní
    # (status=draft) posúvali stránky a časť konceptov by sa preskočila.
    LOG.info("prejdených %d konceptov, ready %d — zverejňujem po %d", total, len(pending), a.batch)
    for i in range(0, len(pending), a.batch):
        flush(pending[i : i + a.batch])

    summary = {
        "drafts_scanned": total,
        "ready": len(ready),
        "published": published if not a.dry_run else 0,
        "failed": failed,
        "renamed_cz_to_sk": renamed,
        "ready_by_supplier": dict(by_src),
        "held_reasons": dict(reasons.most_common()),
        "held_primary": dict(primary.most_common()),
        "held_examples": held,
        "dry_run": a.dry_run,
    }
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "ready": ready}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SG_RESULT " + json.dumps({k: v for k, v in summary.items() if k != "held_examples"}, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
