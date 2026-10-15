#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pripravenosť na staged LKQ/Szekop parts import — checklist + report.

  python bot/prepare_parts_import.py
  python bot/prepare_parts_import.py --dry-run-lkq   # 1× crawl limit 3 (ak sú secrets)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
try:  # loginy z HQ (hq.smartgaraz.sk) majú prednosť pred secrets
    from suppliers.hq_credentials import export_logins as _hq_export
    _hq_export("lkq", "szekop")
except Exception:
    pass

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger("prepare_parts_import")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"


def _ok(msg: str) -> None:
    LOGGER.info("OK  %s", msg)


def _warn(msg: str) -> None:
    LOGGER.warning("!!  %s", msg)


def _fail(msg: str) -> None:
    LOGGER.error("XX  %s", msg)


def check_secrets() -> dict:
    out = {}
    for key in ("WC_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"):
        out[key] = bool(os.getenv(key))
    for key in ("LKQ_USER", "LKQ_PASSWORD"):
        out[key] = bool(os.getenv(key))
    for key in ("SZEKOP_USER", "SZEKOP_PASSWORD"):
        out[key] = bool(os.getenv(key))
    out["SZEKOP_B2B"] = os.getenv("SZEKOP_B2B", "").strip() in ("1", "true", "yes", "on")
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run-lkq", action="store_true", help="crawl 3 ks z prvého LKQ seedu")
    ap.add_argument("--dry-run-szekop", action="store_true", help="crawl 3 ks z TecDoc oil-filter leafu")
    ap.add_argument("--skip-sync", action="store_true")
    args = ap.parse_args()

    report: dict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "checks": {},
        "ready_lkq": False,
        "ready_szekop_oils": True,
        "ready_szekop_tecdoc_parts": False,
        "notes": [],
    }

    # 1) hierarchy
    rc = subprocess.call([sys.executable, str(ROOT / "validate_vehicle_hierarchy.py")])
    report["checks"]["vehicle_hierarchy"] = rc == 0
    (_ok if rc == 0 else _fail)("validate_vehicle_hierarchy")

    # 2) taxonomy files
    for p in (
        DATA / "PARTS_TAXONOMY.md",
        DATA / "PARTS_IMPORT_PILOT.md",
        DATA / "supplier_category_map.json",
        DATA / "parts_pilot_seeds.json",
    ):
        exists = p.is_file()
        report["checks"][p.name] = exists
        (_ok if exists else _fail)(str(p.relative_to(ROOT.parent)))

    seeds = {}
    seeds_path = DATA / "parts_pilot_seeds.json"
    if seeds_path.is_file():
        seeds = json.loads(seeds_path.read_text(encoding="utf-8"))
        n = len(seeds.get("lkq_example_seeds") or [])
        report["checks"]["lkq_seeds"] = n
        _ok(f"lkq example seeds: {n}")

    # 3) secrets (never print values)
    sec = check_secrets()
    report["checks"]["secrets"] = {k: v for k, v in sec.items()}
    if sec["WC_URL"] and sec["WC_CONSUMER_KEY"] and sec["WC_CONSUMER_SECRET"]:
        _ok("WooCommerce secrets")
    else:
        _fail("Chýba WC_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET")
    if sec["LKQ_USER"] and sec["LKQ_PASSWORD"]:
        _ok("LKQ login secrets")
        report["ready_lkq"] = bool(report["checks"].get("vehicle_hierarchy"))
    else:
        _warn("LKQ_USER/PASSWORD chýba — Heijunka diely nepôjde")
    if sec["SZEKOP_B2B"] and not (sec["SZEKOP_USER"] and sec["SZEKOP_PASSWORD"]):
        _warn("SZEKOP_B2B=1 ale chýba user/password")
    else:
        _ok("Szekop public OK (B2B voliteľné)")

    # 4) sync taxonomy
    if not args.skip_sync and sec["WC_URL"] and sec["WC_CONSUMER_KEY"]:
        rc = subprocess.call([sys.executable, str(ROOT / "sync_taxonomy.py"), "--ensure-only"])
        report["checks"]["sync_taxonomy"] = rc == 0
        (_ok if rc == 0 else _fail)("sync_taxonomy --ensure-only")
    else:
        report["checks"]["sync_taxonomy"] = None
        report["notes"].append("sync_taxonomy skipped")

    # 5) optional dry-run
    if args.dry_run_lkq:
        urls = [x.get("url") for x in (seeds.get("lkq_example_seeds") or []) if x.get("url")]
        if not urls or not (sec["LKQ_USER"] and sec["LKQ_PASSWORD"]):
            _warn("dry-run-lkq: chýba seed alebo LKQ secrets")
            report["checks"]["dry_run_lkq"] = False
        else:
            cmd = [
                sys.executable,
                str(ROOT / "import_pipeline.py"),
                "--supplier",
                "lkq",
                "--limit",
                "3",
                "--cheap",
                "--in-stock",
                "--dry-run",
                "--force-daytime",
                "--category-url",
                urls[0],
            ]
            rc = subprocess.call(cmd)
            report["checks"]["dry_run_lkq"] = rc == 0
            (_ok if rc == 0 else _fail)(f"dry-run lkq rc={rc}")

    # Szekop TecDoc leaves in seeds
    sz_leaves = ((seeds.get("szekop") or {}).get("tecdoc_pilot_leaves") or [])
    report["checks"]["szekop_tecdoc_leaves"] = len(sz_leaves)
    if sz_leaves:
        _ok(f"szekop tecdoc pilot leaves: {len(sz_leaves)}")
        report["ready_szekop_tecdoc_parts"] = True
    else:
        _warn("chýbajú szekop tecdoc_pilot_leaves")

    if args.dry_run_szekop:
        leaf = next((x.get("url") for x in sz_leaves if x.get("url")), "")
        if not leaf:
            _warn("dry-run-szekop: chýba leaf URL")
            report["checks"]["dry_run_szekop"] = False
        else:
            cmd = [
                sys.executable,
                str(ROOT / "import_pipeline.py"),
                "--supplier",
                "szekop",
                "--limit",
                "3",
                "--cheap",
                "--in-stock",
                "--dry-run",
                "--force-daytime",
                "--category-url",
                leaf,
            ]
            rc = subprocess.call(cmd)
            report["checks"]["dry_run_szekop"] = rc == 0
            (_ok if rc == 0 else _fail)(f"dry-run szekop rc={rc}")

    report["notes"].append(
        "Szekop TecDoc leaf crawl ready (SSR+expand API). Quiet: bash bot/run_szekop_quiet_drip.sh"
    )

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = REPORTS / f"ops-parts-import-ready-{stamp}.md"
    lines = [
        f"# Parts import readiness — {report['ts']}",
        "",
        f"- **LKQ ready:** {'YES' if report['ready_lkq'] else 'NO'}",
        f"- **Szekop oils:** YES",
        f"- **Szekop TecDoc parts:** {'YES' if report['ready_szekop_tecdoc_parts'] else 'NO'}",
        "",
        "## Checks",
        "```json",
        json.dumps(report["checks"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Next",
        "1. `bash bot/run_szekop_quiet_drip.sh` (denný tichý drip ≤5–8)",
        "2. `bash bot/run_heijunka_loop.sh 40 5 20 '' 1` (večer / force)",
        "3. `python bot/publish_ready_drafts.py --limit 40`",
        "4. Pozri `bot/data/PARTS_IMPORT_PILOT.md`",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    latest = REPORTS / "ops-parts-import-ready-latest.md"
    latest.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    _ok(f"report {out}")

    return 0 if report["ready_lkq"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
