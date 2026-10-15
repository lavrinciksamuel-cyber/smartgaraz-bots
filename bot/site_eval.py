#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Celkové vyhodnotenie stránky pre Cursor (PC / iPhone).

Spúšťa search_insights + rýchly live homepage smoke.
  python site_eval.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# Reuse helpers
sys.path.insert(0, str(ROOT))
from search_insights import catalog_snapshot, env, fetch_search_insights, render_md  # noqa: E402


def _timed_get(url: str, timeout: int = 45) -> tuple[requests.Response, float]:
    """GET + elapsed seconds (proxy for TTFB+body; hosting rarely exposes true TTFB)."""
    t0 = datetime.now(timezone.utc)
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "SmartGaraz-SiteEval/1.0"})
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    return r, elapsed


def homepage_smoke() -> dict:
    base = env("WC_URL").rstrip("/")
    url = base + "/"
    r, elapsed = _timed_get(url)
    html = r.text or ""
    # Second hit — warm (ak hosting/object cache drží)
    _, warm = _timed_get(url)
    shop_elapsed = None
    try:
        _, shop_elapsed = _timed_get(base + "/obchod/", timeout=60)
    except Exception:
        shop_elapsed = None
    return {
        "status": r.status_code,
        "elapsed_s": round(elapsed, 2),
        "warm_s": round(warm, 2),
        "shop_s": round(shop_elapsed, 2) if shop_elapsed is not None else None,
        "html_kb": round(len(html) / 1024, 1),
        "has_sg_home": 'data-sg-home="' in html or "sg-home v" in html,
        "has_old_elementor_hero": "SMARTGARÁŽ E-SHOP" in html,
        "has_ai_search": "sg-ai-search" in html or "data-sg-ai-search" in html,
        # Skutočný Fibo widget — nie náš starý selector string v hub JS.
        "has_fibo": (
            "dgwt-wcas-search-wrapp" in html
            or "dgwt-wcas-sf-wrapp" in html
            or 'class="dgwt-wcas' in html
        ),
        "has_brand_blue": "#0052CC" in html or "0052cc" in html.lower() or "sg-modern" in html,
        "title": (re.search(r"<title>(.*?)</title>", html, re.I | re.S) or [None, ""])[1].strip()[:120],
    }


def main() -> int:
    if not env("WC_URL"):
        raise SystemExit("Chýba WC_URL")

    smoke = homepage_smoke()
    from customer_paths_smoke import check_paths  # noqa: E402

    customer = check_paths(env("WC_URL").rstrip("/"))
    insights = fetch_search_insights()
    catalog = catalog_snapshot()
    body = render_md(insights, catalog)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Site evaluation — {now}",
        "",
        "## Homepage smoke",
        f"- HTTP: {smoke['status']}",
        f"- Title: {smoke['title']}",
        f"- Load cold/warm: {smoke.get('elapsed_s')}s / {smoke.get('warm_s')}s"
        + (f" | shop {smoke.get('shop_s')}s" if smoke.get("shop_s") is not None else ""),
        f"- HTML size: {smoke.get('html_kb')} KB",
        f"- Modern home (sg-home): {'ANO' if smoke['has_sg_home'] else 'NIE'}",
        f"- Starý Elementor hero: {'ANO (treba 1.4.3+/hijack)' if smoke['has_old_elementor_hero'] else 'nie'}",
        f"- AI search markup: {'ANO' if smoke['has_ai_search'] else 'NIE (nahraj 1.5.0)'}",
        f"- FiboSearch ešte v DOM: {'ANO — vymeň / deaktivuj' if smoke['has_fibo'] else 'nie'}",
        f"- Brand CSS signal: {'ANO' if smoke['has_brand_blue'] else 'slabé'}",
        "",
        "## Customer paths",
        f"- Verdikt: {'OK' if customer.get('ok') else 'FAIL'}",
        f"- Failed: {', '.join(customer.get('failed') or []) or '—'}",
        *[
            f"- {c.get('name')}: HTTP {c.get('status', '—')} "
            f"{'ok' if c.get('ok') else 'NO'}"
            + (f" ({c.get('warn') or c.get('error')})" if (c.get('warn') or c.get('error')) else "")
            for c in (customer.get("checks") or [])
        ],
        "",
        body,
        "",
        "## Verdikt (auto)",
    ]

    score = 0
    if smoke["has_sg_home"]:
        score += 2
    if smoke["has_ai_search"]:
        score += 2
    if not smoke["has_old_elementor_hero"]:
        score += 1
    if not smoke["has_fibo"]:
        score += 1
    if catalog.get("pct_with_price", 0) > 80:
        score += 1
    if catalog.get("pct_with_fitment_make", 0) > 10:
        score += 1
    if customer.get("ok"):
        score += 1

    if smoke.get("status") != 200:
        lines.append(
            f"**JIDOKA** homepage HTTP {smoke.get('status')} — žiadny ďalší import/publish claim."
        )
    elif not customer.get("ok"):
        lines.append(
            f"**JIDOKA customer paths** fail: {', '.join(customer.get('failed') or [])}. "
            "Pozri `bot/CUSTOMER_OPS.md`."
        )
    elif score >= 6:
        lines.append(
            f"**Pripravené na zákaznícky beh** (score {score}/9). "
            "Drž quiet + publish ready + SU2 mail loop."
        )
    elif score >= 4:
        lines.append(f"**Čiastočne OK** (score {score}/9). Doplož homepage / Fibo / customer paths.")
    else:
        lines.append(f"**Treba zásah** (score {score}/9). Najprv frontend (home + AI search), potom katalóg.")

    out = ROOT / "reports" / f"site-eval-{datetime.now().strftime('%Y%m%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")

    if smoke.get("status") != 200 or not customer.get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
