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


def prepare_szekop_phase1() -> None:
    """Catalog step settings."""
    os.environ["SZEKOP_B2B"] = "0"
    os.environ["SG_SZEKOP_PHASE"] = "1"
    os.environ.setdefault("SG_GUARD_SLUG", "szekop-public")
    os.environ.setdefault("SG_POLITE_HOSTS", "nextis.cz")
    os.environ.setdefault("SG_POLITE_MIN", "20")
    os.environ.setdefault("SG_POLITE_MAX", "35")
    os.environ.setdefault("SG_DAILY_CAP", "40")
    os.environ.setdefault("SG_RUN_CAP", "18")
    os.environ.setdefault("SG_HTTP_RETRIES", "0")
    os.environ["CRAWL_DELAY"] = "0"


def _reraise_stop(exc: BaseException) -> None:
    from supplier_pace import SupplierStop
    if isinstance(exc, SupplierStop):
        raise exc


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
    """Všetky SKU vo WC — z HQ (rýchle), fallback WC REST."""
    from hq_skus import existing
    skus, urls, src = existing()
    global EXISTING_URLS
    EXISTING_URLS = urls
    LOGGER.info("existujúce SKU: %d, URL: %d (zdroj %s)", len(skus), len(urls), src)
    return set(skus)


EXISTING_URLS: set = set()


def import_urls(supplier, urls: List[str], skus: set, sh: Shard, deadline: float) -> int:
    """Parse → skip existujúce SKU → sklad → import (cheap, koncept)."""
    from import_pipeline import import_products
    from ai_engine import is_supplier_in_stock
    fresh = []
    for u in urls:
        if time.time() > deadline:
            break
        if u in EXISTING_URLS or u.split("?")[0] in EXISTING_URLS:
            sh.state["stats"]["skipped_existing"] += 1  # už importované → bez fetchu detailu
            continue
        try:
            p = supplier.parse_product(u)
        except Exception as exc:
            _reraise_stop(exc)
            LOGGER.debug("parse fail: %s", type(exc).__name__)
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
    """Make → model → engine (SSR). Pri dennom strope sa zoznam uloží a pokračuje ďalší beh."""
    if sh.state.get("engines_complete") and sh.state.get("engines"):
        if sh.state.get("engines_all_makes", "0") == os.environ.get("SG_SZEKOP_ALL_MAKES", "0"):
            return sh.state["engines"]
    pre = "/sk/katalog/tecdoc/osobne/"
    makes = [(h, t) for h, t in _links(sup.http, SZ_ENTRY, pre) if re.fullmatch(pre + r"[a-z0-9-]+/\d+", h)]
    rank = {m: i for i, m in enumerate(PRIORITY_MAKES)}
    makes.sort(key=lambda x: (rank.get(x[0].split("/")[5], 999), x[0]))
    engines: List[Dict[str, str]] = list(sh.state.get("engines") or [])
    start = int(sh.state.get("engine_make_i") or 0)
    for i, (mh, mt) in enumerate(makes):
        if i < start:
            continue
        slug = mh.split("/")[5]
        if slug not in rank and os.environ.get("SG_SZEKOP_ALL_MAKES", "0") != "1":
            sh.state["engine_make_i"] = i + 1
            continue  # najprv známe značky; ostatné s SG_SZEKOP_ALL_MAKES=1
        try:
            models = [(h, t) for h, t in _links(sup.http, SZ_BASE + mh, pre + slug + "/")
                      if re.fullmatch(pre + slug + r"/[a-z0-9-]+/\d+/\d+", h)]
        except Exception as exc:
            _reraise_stop(exc)
            sh.state["engine_make_i"] = i + 1
            sh.state["engines"] = engines
            sh.save()
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
            except Exception as exc:
                _reraise_stop(exc)
                continue
            for eh, et in engs:
                engines.append({"url": SZ_BASE + eh, "make": mt, "model": t, "engine": et})
        sh.state["engine_make_i"] = i + 1
        sh.state["engines"] = engines
        sh.state["engines_complete"] = False
        sh.save()
    LOGGER.info("Szekop shard %d/%d: %d motorov", sh.i, sh.n, len(engines))
    sh.state["engines"] = engines
    sh.state["engines_complete"] = True
    sh.state["engines_all_makes"] = os.environ.get("SG_SZEKOP_ALL_MAKES", "0")
    sh.state.pop("engine_make_i", None)
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
            except Exception as exc:
                _reraise_stop(exc)
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
    except Exception as exc:
        _reraise_stop(exc)
        return []
    out: List[str] = []
    seen_pages = {leaf}
    queue_pages: List[str] = []
    max_pages = int(os.environ.get("SG_SZEKOP_MAX_PAGES", "30"))
    base_leaf = leaf.split("?")[0]
    while True:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            raw = a["href"].split("#")[0]
            full = SZ_BASE + raw if raw.startswith("/") else raw
            h = full.split("?")[0]
            if h.startswith(SZ_BASE) and is_tecdoc_product_url(h) and h not in out:
                out.append(h)
            # Stránkovanie listu (?page= / ?strana= / ?p=) — predtým len 1. stránka listu.
            if h == base_leaf and "?" in full and re.search(r"[?&](page|strana|p|pg)=\d+", full) \
                    and full not in seen_pages and len(seen_pages) < max_pages:
                seen_pages.add(full)
                queue_pages.append(full)
        if not queue_pages:
            break
        try:
            html = sup.http.get(queue_pages.pop(0)).text or ""
        except Exception as exc:
            _reraise_stop(exc)
            break
    return out


