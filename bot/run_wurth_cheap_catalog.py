#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Würth katalóg — rýchly objem (--cheap = 0 € Claude) po leaf kategóriách.

`--cheap` = lacný AI režim (keyword + template), NIE filter ceny.
Importuje aj drahé náradie; publish default max-price vysoký (5000 €).

  python bot/run_wurth_cheap_catalog.py --leaves 6 --limit 40
  python bot/run_wurth_cheap_catalog.py --quiet-drip   # deň: 1×≤8, sleep≥2s
  python bot/run_wurth_cheap_catalog.py --reset-progress

Progress: bot/data/wurth_cheap_progress.json
Priorita leafov: autodiely → chémia → náradie → ostatné (BOZP…).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
try:  # loginy z HQ (hq.smartgaraz.sk) majú prednosť pred secrets
    from suppliers.hq_credentials import export_logins as _hq_export
    _hq_export("wurth")
except Exception:
    pass

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from suppliers.wurth import (  # noqa: E402
    DEFAULT_CATEGORY_SEEDS,
    WurthSupplier,
    _cyid,
    child_category_urls,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
LOG = logging.getLogger("wurth_cheap_catalog")

PROGRESS = ROOT / "data" / "wurth_cheap_progress.json"

# Auto-relevantné vetvy (bez stavebnej chémie / záhrady / bicyklov).
SKIP_SLUG_PARTS = (
    "stavebn",
    "budov",
    "domacnost",
    "bicykl",
    "poľnoh",
    "polnoh",
    "zametanie",
    "kopanie",
    "metly-kefy",
)


def _slug(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    return parts[1] if len(parts) > 1 else url


def load_progress() -> Dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_cyids": [], "runs": []}


def save_progress(data: Dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# Stredné vetvy (autodiely + dielenská chémia + auto-náradie) — rýchlejší BFS.
MID_SEEDS = (
    "https://eshop.wurth.sk/Produktove-kategorie/Diely-pre-vozidla/31436502.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Elektricke-prislusenstvo-vozidla/31436501.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Prislusenstvo-pre-vozidla/31436503.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Klimatizacna-technika-pre-vozidla/31436504.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Cistiace-a-osetrovacie-prostriedky/31433007.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Maziva-a-odhrdzovace/31433008.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Servisna-chemia/31433009.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Tesniace-hmoty/31433003.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Lepiace-pasky/31433004.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Lepidla/31433005.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Povrchova-ochrana-a-uprava/31433006.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Specialne-naradie-pre-autoopravy/31436023.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Inspekcne-naradie-pre-auta-uzit.-vozidla/31436026.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Karosarske-naradie/31436007.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Klieste-a-pinzety/31436022.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Stahovaky/31436001.cyid/3143.cgid/sk/SK/EUR/",
)


def discover_leaves(supplier: WurthSupplier, max_pages: int = 250) -> List[str]:
    """BFS pod MID_SEEDS (+ DEFAULT) → leaf URL (bez detí). Neukončený queue → tiež leaf."""
    queue: List[str] = list(MID_SEEDS) + list(DEFAULT_CATEGORY_SEEDS)
    seen: Set[str] = set()
    leaves: List[str] = []
    pages = 0
    while queue and pages < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        pages += 1
        slug = _slug(url).lower()
        if any(x in slug for x in SKIP_SLUG_PARTS):
            continue
        try:
            html = supplier.http.get(url).text or ""
        except Exception:
            leaves.append(url)
            continue
        kids = sorted(child_category_urls(url, html))
        kids = [k for k in kids if not any(x in _slug(k).lower() for x in SKIP_SLUG_PARTS)]
        if not kids:
            leaves.append(url)
        else:
            for k in kids:
                if k not in seen:
                    queue.append(k)
    # Zvyšok fronty = ešte neprebádané vetvy → ber ako import seed (leaf-ish)
    for url in queue:
        slug = _slug(url).lower()
        if any(x in slug for x in SKIP_SLUG_PARTS):
            continue
        leaves.append(url)
    by_id: Dict[str, str] = {}
    for u in leaves:
        cid = _cyid(u)
        if cid:
            by_id[cid] = u
    return [by_id[k] for k in sorted(by_id.keys())]


def run_import(
    category_url: str,
    limit: int,
    sleep: float,
    *,
    quiet_drip: bool = False,
    force_daytime: bool = False,
) -> tuple[int, int]:
    """Vrátí (returncode, success_count). success≈limit → leaf ešte nie je hotový."""
    log_path = Path("/tmp") / f"wurth_import_{_cyid(category_url)}.log"
    cmd = [
        sys.executable,
        str(ROOT / "import_pipeline.py"),
        "--supplier",
        "wurth",
        "--limit",
        str(limit),
        "--cheap",
        "--fast",
        "--in-stock",
        "--sleep",
        str(sleep),
        "--category-url",
        category_url,
    ]
    if quiet_drip:
        cmd.append("--quiet-drip")
    elif force_daytime:
        cmd.append("--force-daytime")
    LOG.info("IMPORT limit=%s %s", limit, _slug(category_url))
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # "🎉 Hotovo: 15/15 úspešných" alebo "Hotovo: 12/15"
    ok_n = 0
    import re

    m = re.search(r"Hotovo:\s*(\d+)\s*/\s*(\d+)", text)
    if m:
        ok_n = int(m.group(1))
    LOG.info("IMPORT result rc=%s ok=%s/%s log=%s", proc.returncode, ok_n, limit, log_path)
    return int(proc.returncode), ok_n


def run_publish(limit: int, max_price: float, min_id: int) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "publish_ready_drafts.py"),
        "--limit",
        str(limit),
        "--max-price",
        str(max_price),
        "--min-id",
        str(min_id),
        "--name-contains",
        "Würth",
    ]
    LOG.info("PUBLISH Würth limit=%s max_price=%s min_id=%s", limit, max_price, min_id)
    return int(subprocess.run(cmd, cwd=str(ROOT)).returncode)


def homepage_ok() -> bool:
    try:
        import requests

        r = requests.get("https://www.smartgaraz.sk/", timeout=20, allow_redirects=True)
        ok = r.status_code == 200
        LOG.info("Homepage smoke HTTP %s", r.status_code)
        return ok
    except Exception as exc:
        LOG.warning("Homepage smoke fail: %s", exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaves", type=int, default=6, help="Koľko nových leaf kategórií v tomto behu")
    ap.add_argument("--limit", type=int, default=40, help="Max produktov na leaf (heijunka)")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--publish-limit", type=int, default=50)
    ap.add_argument(
        "--max-price",
        type=float,
        default=5000.0,
        help="Publish QA strop (€) — vysoký, aby prešli aj drahé náradie",
    )
    ap.add_argument("--min-id", type=int, default=1, help="Publish min WC id (default všetky)")
    ap.add_argument("--skip-publish", action="store_true")
    ap.add_argument("--reset-progress", action="store_true")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument(
        "--force-daytime",
        action="store_true",
        help="Cez SK 07–20 povoliť plný katalógový beh (inak daytime quiet)",
    )
    ap.add_argument(
        "--quiet-drip",
        action="store_true",
        help="Denný quiet drip: 1 leaf, ≤8 ks, sleep≥2s + smoke gate",
    )
    args = ap.parse_args()

    quiet_drip = bool(args.quiet_drip)
    # Denný quiet (Europe/Bratislava 07–20): bulk pauza; quiet drip OK.
    if not args.discover_only and not args.force_daytime:
        try:
            from daytime_quiet import (
                clamp_for_quiet_drip,
                homepage_smoke_ok,
                is_daytime_quiet,
                quiet_drip_enabled,
                quiet_drip_profile,
                quiet_message,
            )

            if is_daytime_quiet():
                if not quiet_drip_enabled():
                    LOG.warning("%s", quiet_message())
                    LOG.warning("SG_QUIET_DRIP=0 — len publish + smoke.")
                    if not args.skip_publish:
                        run_publish(
                            args.publish_limit,
                            max(args.max_price, 20000.0),
                            args.min_id,
                        )
                    homepage_ok()
                    return 0
                # Implicit drip v quiet okne (nie bulk multi-leaf).
                quiet_drip = True
                prof = quiet_drip_profile()
                args.leaves = min(max(1, args.leaves), prof.max_leaves)
                args.limit, args.sleep = clamp_for_quiet_drip(args.limit, args.sleep)
                args.publish_limit = min(args.publish_limit, prof.publish_limit)
                ok, code = homepage_smoke_ok()
                if not ok:
                    LOG.error(
                        "JIDOKA: homepage HTTP %s — quiet drip STOP (web mrzne/down).",
                        code,
                    )
                    return 2
                LOG.info(
                    "QUIET_DRIP Würth leaves=%s limit=%s sleep=%ss publish≤%s",
                    args.leaves,
                    args.limit,
                    args.sleep,
                    args.publish_limit,
                )
        except Exception:
            pass
    elif quiet_drip:
        # Explicit --quiet-drip aj mimo quiet okna (stále šetrný).
        try:
            from daytime_quiet import clamp_for_quiet_drip, quiet_drip_profile

            prof = quiet_drip_profile()
            args.leaves = min(max(1, args.leaves), prof.max_leaves)
            args.limit, args.sleep = clamp_for_quiet_drip(args.limit, args.sleep)
            args.publish_limit = min(args.publish_limit, prof.publish_limit)
        except Exception:
            args.leaves = 1
            args.limit = min(args.limit, 8)
            args.sleep = max(args.sleep, 2.0)

    if args.reset_progress and PROGRESS.exists():
        PROGRESS.unlink()
        LOG.info("Progress reset")

    progress = load_progress()
    done: Set[str] = set(progress.get("done_cyids") or [])

    creds = {
        "user": os.environ.get("WURTH_USER"),
        "password": os.environ.get("WURTH_PASSWORD"),
        "login_url": os.environ.get("WURTH_LOGIN_URL") or None,
    }
    supplier = WurthSupplier(credentials=creds)
    login_ok = supplier.ensure_login()
    if login_ok:
        LOG.info("Würth login OK (B2B)")
    else:
        # Public crawl je default — False = skip/public, nie fatálna chyba.
        LOG.info("Würth public crawl (bez B2B session)")

    cached = progress.get("leaf_urls") or []
    if cached and not args.discover_only and not args.reset_progress:
        leaves = list(cached)
        LOG.info("Leaves z progress cache: %d", len(leaves))
    else:
        leaves = discover_leaves(supplier)
        progress["leaf_urls"] = leaves
        save_progress(progress)

    from account_split import mine as _acct_mine
    pending = [u for u in leaves if _cyid(u) not in done and _acct_mine("wc:" + _cyid(u))]

    def _prio(url: str) -> tuple:
        cid = _cyid(url)
        # autodiely → chémia → náradie → BOZP / ostatné
        if cid.startswith("314365"):
            bucket = 0
        elif cid.startswith("314330"):
            bucket = 1
        elif cid.startswith("314360"):
            bucket = 2
        else:
            bucket = 3
        return (bucket, cid)

    pending.sort(key=_prio)
    LOG.info("Leaves total=%d done=%d pending=%d", len(leaves), len(done), len(pending))
    if args.discover_only:
        progress["leaf_urls"] = leaves
        save_progress(progress)
        for u in pending[:50]:
            LOG.info("PENDING %s %s", _cyid(u), _slug(u))
        if len(pending) > 50:
            LOG.info("... +%d", len(pending) - 50)
        return 0

    batch = pending[: max(0, args.leaves)]
    if not batch:
        LOG.info("Žiadne pending leafy — katalóg seedov hotový (alebo reset)")
        if not args.skip_publish:
            run_publish(args.publish_limit, args.max_price, args.min_id)
        homepage_ok()
        return 0

    run_meta = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "quiet_drip" if quiet_drip else "normal",
        "leaves": [],
        "publish_rc": None,
        "homepage_ok": None,
    }

    for url in batch:
        cid = _cyid(url)
        slug = _slug(url)
        rc, ok_n = run_import(
            url,
            args.limit,
            args.sleep,
            quiet_drip=quiet_drip,
            force_daytime=bool(args.force_daytime),
        )
        run_meta["leaves"].append(
            {
                "cyid": cid,
                "slug": slug,
                "rc": rc,
                "ok": ok_n,
                "limit": args.limit,
                "sleep": args.sleep,
            }
        )
        # Jeden prechod na leaf (heijunka). Ďalšie SKU v tom istom liste
        # až po reset/progress alebo vyšší --limit v neskoršom kole.
        if rc == 0:
            done.add(cid)
            progress["done_cyids"] = sorted(done)
            save_progress(progress)
            LOG.info("Marked done %s %s (ok=%s)", cid, slug, ok_n)
        else:
            LOG.warning("Import rc=%s — neoznačujem done %s", rc, slug)
        if quiet_drip:
            try:
                from daytime_quiet import homepage_smoke_ok

                ok, code = homepage_smoke_ok()
                if not ok:
                    LOG.error(
                        "JIDOKA: po importe homepage HTTP %s — STOP ďalších leafov.",
                        code,
                    )
                    run_meta["homepage_ok"] = False
                    progress.setdefault("runs", []).append(run_meta)
                    save_progress(progress)
                    return 2
            except Exception:
                pass
            time.sleep(1.5)
        else:
            time.sleep(0.5)

    imported_ok = sum(int(x.get("ok") or 0) for x in run_meta["leaves"])
    if not args.skip_publish and imported_ok > 0:
        run_meta["publish_rc"] = run_publish(args.publish_limit, args.max_price, args.min_id)
    elif not args.skip_publish:
        LOG.info("Skip publish — beh bez nových importov (ok=0)")
        run_meta["publish_rc"] = None
    run_meta["homepage_ok"] = homepage_ok()
    progress.setdefault("runs", []).append(run_meta)
    progress["done_cyids"] = sorted(done)
    save_progress(progress)

    LOG.info(
        "Hotovo beh: leaves=%d done_total=%d pending_left=%d homepage=%s",
        len(batch),
        len(done),
        max(0, len(leaves) - len(done)),
        run_meta["homepage_ok"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
