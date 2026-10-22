#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heijunka Szekop TecDoc listy — staged ≤50 SKU / leaf.

Fronta z `parts_pilot_seeds.json` → `szekop.tecdoc_pilot_leaves`
(+ voliteľne expand parentov). Stav: `heijunka_szekop_queue_state.json`.

  python bot/heijunka_szekop_parts.py --jobs 3 --limit 30
  python bot/heijunka_szekop_parts.py --dry-run --jobs 2
  python bot/heijunka_szekop_parts.py --expand-filters   # doplň filtre z API
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("heijunka_szekop")
SEEDS = ROOT / "data" / "parts_pilot_seeds.json"
STATE_PATH = ROOT / "data" / "heijunka_szekop_queue_state.json"
REPORTS = ROOT / "reports"

# TecDoc parents na example_engine (Octavia III 1.6 TDI). Cursor v state
# rotuje po 2–3 rodičoch / beh → stále nové listy namiesto recheck seeds.
_DEFAULT_EXPAND = (
    ("100005", None),             # Filtre
    ("100626", "100006~100626"),  # Kotúčová brzda
    ("100018", None),             # Stierače
    ("100006", None),             # Brzdové zariadenie
    ("100002", None),             # Motor
    ("100007", None),             # Chladenie
    ("100011", None),             # Pruženie / tlmenie
    ("100050", None),             # Spojka
    ("100010", None),             # Elektrina
    ("100008", None),             # Zapalovanie
    ("100012", None),             # Riadenie
    ("100004", None),             # Výfuk
)
_EXPAND_PER_RUN = 3


def _load_seeds() -> dict:
    if not SEEDS.is_file():
        return {}
    return json.loads(SEEDS.read_text(encoding="utf-8"))


def _engine_url(seeds: dict) -> str:
    sz = seeds.get("szekop") or {}
    return (sz.get("example_engine") or "").rstrip("/")


def _seed_jobs(seeds: dict) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in (seeds.get("szekop") or {}).get("tecdoc_pilot_leaves") or []:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        out.append(
            {
                "id": str(row.get("leaf_id") or url.rstrip("/").split("/")[-1].split("?")[0]),
                "label": row.get("label") or url,
                "url": url,
                "parent_id": str(row.get("parent_id") or ""),
            }
        )
    return out


def _job_key(job: Dict[str, str]) -> str:
    return f"szekop|{job.get('id') or ''}|{job.get('url', '').split('?')[0].rstrip('/').split('/')[-1]}"


def _load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "failed": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _expand_parents(engine: str, parents: List[tuple]) -> List[Dict[str, str]]:
    from suppliers.szekop import SzekopSupplier

    sup = SzekopSupplier()
    out: List[Dict[str, str]] = []
    for cid, path in parents:
        leaves = sup.expand_tecdoc_subcategories(
            engine,
            str(cid),
            path=path,
        )
        for row in leaves:
            if row.get("end") != "1" or not row.get("leaf_url"):
                continue
            # Skip brake fluid as "parts" (náplň) — optional keep; leave in queue
            out.append(
                {
                    "id": row["id"],
                    "label": row.get("name") or row["id"],
                    "url": row["leaf_url"],
                    "parent_id": str(cid),
                }
            )
    return out


def _run_import(url: str, limit: int, sleep: float, dry_run: bool, offset: int = 0) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "import_pipeline.py"),
        "--supplier",
        "szekop",
        "--limit",
        str(limit),
        "--cheap",
        "--in-stock",
        "--category-url",
        url,
        "--sleep",
        str(sleep),
    ]
    if dry_run:
        cmd.append("--dry-run")
    # Evening / CI sets SG_DAYTIME_QUIET=force_run; local daytime needs --force-daytime
    if os.getenv("SG_DAYTIME_QUIET", "").strip().lower() in ("force_run", "0", "false", "off"):
        pass
    else:
        try:
            from daytime_quiet import is_daytime_quiet

            if is_daytime_quiet():
                cmd.append("--force-daytime")
        except Exception:
            pass
    env = os.environ.copy()
    if offset > 0:
        env["SG_DISCOVER_OFFSET"] = str(int(offset))
    LOGGER.info("import szekop limit=%s offset=%s %s", limit, offset, url[-70:])
    return int(subprocess.run(cmd, cwd=str(ROOT), env=env).returncode)