def run_szekop(sh: Shard, deadline: float, skus: set) -> int:
    prepare_szekop_phase1()
    from suppliers import get_supplier
    sup = get_supplier("szekop")
    if not getattr(sup, "_public_only", True):
        raise RuntimeError("status=failed")
    engines = szekop_engines(sup, sh)
    # Nový cyklus: všetky motory prejdené pred > SG_SZEKOP_RECYCLE_H h → znova (len nové SKU,
    # existujúce sa preskočia bez fetchu). Predtým sa hotový shard už nikdy nevrátil
    # (nové diely / znova naskladnené kusy sa nedostali do obchodu).
    if engines and all(e["url"] in set(sh.state["done"]) for e in engines):
        fin = sh.state.get("cycle_done_at")
        if not fin:
            sh.state["cycle_done_at"] = datetime.now(timezone.utc).isoformat()
            sh.save()
        else:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(fin)).total_seconds() / 3600
            if age_h >= float(os.environ.get("SG_SZEKOP_RECYCLE_H", "48")):
                LOGGER.info("Szekop shard %d/%d: nový cyklus (predošlý hotový pred %.0f h)", sh.i, sh.n, age_h)
                sh.state["done"] = []
                sh.state.pop("cycle_done_at", None)
                sh.state["cycles"] = int(sh.state.get("cycles") or 0) + 1
                if sh.state["cycles"] % 3 == 0:
                    sh.state.pop("engines", None)  # občas obnov zoznam motorov (nové modely)
                    engines = szekop_engines(sup, sh)
                sh.save()
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
                LOGGER.info("leaf: url=%d nových=%d (spolu %d)", len(urls), got, made)
            except Exception as exc:
                _reraise_stop(exc)
                LOGGER.warning("leaf fail: %s", type(exc).__name__)
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
    raise RuntimeError("skipped")


def _finish_shard(sh: Shard, supplier: str, shard: str, made: int, t0: float, *, ok: bool, reason: str) -> int:
    from supplier_pace import decide_outcome, job_summary, guard
    sh.save()
    stats = sh.state.get("stats") or {}
    seen = int(stats.get("parsed") or 0)
    skipped = int(stats.get("skipped_existing") or 0)
    cap = reason in {"cap", "run_cap"}
    blocked = reason == "blocked"
    requests_n = 0
    try:
        requests_n = int(guard(os.environ.get("SG_GUARD_SLUG") or "szekop-public").snapshot().get("run_requests") or 0)
    except Exception:
        requests_n = 0
    # Count requests toward progress when the run was not stopped.
    progress = requests_n if not blocked else 0
    success, why = decide_outcome(
        seen=max(seen, progress), new=made, skipped_existing=skipped, blocked=blocked, cap=cap,
    )
    if reason == "login":
        success, why = False, "login"
    slug = os.environ.get("SG_GUARD_SLUG") or "szekop-public"
    try:
        g = guard(slug)
        if success:
            g.note_success()
        elif why != "paused":
            g.note_failure(why)
    except Exception:
        pass
    job_summary(
        "Szekop",
        status="ok" if success else "failed",
        new=made,
        seen=seen,
        skipped=skipped,
    )
    print("SG_RESULT " + json.dumps({
        "ok": success, "status": "ok" if success else "failed",
        "new": made, "seen": seen, "skipped": skipped,
    }, ensure_ascii=False), flush=True)
    return 0 if success else 1


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
    if a.supplier == "wurth":
        from supplier_pace import job_summary
        job_summary("Wurth", status="failed", new=0, seen=0)
        print("SG_RESULT " + json.dumps({
            "ok": False, "status": "failed", "new": 0, "seen": 0,
        }, ensure_ascii=False), flush=True)
        return 1
    prepare_szekop_phase1()
    from supplier_pace import DailyCapReached, LoginFailed, RunCapReached, SupplierBlocked, guard, job_summary
    from supplier_pace import resume_requested
    st = guard(os.environ.get("SG_GUARD_SLUG") or "szekop-public").begin(resume=resume_requested())
    if st.get("paused"):
        job_summary("Szekop", status="failed", new=0, seen=0)
        print('SG_RESULT {"ok": false, "status": "failed", "new": 0, "seen": 0}', flush=True)
        return 1
    if int(st.get("cap_left") or 0) <= 0:
        job_summary("Szekop", status="ok", new=0, seen=int(st.get("requests") or 0))
        print("SG_RESULT " + json.dumps({
            "ok": True, "status": "ok", "new": 0, "seen": int(st.get("requests") or 0),
        }, ensure_ascii=False), flush=True)
        return 0
    try:
        skus = existing_skus()
        made = run_szekop(sh, deadline, skus)
    except (DailyCapReached, RunCapReached):
        return _finish_shard(sh, "szekop", a.shard, int(sh.state["stats"].get("created") or 0), t0, ok=True, reason="cap")
    except SupplierBlocked:
        return _finish_shard(sh, "szekop", a.shard, int(sh.state["stats"].get("created") or 0), t0, ok=False, reason="blocked")
    except LoginFailed:
        return _finish_shard(sh, "szekop", a.shard, 0, t0, ok=False, reason="login")
    return _finish_shard(sh, "szekop", a.shard, made, t0, ok=True, reason="done")


if __name__ == "__main__":
    raise SystemExit(main())
