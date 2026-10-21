#!/usr/bin/env python3
"""Plný katalóg Szekop / Würth — paralelné shardy s resumable kurzorom.

  python bot/catalog_shard.py --supplier szekop --shard 0/3 --budget-min 300
  python bot/catalog_shard.py --supplier wurth  --shard 1/2 --budget-min 300

Každý shard (GitHub Actions matrix job) má vlastný stav
bot/data/shard_state/<supplier>-<i>of<n>.json (actions/cache, ukladá sa aj
pri zrušení). Úlohy sú rozdelené crc32(kľúč) % n → dva joby nikdy nerobia
to isté. Import ide cez import_products (cheap, len koncepty, bez fallback
kategórií — nejasná kategória = koncept bez kategórie). Fotky sa pri
SG_DEFER_IMAGES=1 len zapíšu do meta a doplní ich enrich_images.py.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LOGGER = logging.getLogger("catalog_shard")
SZ_BASE = "https://szekopsk.nextis.cz"
SZ_ENTRY = SZ_BASE + "/sk/katalog/tecdoc/osobne"
SZ_TOP = ("100005", "100006", "100002", "100007", "100011", "100012", "100050",
          "100004", "100010", "100008", "100018", "100001")
# Najčastejšie značky v SK/CZ parku najprv (zvyšok potom abecedne).
PRIORITY_MAKES = ("skoda", "volkswagen", "audi", "seat", "cupra", "ford", "opel", "peugeot", "renault",
                  "hyundai", "kia", "toyota", "bmw", "mercedes-benz", "citroen", "dacia", "fiat", "nissan",
                  "mazda", "honda", "volvo", "suzuki", "mitsubishi", "mini", "land-rover", "jeep", "alfa-romeo",
                  "chevrolet", "subaru", "lexus", "porsche", "jaguar", "smart", "ds", "tesla", "mg")


def shard_of(key: str, n: int) -> int:
    return zlib.crc32(key.encode("utf-8")) % n


class Shard:
    def __init__(self, supplier: str, i: int, n: int):
        self.i, self.n = i, n
        self.path = ROOT / "data" / "shard_state" / f"{supplier}-{i}of{n}.json"
        self.state: Dict[str, Any] = {}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.state = {}
        self.state.setdefault("done", [])
        self.state.setdefault("stats", {"created": 0, "parsed": 0, "skipped_existing": 0, "runs": 0})

    def mine(self, key: str) -> bool:
        return shard_of(key, self.n) == self.i

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def existing_skus() -> set:
    """Všetky SKU vo WC (1× na beh, ~100/req, šetrne)."""
    import requests
    from shop_guard import wait_if_slow
    url = os.environ.get("WC_URL", "").rstrip("/")
    auth = (os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", ""))
    out, page = set(), 1
    while page < 2000:
        wait_if_slow()
        r = None
        for wait in (0, 10, 30, 60):
            time.sleep(wait)
            try:
                r = requests.get(f"{url}/wp-json/wc/v3/products", auth=auth, timeout=90,
                                 params={"per_page": 100, "page": page, "status": "any", "_fields": "sku"})
                if r.ok:
                    break
            except Exception:
                r = None
        if r is None or not r.ok:
            break
        rows = r.json()
        out |= {str(x.get("sku") or "").strip() for x in rows if x.get("sku")}
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.3)
    LOGGER.info("WC existujúce SKU: %d", len(out))
    return out


def import_urls(supplier, urls: List[str], skus: set, sh: Shard, deadline: float) -> int:
    """Parse → skip existujúce SKU → sklad → import (cheap, koncept)."""
    from import_pipeline import import_products
    from ai_engine import is_supplier_in_stock
    fresh = []
    for u in urls:
        if time.time() > deadline:
            break
        try:
            p = supplier.parse_product(u)
        except Exception as exc:
            LOGGER.debug("parse fail %s: %s", u, exc)
            p = None
        sh.state["stats"]["parsed"] += 1
        if not p or not getattr(p, "name", ""):
            continue
        raw = p.to_ai_dict() if hasattr(p, "to_ai_dict") else {}
        sku = str(raw.get("sku") or getattr(p, "sku", "") or "").strip()
        if sku and sku in skus:
            sh.state["stats"]["skipped_existing"] += 1
            continue
        if not is_supplier_in_stock(raw):
            continue
        if sku:
            skus.add(sku)
        fresh.append(p)
    if not fresh:
        return 0
    res = import_products(fresh, sleep=float(os.environ.get("SG_IMPORT_SLEEP", "0.2")), cheap=True,
                          fast=True, in_stock_only=True)
    made = sum(1 for r in res if r.get("wc_product_id"))
    sh.state["stats"]["created"] += made
    return made


# ----------------------------------------------------------------- Szekop

def _links(http, url: str, prefix: str) -> List[tuple]:
    from bs4 import BeautifulSoup
    html = http.get(url).text or ""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        h = a["href"].split("?")[0].split("#")[0]
        if h.startswith(prefix) and h not in seen and h.rstrip("/") != prefix.rstrip("/"):
            seen.add(h)
            out.append((h, a.get_text(" ", strip=True)))
    return out


def szekop_engines(sup, sh: Shard) -> List[Dict[str, str]]:
    """Make → model → engine (SSR). Modely sú shardované, teda každý shard
    stiahne len motory svojich modelov. Výsledok sa cachuje v stave."""
    if sh.state.get("engines"):
        return sh.state["engines"]
    pre = "/sk/katalog/tecdoc/osobne/"
    makes = [(h, t) for h, t in _links(sup.http, SZ_ENTRY, pre) if re.fullmatch(pre + r"[a-z0-9-]+/\d+", h)]
    rank = {m: i for i, m in enumerate(PRIORITY_MAKES)}
    makes.sort(key=lambda x: (rank.get(x[0].split("/")[5], 999), x[0]))
    engines: List[Dict[str, str]] = []
    for mh, mt in makes:
        slug = mh.split("/")[5]
        if slug not in rank:
            continue  # najprv známe značky; ostatné neskôr (SG_SZEKOP_ALL_MAKES=1)
        try:
            models = [(h, t) for h, t in _links(sup.http, SZ_BASE + mh, pre + slug + "/")
                      if re.fullmatch(pre + slug + r"/[a-z0-9-]+/\d+/\d+", h)]
        except Exception:
            continue
        for h, t in models:
            if not sh.mine(h):
                continue
            yrs = [int(y) for y in re.findall(r"(?:19|20)\d{2}", t)]
            if yrs and max(yrs) < int(os.environ.get("SG_SZEKOP_MIN_YEAR", "2005")) and not re.search(r"-\s*$", t):
                continue  # model ukončený pred 2005 → v prevádzke už málo áut
            try:
                engs = [(eh, et) for eh, et in _links(sup.http, SZ_BASE + h, h.rsplit("/", 2)[0] + "/")
                        if re.fullmatch(r".*/\d+/\d+/\d+", eh)]
            except Exception:
                continue
            for eh, et in engs:
                engines.append({"url": SZ_BASE + eh, "make": mt, "model": t, "engine": et})
            time.sleep(0.2)
    LOGGER.info("Szekop shard %d/%d: %d motorov", sh.i, sh.n, len(engines))
    sh.state["engines"] = engines
    sh.save()
    return engines


def szekop_leaves(sup, eng: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for cid in SZ_TOP:
        stack = [(cid, cid)]
        depth = 0
        while stack and depth < 40:
            depth += 1
            nid, path = stack.pop()
            try:
                rows = sup.expand_tecdoc_subcategories(eng["url"], nid, path=path, manufacturer=eng["make"],
                                                       model=eng["model"], engine=eng["engine"])
            except Exception:
                rows = []
            for r in rows:
                if r.get("end") == "1" and r.get("leaf_url"):
                    out.append(r["leaf_url"])
                elif r.get("id") and r.get("path"):
                    stack.append((r["id"], r["path"]))
    return out


def szekop_leaf_products(sup, leaf: str) -> List[str]:
    """Len produkty z daného listu — BEZ fallbacku na crawl celého webu
    (prázdny list, napr. olejový filter pre elektromobil, inak visel 25 min)."""
    from suppliers.szekop import is_tecdoc_product_url
    from bs4 import BeautifulSoup
    try:
        html = sup.http.get(leaf).text or ""
    except Exception:
        return []
    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        h = a["href"].split("#")[0].split("?")[0]
        if h.startswith("/"):
            h = SZ_BASE + h
        if h.startswith(SZ_BASE) and is_tecdoc_product_url(h) and h not in out:
            out.append(h)
    return out


def run_szekop(sh: Shard, deadline: float, skus: set) -> int:
    from suppliers import get_supplier
    sup = get_supplier("szekop")
    engines = szekop_engines(sup, sh)
    done = set(sh.state["done"])
    made = 0
    for eng in engines:
        if time.time() > deadline:
            break
        if eng["url"] in done:
            continue
        leaves = szekop_leaves(sup, eng)
        LOGGER.info("=== ENGINE %s %s %s: %d listov", eng["make"], eng["model"], eng["engine"], len(leaves))
        leaf_done = set(sh.state.get("leaf_done_" + eng["url"], []))
        for leaf in leaves:
            if time.time() > deadline:
                break
            if leaf in leaf_done:
                continue
            try:
                urls = szekop_leaf_products(sup, leaf)
                got = import_urls(sup, urls, skus, sh, deadline) if urls else 0
                made += got
                LOGGER.info("leaf %s: url=%d nových=%d (spolu %d)", leaf.split("/?")[0][-60:], len(urls), got, made)
            except Exception as exc:
                LOGGER.warning("leaf fail %s: %s", leaf, exc)
            leaf_done.add(leaf)
            sh.state["leaf_done_" + eng["url"]] = sorted(leaf_done)
            sh.save()
        else:
            sh.state.pop("leaf_done_" + eng["url"], None)
            sh.state["done"].append(eng["url"])
            done.add(eng["url"])
            sh.save()
    return made


# ------------------------------------------------------------------ Würth

def run_wurth(sh: Shard, deadline: float, skus: set) -> int:
    from suppliers import get_supplier
    import suppliers.wurth as W
    sup = get_supplier("wurth")
    if not sh.state.get("urls"):
        # Plný crawl (bez 400 stropu); zoznam URL sa uloží a delí shardom.
        os.environ.setdefault("SG_WURTH_MAX_PAGES", "1500")  # ~2 s/str. ≈ 50 min
        urls = sup.discover_product_urls(None)  # vrátane B2B loginu (verejný katalóg dáva 0 URL)
        sh.state["urls"] = [u for u in urls if sh.mine(u)]
        LOGGER.info("Würth crawl: %d URL, shard %d/%d → %d", len(urls), sh.i, sh.n, len(sh.state["urls"]))
        sh.save()
    urls = sh.state["urls"]
    cur = int(sh.state.get("cursor") or 0)
    made = 0
    while cur < len(urls) and time.time() < deadline:
        chunk = urls[cur:cur + 50]
        made += import_urls(sup, chunk, skus, sh, deadline)
        cur += len(chunk)
        sh.state["cursor"] = cur
        sh.save()
        LOGGER.info("Würth kurzor %d/%d nových spolu %d", cur, len(urls), made)
    if cur >= len(urls):
        sh.state.pop("urls", None)  # ďalší beh = nový crawl (nové SKU)
        sh.state["cursor"] = 0
        sh.save()
    return made


def main() -> int:
    from ai_engine import setup_logging
    try:
        setup_logging()
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True, choices=["szekop", "wurth"])
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--budget-min", type=float, default=300)
    a = ap.parse_args()
    i, n = (int(x) for x in a.shard.split("/"))
    sh = Shard(a.supplier, i, n)
    sh.state["stats"]["runs"] += 1
    t0 = time.time()
    deadline = t0 + a.budget_min * 60
    skus = existing_skus()
    made = run_szekop(sh, deadline, skus) if a.supplier == "szekop" else run_wurth(sh, deadline, skus)
    sh.save()
    hrs = max((time.time() - t0) / 3600, 1e-6)
    print("SG_RESULT " + json.dumps({"ok": True, "supplier": a.supplier, "shard": a.shard, "new": made,
                                     "per_hour": round(made / hrs), "minutes": round((time.time() - t0) / 60, 1),
                                     "stats": sh.state["stats"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