def _publish(limit: int = 40) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "publish_ready_drafts.py"),
        "--limit",
        str(limit),
        "--sleep",
        "0.5",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    # count PUB lines
    n = sum(1 for line in (proc.stdout or "").splitlines() if "PUB #" in line or "published=" in line.lower())
    LOGGER.info("publish rc=%s", proc.returncode)
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs", type=int, default=3, help="listov / beh (≤8)")
    ap.add_argument("--limit", type=int, default=30, help="SKU / leaf (≤50)")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-failed", action="store_true")
    ap.add_argument(
        "--expand-filters",
        action="store_true",
        help="Doplň frontu o Filtre+kotúče+stierače z expand API (Octavia engine)",
    )
    ap.add_argument("--seeds-only", action="store_true", help="Len seeds, bez expand")
    ap.add_argument("--force-daytime", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()

    if args.limit > 50:
        raise SystemExit("Heijunka Szekop: --limit max 50")
    if args.jobs > 8:
        args.jobs = 8

    if not args.force_daytime and not args.dry_run:
        try:
            from daytime_quiet import is_daytime_quiet, quiet_message

            if is_daytime_quiet() and os.getenv("SG_DAYTIME_QUIET", "").strip().lower() != "force_run":
                LOGGER.warning("%s", quiet_message())
                return 0
        except Exception:
            pass

    seeds = _load_seeds()
    jobs = _seed_jobs(seeds)
    state = _load_state()
    if args.reset_failed:
        state["failed"] = {}
    done = state.setdefault("done", {})
    failed = state.setdefault("failed", {})
    progress = state.setdefault("progress", {})  # key -> {offset, rounds}
    extra_leaves = state.setdefault("extra_leaves", [])

    # Obnov extra listy z predchádzajúcich expandov
    seen_urls = {j["url"].split("?")[0] for j in jobs}
    for j in extra_leaves:
        key_u = (j.get("url") or "").split("?")[0]
        if key_u and key_u not in seen_urls:
            jobs.append(j)
            seen_urls.add(key_u)

    # Expand: rotuj rodičov (nové listy = nové produkty), soft-fail
    if args.expand_filters and not args.seeds_only:
        engine = _engine_url(seeds)
        if engine:
            cursor = int(state.get("expand_cursor") or 0)
            batch_parents = []
            n = len(_DEFAULT_EXPAND)
            for i in range(_EXPAND_PER_RUN):
                batch_parents.append(_DEFAULT_EXPAND[(cursor + i) % n])
            state["expand_cursor"] = (cursor + _EXPAND_PER_RUN) % n
            try:
                extra = _expand_parents(engine, batch_parents)
                added = 0
                for j in extra:
                    key_u = j["url"].split("?")[0]
                    if key_u not in seen_urls:
                        jobs.append(j)
                        extra_leaves.append(j)
                        seen_urls.add(key_u)
                        added += 1
                LOGGER.info(
                    "expand cursor=%s parents=%s +%d new leaves (total %d)",
                    cursor,
                    [p[0] for p in batch_parents],
                    added,
                    len(jobs),
                )
            except Exception as exc:
                LOGGER.warning("expand soft-fail (pokračujem seeds/extra): %s", exc)
        else:
            LOGGER.warning("chýba example_engine v seeds")

    pending = [
        j
        for j in jobs
        if _job_key(j) not in done
        and _job_key(j) not in failed
        and not (progress.get(_job_key(j)) or {}).get("exhausted")
    ]
    if not pending and jobs and not args.dry_run:
        # Fronta hotová → nový cyklus s hlbším offsetom (skip-existing = len nové SKU)
        for k in list(done.keys()):
            pr = progress.setdefault(k, {})
            pr["offset"] = int(pr.get("offset") or 0) + int(pr.get("last_limit") or 40)
            pr["rounds"] = int(pr.get("rounds") or 0) + 1
            if pr["rounds"] >= 8 or pr["offset"] >= 400:
                pr["exhausted"] = True
                continue
            done.pop(k, None)
        pending = [
            j
            for j in jobs
            if _job_key(j) not in done
            and _job_key(j) not in failed
            and not (progress.get(_job_key(j)) or {}).get("exhausted")
        ]
        LOGGER.info("szekop fronta prejdená → nový cyklus pending=%d", len(pending))

    LOGGER.info(
        "fronta: pending=%d done=%d failed=%d total=%d extra=%d",
        len(pending),
        len(done),
        len(failed),
        len(jobs),
        len(extra_leaves),
    )
    from account_split import mine as _acct_mine, count as _acct_n
    if _acct_n() > 1:
        pending = [j for j in pending if _acct_mine("hs:" + json.dumps(j, sort_keys=True, ensure_ascii=False))]
    batch = pending[: max(0, args.jobs)]
    results: List[Dict[str, Any]] = []

    for job in batch:
        key = _job_key(job)
        offset = int((progress.get(key) or {}).get("offset") or 0)
        LOGGER.info("=== JOB %s offset=%s | %s ===", key, offset, job.get("label"))
        row: Dict[str, Any] = {"job": job, "key": key, "ok": False, "offset": offset}
        try:
            rc = _run_import(job["url"], args.limit, args.sleep, args.dry_run, offset=offset)
            row["import_rc"] = rc
            if rc != 0:
                raise RuntimeError(f"import_pipeline rc={rc}")
            pub = 0
            if not args.dry_run and not args.no_publish:
                pub = _publish(40)
                row["published"] = pub
            row["ok"] = True
            if not args.dry_run:
                pr = progress.setdefault(key, {})
                pr["last_limit"] = args.limit
                pr["last_published"] = pub
                pr["at"] = datetime.now(timezone.utc).isoformat()
                if pub == 0 and offset > 0:
                    pr["exhausted"] = True
                elif pub == 0 and offset == 0:
                    # Prvý prechod bez nových — done (recycle neskôr posunie offset)
                    pass
                else:
                    # Úspech s novými → nabudúce hlbšie
                    pr["offset"] = offset + args.limit
                done[key] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "url": job["url"],
                    "label": job.get("label"),
                    "limit": args.limit,
                    "offset": offset,
                    "published": pub,
                }
                failed.pop(key, None)
        except Exception as exc:
            LOGGER.error("JOB FAIL %s: %s", key, exc)
            row["error"] = str(exc)
            if not args.dry_run:
                failed[key] = {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        results.append(row)
        _save_state(state)
        time.sleep(0.4)

    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"ops-heijunka-szekop-{ts}.md"
    lines = [
        f"# Heijunka Szekop TecDoc — {ts}",
        "",
        f"- jobs this run: {len(results)}",
        f"- ok: {sum(1 for r in results if r.get('ok'))}",
        f"- fail: {sum(1 for r in results if not r.get('ok'))}",
        f"- queue done: {len(done)} / {len(jobs)}",
        "",
        "## Runs",
    ]
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        lines.append(f"- **{status}** `{r['key']}` {(r.get('job') or {}).get('label','')} {r.get('error','')}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("report %s", report)
    # Heijunka: čiastočný úspech = OK (jeden padnutý leaf nemá zabíjať večerný job).
    ok_n = sum(1 for r in results if r.get("ok"))
    if not results or ok_n > 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
