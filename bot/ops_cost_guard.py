#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonómny cost/muda guard — bez Anthropic.

Kontroluje:
  • homepage smoke + page-cache HIT signal
  • odporúčané env (AI_MODE=cheap, IMPORT_IN_STOCK)
  • WC: koľko produktov má _ai_mode=full vs cheap (sample)

  python bot/ops_cost_guard.py
  python bot/ops_cost_guard.py --write-report
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
LOG = logging.getLogger("ops_cost_guard")


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on", "cheap", "template", "budget", "zero"}


def homepage_check() -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False}
    try:
        r = requests.get(
            "https://www.smartgaraz.sk/",
            timeout=30,
            headers={
                "User-Agent": "SmartGaraz-CostGuard/1.0",
                "Accept-Encoding": "gzip",
            },
        )
        out["status"] = r.status_code
        out["cache"] = r.headers.get("x-sg-cache")
        out["cache_enc"] = r.headers.get("x-sg-cache-encoding")
        out["plugin"] = r.headers.get("x-sg-cache-plugin")
        out["ok"] = r.status_code == 200
        # warm
        r2 = requests.get(
            "https://www.smartgaraz.sk/",
            timeout=30,
            headers={"User-Agent": "SmartGaraz-CostGuard/1.0", "Accept-Encoding": "gzip"},
        )
        out["warm_cache"] = r2.headers.get("x-sg-cache")
        out["warm_enc"] = r2.headers.get("x-sg-cache-encoding")
    except Exception as exc:
        out["error"] = str(exc)
    return out


def sample_ai_modes(limit: int = 100) -> Dict[str, int]:
    url = (os.getenv("WC_URL") or "").rstrip("/")
    ck = os.getenv("WC_CONSUMER_KEY")
    cs = os.getenv("WC_CONSUMER_SECRET")
    counts = {"cheap": 0, "full": 0, "other": 0, "sampled": 0}
    if not (url and ck and cs):
        return counts
    auth = HTTPBasicAuth(ck, cs)
    try:
        r = requests.get(
            f"{url}/wp-json/wc/v3/products",
            auth=auth,
            params={"per_page": min(100, limit), "status": "publish", "orderby": "date", "order": "desc"},
            timeout=60,
        )
        r.raise_for_status()
        products = r.json() if isinstance(r.json(), list) else []
    except Exception as exc:
        LOG.warning("WC sample fail: %s", exc)
        return counts
    for p in products:
        counts["sampled"] += 1
        meta = {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}
        mode = str(meta.get("_ai_mode") or meta.get("_ai_processed") or "").lower()
        if mode in {"cheap", "template"}:
            counts["cheap"] += 1
        elif mode in {"full", "yes"}:
            counts["full"] += 1
        else:
            counts["other"] += 1
    return counts


def build_report() -> tuple[str, bool]:
    ai_mode = (os.getenv("AI_MODE") or "").strip() or "(unset)"
    in_stock = (os.getenv("IMPORT_IN_STOCK") or "").strip() or "(unset)"
    home = homepage_check()
    modes = sample_ai_modes(100)
    customer_ok = True
    customer_line = "(skip)"
    try:
        sys.path.insert(0, str(ROOT))
        from customer_paths_smoke import check_paths  # noqa: E402

        base = (os.getenv("WC_URL") or "https://www.smartgaraz.sk").rstrip("/")
        cust = check_paths(base)
        customer_ok = bool(cust.get("ok"))
        failed = ", ".join(cust.get("failed") or []) or "—"
        customer_line = f"ok={customer_ok} failed={failed}"
    except Exception as exc:
        customer_ok = False
        customer_line = f"error={exc}"
    lines = [
        f"# Cost / muda guard — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Env (0 € Claude)",
        f"- `AI_MODE` = `{ai_mode}` — odporúčané: `cheap`",
        f"- `IMPORT_IN_STOCK` = `{in_stock}` — odporúčané: `1`",
        f"- Anthropic key present: {'yes' if os.getenv('ANTHROPIC_API_KEY') else 'no'}",
        "",
        "## Homepage",
        f"- HTTP {home.get('status')} ok={home.get('ok')} cache={home.get('cache')} enc={home.get('cache_enc')}",
        f"- Warm: cache={home.get('warm_cache')} enc={home.get('warm_enc')}",
        "",
        "## Customer paths",
        f"- {customer_line} — runbook `bot/CUSTOMER_OPS.md`",
        "",
        "## AI mode sample (recent publish)",
        f"- sampled={modes['sampled']} cheap={modes['cheap']} full={modes['full']} other={modes['other']}",
        "",
        "## Autonómne pravidlá (držať)",
        "- `refresh-clicked` → `--skip-ai` (B2B only)",
        "- import → `--cheap --fast --in-stock` (default cheap od import_pipeline)",
        "- full Claude len s `--full-ai --full-ai-confirm` a limit ≤20",
        "- heijunka dávky 20–50 ks",
        "- quiet SK 07–20: drip only (nie bulk)",
        "",
    ]
    if ai_mode.lower() not in {"cheap", "template", "budget", "zero"}:
        lines.append("⚠️  Nastav Cloud/GHA secret `AI_MODE=cheap` (0 € template path).")
    if not _env_truthy("IMPORT_IN_STOCK"):
        lines.append("⚠️  Nastav `IMPORT_IN_STOCK=1` (skip OOS = menej crawl/API).")
    if home.get("warm_cache") != "HIT":
        lines.append("⚠️  Homepage warm nie je HIT — skontroluj page-cache / import thrash.")
    if not home.get("ok"):
        lines.append("🛑 Homepage nie je 200 — jidoka.")
    if not customer_ok:
        lines.append("🛑 Customer paths FAIL — oprav pred claimom „beh so zákazníkmi“.")
    if modes["full"] > modes["cheap"] and modes["sampled"] >= 20:
        lines.append("ℹ️  Viac `full` než `cheap` v sample — nové importy majú ísť cheap.")
    ok = bool(home.get("ok")) and customer_ok
    return "\n".join(lines) + "\n", ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-report", action="store_true")
    ap.add_argument("--strict", action="store_true", help="Exit 2 ak homepage/customer paths padnú")
    args = ap.parse_args()
    text, ok = build_report()
    print(text)
    if args.write_report:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        path = REPORTS / f"ops-cost-{stamp}.md"
        path.write_text(text, encoding="utf-8")
        # stable latest pointer
        (REPORTS / "ops-cost-latest.md").write_text(text, encoding="utf-8")
        LOG.info("Wrote %s", path)
    if args.strict and not ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
