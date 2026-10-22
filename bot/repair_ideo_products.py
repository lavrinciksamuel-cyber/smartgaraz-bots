#!/usr/bin/env python3
"""Oprava ideo konceptov: čistý názov, kategória z ideo mapy (bez „Auto chémia“),
supplier_url s #ART (variant). Len koncepty; publikované sa nemenia.

  python bot/repair_ideo_products.py [--dry-run]
"""
from __future__ import annotations
import argparse, json, logging, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LOG = logging.getLogger("repair_ideo")


def ideo_skus() -> list:
    import requests
    tok = os.environ["HQ_SYNC_TOKEN"]
    r = requests.get("https://hq.smartgaraz.sk/api/sync/existing-skus", params={}, timeout=60,
                     headers={"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip", "User-Agent": "smartgaraz-bot"})
    r.raise_for_status()
    return sorted(s for s in r.json().get("skus", []) if s.startswith("IDEO-"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args()
    import requests
    from suppliers import get_supplier
    from ai_engine import Config, WooCommerceClient
    from taxonomy import sanitize_category_path
    from shop_guard import wait_if_slow
    wc_url = os.environ["WC_URL"].rstrip("/")
    auth = (os.environ["WC_CONSUMER_KEY"], os.environ["WC_CONSUMER_SECRET"])
    wc = WooCommerceClient(Config())
    sup = get_supplier("ideo")
    if not sup.ensure_login():
        print("SG_RESULT " + json.dumps({"ok": False, "msg": "ideo login zlyhal"})); return 1
    skus = set(ideo_skus())
    # HQ mirror môže zaostávať → doplň IDEO- SKU priamo z WC (search).
    for page in range(1, 20):
        wait_if_slow()
        r = requests.get(f"{wc_url}/wp-json/wc/v3/products", auth=auth, timeout=120,
                         params={"search": "IDEO-", "status": "any", "per_page": 100, "page": page, "_fields": "sku"})
        rows = r.json() if r.ok else []
        skus |= {str(x.get("sku") or "") for x in rows if str(x.get("sku") or "").startswith("IDEO-")}
        if len(rows) < 100:
            break
    skus = sorted(skus)
    LOG.info("ideo SKU v HQ: %d", len(skus))
    stats = {"total": len(skus), "renamed": 0, "recategorized": 0, "no_category": 0, "skipped_published": 0, "not_found": 0, "failed": 0}
    examples, no_cat = [], []
    variants_cache: dict = {}
    for sku in skus:
        wait_if_slow()
        try:
            r = requests.get(f"{wc_url}/wp-json/wc/v3/products", auth=auth, timeout=90,
                             params={"sku": sku, "status": "any", "_fields": "id,name,status,categories,meta_data"})
            rows = r.json() if r.ok else []
            if not rows:
                stats["not_found"] += 1; continue
            p = rows[0]
            if p.get("status") == "publish":
                stats["skipped_published"] += 1; continue
            meta = {m["key"]: m["value"] for m in p.get("meta_data") or [] if isinstance(m, dict)}
            base = str(meta.get("supplier_url") or "").split("#")[0]
            if not base:
                stats["not_found"] += 1; continue
            if base not in variants_cache:
                variants_cache[base] = {v.sku: v for v in sup.parse_variants(base)}
            v = variants_cache[base].get(sku)
            if not v:
                stats["not_found"] += 1; continue
            path = (v.raw_extra or {}).get("sg_category_path") or []
            upd = {"name": v.name, "meta_data": [{"key": "supplier_url", "value": v.supplier_url}]}
            if path:
                cid = wc.ensure_category_path(sanitize_category_path(path))
                if cid:
                    upd["categories"] = [{"id": cid}]
                    if [c.get("id") for c in p.get("categories") or []] != [cid]:
                        stats["recategorized"] += 1
            else:
                stats["no_category"] += 1; no_cat.append(f"{sku} {v.name[:60]}")
            if v.name != p.get("name"):
                stats["renamed"] += 1
            if len(examples) < 8:
                examples.append({"sku": sku, "old": p.get("name", "")[:70], "new": v.name[:70], "cat": " > ".join(path) or "—"})
            if not a.dry_run:
                u = requests.put(f"{wc_url}/wp-json/wc/v3/products/{p['id']}", auth=auth, json=upd, timeout=90)
                if not u.ok:
                    stats["failed"] += 1; LOG.warning("PUT %s HTTP %s", p["id"], u.status_code)
            time.sleep(0.3)
        except Exception as exc:
            stats["failed"] += 1; LOG.warning("%s zlyhalo: %s", sku, exc)
    print("SG_RESULT " + json.dumps({"ok": True, **stats, "examples": examples, "no_category": no_cat[:30], "dry_run": a.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
