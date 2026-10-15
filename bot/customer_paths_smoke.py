#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zákaznícke cesty — jidoka smoke (shop + SmartÚčto + Diag).

  python bot/customer_paths_smoke.py
  python bot/customer_paths_smoke.py --write-report
  python bot/customer_paths_smoke.py --base https://www.smartgaraz.sk

Exit 0 = všetky cesty OK. Exit 2 = niečo padlo (zastav linku).
Bez WC_* — len verejné URL + smartucto status.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
DEFAULT_BASE = (os.getenv("WC_URL") or "https://www.smartgaraz.sk").rstrip("/")


def _get(url: str, timeout: int = 45) -> Dict[str, Any]:
    out: Dict[str, Any] = {"url": url, "ok": False}
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0", "Cache-Control": "no-cache"},
            allow_redirects=True,
        )
        out["status"] = r.status_code
        out["bytes"] = len(r.content or b"")
        text = r.text or ""
        out["snippet"] = text[:200].replace("\n", " ")
        out["ok"] = r.status_code == 200 and out["bytes"] > 200
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


def check_paths(base: str) -> Dict[str, Any]:
    base = base.rstrip("/")
    checks: List[Dict[str, Any]] = []

    home = _get(f"{base}/")
    home["name"] = "homepage"
    home["need"] = ["sg-home", "Smart"]
    if home.get("ok"):
        body = requests.get(
            f"{base}/",
            timeout=45,
            headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0"},
        ).text
        home["has_sg_home"] = 'data-sg-home="' in body or "sg-home" in body
        if not home["has_sg_home"]:
            home["ok"] = False
            home["warn"] = "missing sg-home marker"
    checks.append(home)

    ucto = _get(f"{base}/ucto/")
    ucto["name"] = "ucto_landing"
    if ucto.get("ok"):
        body = requests.get(
            f"{base}/ucto/?v=smoke",
            timeout=45,
            headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0", "Cache-Control": "no-cache"},
        ).text
        ucto["has_nav"] = "SmartGaráž" in body and "Smart diag" in body
        ucto["has_brand"] = "SmartÚčto" in body or "Smart<span>Účto</span>" in body
        ucto["has_auth"] = ("Login" in body and "Sign up" in body) or (
            "Prihlásenie" in body and "Registrácia" in body
        )
        ucto["has_ai"] = 'id="su-ai"' in body or "su-ai__form" in body
        if not (ucto["has_nav"] and ucto["has_brand"] and ucto["has_auth"] and ucto["has_ai"]):
            ucto["ok"] = False
            ucto["warn"] = "missing ucto nav/auth/ai"
    checks.append(ucto)

    app = _get(f"{base}/ucto-app/")
    app["name"] = "ucto_app"
    checks.append(app)

    diag = _get(f"{base}/diag/")
    diag["name"] = "diag"
    if diag.get("ok"):
        body = requests.get(
            f"{base}/diag/",
            timeout=45,
            headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0"},
        ).text
        diag["has_workbench"] = "dg-paste" in body or "dg-analyze" in body
        if not diag.get("has_workbench"):
            diag["ok"] = False
            diag["warn"] = "missing diag workbench"
    checks.append(diag)

    it_page = _get(f"{base}/it-sluzby/")
    it_page["name"] = "it_services_landing"
    if it_page.get("ok"):
        body = requests.get(
            f"{base}/it-sluzby/?v=smoke",
            timeout=45,
            headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0", "Cache-Control": "no-cache"},
        ).text
        it_page["has_hero"] = "Softvérové úpravy" in body
        it_page["has_pricing"] = "sg-it__plan" in body or "Cenník" in body
        it_page["has_form"] = "sg_it_consultation" in body
        if not (it_page["has_hero"] and it_page["has_pricing"] and it_page["has_form"]):
            it_page["ok"] = False
            it_page["warn"] = "missing hero/pricing/consultation form (stale cache or undeployed page)"
    checks.append(it_page)

    status_url = f"{base}/wp-json/smartucto/v1/status"
    st: Dict[str, Any] = {"name": "smartucto_status", "url": status_url, "ok": False}
    try:
        r = requests.get(status_url, timeout=30, headers={"User-Agent": "SmartGaraz-CustomerSmoke/1.0"})
        st["status"] = r.status_code
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(data, dict):
            data = {}
        st["body"] = {
            "ok": data.get("ok"),
            "licenseReady": data.get("licenseReady"),
            "payTransferReady": data.get("payTransferReady"),
            "backupReady": data.get("backupReady"),
            "secretsSource": data.get("secretsSource"),
            "version": data.get("version"),
        }
        st["ok"] = (
            r.status_code == 200
            and bool(data.get("ok"))
            and bool(data.get("licenseReady"))
            and bool(data.get("payTransferReady"))
        )
        if not st["ok"]:
            st["warn"] = "license/pay not ready"
    except Exception as exc:
        st["error"] = str(exc)
    checks.append(st)

    # Fitment strip — query bypasses disk page cache
    fit: Dict[str, Any] = {
        "name": "shop_fitment",
        "url": f"{base}/domov/obchod/?sg_fitment_smoke=1",
        "ok": False,
    }
    try:
        r = requests.get(
            fit["url"],
            timeout=45,
            headers={
                "User-Agent": "SmartGaraz-CustomerSmoke/1.0",
                "Cache-Control": "no-cache",
            },
        )
        fit["status"] = r.status_code
        body = r.text or ""
        fit["has_generation"] = 'id="sg-fit-generation"' in body or 'name="sg_generation"' in body
        fit["has_s5"] = 'value="S5"' in body
        fit["has_rs4"] = 'value="RS4"' in body
        fit["ok"] = r.status_code == 200 and fit["has_generation"]
        if not fit["ok"]:
            fit["warn"] = "missing Generácia dropdown (stale cache or undeployed theme)"
        elif not (fit["has_s5"] and fit["has_rs4"]):
            fit["warn"] = "Generácia OK, but Audi S5/RS4 options missing"
    except Exception as exc:
        fit["error"] = str(exc)
    checks.append(fit)

    # Apply filter must not 504 — was broken by heavy meta_query OR/NOT EXISTS.
    fit_apply: Dict[str, Any] = {
        "name": "shop_fitment_apply",
        "url": (
            f"{base}/domov/obchod/"
            "?sg_make=%C5%A0koda&sg_model=Octavia&sg_generation=III"
        ),
        "ok": False,
    }
    try:
        t0 = time.time()
        r = requests.get(
            fit_apply["url"],
            timeout=35,
            headers={
                "User-Agent": "SmartGaraz-CustomerSmoke/1.0",
                "Cache-Control": "no-cache",
            },
            allow_redirects=True,
        )
        fit_apply["status"] = r.status_code
        fit_apply["seconds"] = round(time.time() - t0, 2)
        body = r.text or ""
        fit_apply["has_strip"] = 'id="sg-fit-make"' in body
        fit_apply["ok"] = (
            r.status_code == 200
            and fit_apply["has_strip"]
            and fit_apply["seconds"] < 30
        )
        if not fit_apply["ok"]:
            fit_apply["warn"] = "fitment apply slow/timeout/504 — check sg_fitment_product_query"
    except Exception as exc:
        fit_apply["error"] = str(exc)
    checks.append(fit_apply)

    # Akcie / čo sa oplatí — nie fallback „Sady“
    akcie: Dict[str, Any] = {
        "name": "shop_akcie_worth_it",
        "url": f"{base}/domov/obchod/?oplati=1",
        "ok": False,
    }
    try:
        t0 = time.time()
        r = requests.get(
            akcie["url"],
            timeout=35,
            headers={
                "User-Agent": "SmartGaraz-CustomerSmoke/1.0",
                "Cache-Control": "no-cache",
            },
            allow_redirects=True,
        )
        akcie["status"] = r.status_code
        akcie["seconds"] = round(time.time() - t0, 2)
        body = r.text or ""
        akcie["has_title"] = ("Čo sa oplatí" in body) or ("oplati=1" in body)
        # Must NOT be the old Sady category dump of repair kits.
        akcie["not_sady_cat"] = "vyhodne-sady-akcie" not in (r.url or "")
        akcie["ok"] = (
            r.status_code == 200
            and akcie["not_sady_cat"]
            and akcie["seconds"] < 30
        )
        if not akcie["ok"]:
            akcie["warn"] = "Akcie/?oplati missing or slow"
    except Exception as exc:
        akcie["error"] = str(exc)
    checks.append(akcie)

    failed = [c["name"] for c in checks if not c.get("ok")]
    return {
        "base": base,
        "ok": len(failed) == 0,
        "failed": failed,
        "checks": checks,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def render_md(result: Dict[str, Any]) -> str:
    lines = [
        f"# Customer paths smoke — {result['at']}",
        "",
        f"- Base: `{result['base']}`",
        f"- Verdikt: **{'OK' if result['ok'] else 'FAIL'}**",
    ]
    if result["failed"]:
        lines.append(f"- Failed: {', '.join(result['failed'])}")
    lines.append("")
    lines.append("| Path | HTTP | OK | Notes |")
    lines.append("|---|---:|:---:|---|")
    for c in result["checks"]:
        notes = c.get("warn") or c.get("error") or ""
        if c.get("body"):
            notes = json.dumps(c["body"], ensure_ascii=False)
        lines.append(
            f"| {c.get('name')} | {c.get('status', '—')} | "
            f"{'yes' if c.get('ok') else 'NO'} | {notes} |"
        )
    lines.append("")
    lines.append("Runbook: `bot/CUSTOMER_OPS.md`")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Customer-facing paths smoke")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--write-report", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = check_paths(args.base)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_md(result))

    if args.write_report:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        path = REPORTS / f"ops-customer-smoke-{stamp}.md"
        text = render_md(result)
        path.write_text(text, encoding="utf-8")
        (REPORTS / "ops-customer-smoke-latest.md").write_text(text, encoding="utf-8")
        print(f"Wrote {path}", file=sys.stderr)

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
