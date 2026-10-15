#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Search + catalog insights for Cursor evaluation.

Usage:
  python search_insights.py
  python search_insights.py --out bot/reports/search-insights.md

Needs WC_* in bot/.env. Optional SG_INSIGHTS_KEY (from WP option sg_insights_key).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

KNOWN_SUPPLIERS = ["KENT", "Metabond", "PRO-TEC", "Würth", "LKQ", "Szekop", "BLUECHEM", "MOPAR", "Pennzoil"]


def _is_retired_category(cat: Dict[str, Any]) -> bool:
    """Soft-retired / orphan duplicate categories — nie sú signál na doplnenie."""
    name = (cat.get("name") or "").lower()
    slug = (cat.get("slug") or "").lower()
    return (
        slug.startswith("zz-orphan")
        or "[orphan]" in name
        or name.startswith("[orphan]")
    )


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def wc_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = env("WC_URL").rstrip("/") + "/wp-json/wc/v3/" + path.lstrip("/")
    auth = (env("WC_CONSUMER_KEY"), env("WC_CONSUMER_SECRET"))
    r = requests.get(url, params=params or {}, auth=auth, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_search_insights() -> Optional[Dict[str, Any]]:
    key = env("SG_INSIGHTS_KEY")
    if not key:
        return None
    base = env("WC_URL").rstrip("/")
    r = requests.get(
        f"{base}/wp-json/smartgaraz/v1/search-insights",
        headers={"X-SG-Insights-Key": key},
        timeout=60,
    )
    if r.status_code == 401 or r.status_code == 403:
        return {"error": f"insights auth failed ({r.status_code}) — skontroluj SG_INSIGHTS_KEY"}
    r.raise_for_status()
    return r.json()


def catalog_snapshot() -> Dict[str, Any]:
    cats = wc_get("products/categories", {"per_page": 100, "hide_empty": True})
    top = sorted(
        [
            c
            for c in cats
            if isinstance(c, dict)
            and int(c.get("parent") or 0) == 0
            and not _is_retired_category(c)
        ],
        key=lambda c: int(c.get("count") or 0),
        reverse=True,
    )[:12]

    # Sample recent products for quality / fitment
    products = wc_get("products", {"per_page": 50, "orderby": "date", "order": "desc", "status": "publish"})
    with_image = 0
    with_price = 0
    with_fit = 0
    for p in products if isinstance(products, list) else []:
        if p.get("images"):
            with_image += 1
        if p.get("price") or p.get("regular_price"):
            with_price += 1
        meta = {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}
        if meta.get("_sg_fit_make"):
            with_fit += 1

    n = max(len(products) if isinstance(products, list) else 0, 1)
    return {
        "top_categories": [{"name": c.get("name"), "count": c.get("count")} for c in top],
        "sample_size": n,
        "pct_with_image": round(100 * with_image / n, 1),
        "pct_with_price": round(100 * with_price / n, 1),
        "pct_with_fitment_make": round(100 * with_fit / n, 1),
    }


def contract_hints(zero_queries: List[Any], top_cats: List[Dict[str, Any]]) -> List[str]:
    hints: List[str] = []
    zero_text = " ".join(str(z.get("query_text") or z.get("query") or "") for z in (zero_queries or [])).lower()
    for brand in KNOWN_SUPPLIERS:
        if brand.lower() in zero_text and brand not in ("KENT", "Metabond", "PRO-TEC"):
            hints.append(f"Dopyty bez výsledkov spomínajú **{brand}** — over sklad / zmluvu.")
    low = [
        c
        for c in top_cats
        if int(c.get("count") or 0) < 5 and not _is_retired_category(c)
    ]
    for c in low[:5]:
        hints.append(f"Kategória **{c.get('name')}** má málo produktov ({c.get('count')}) — doplniť z LKQ/Würth/Szekop.")
    if "náradie" in json.dumps(top_cats, ensure_ascii=False).lower() or "vybavenie" in json.dumps(top_cats, ensure_ascii=False).lower():
        hints.append("Skontroluj pokrytie **Náradie & Vybavenie** pred bulk Würth/Szekop.")
    if not hints:
        hints.append("Zatiaľ málo search dát — po nasadení AI search nechaj 7 dní bežať logy, potom znova.")
        hints.append("Pred tisíckami dielov: paginácia crawlu + SKU upsert + fitment z feedu (už čiastočne v ai_engine).")
    return hints


def render_md(insights: Optional[Dict[str, Any]], catalog: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Search & catalog insights — {now}",
        "",
        "## Katalóg (sample)",
        f"- Sample size: {catalog.get('sample_size')}",
        f"- S obrázkom: {catalog.get('pct_with_image')}%",
        f"- S cenou: {catalog.get('pct_with_price')}%",
        f"- S fitment make: {catalog.get('pct_with_fitment_make')}%",
        "",
        "### Top kategórie",
    ]
    for c in catalog.get("top_categories") or []:
        lines.append(f"- {c.get('name')}: {c.get('count')}")

    zero: List[Any] = []
    if insights and not insights.get("error"):
        lines += ["", "## Search (30 dní)"]
        lines.append("### Top dopyty")
        for row in (insights.get("top_queries") or [])[:20]:
            if isinstance(row, dict):
                lines.append(f"- `{row.get('query_text')}` ×{row.get('c')} (avg hits {row.get('avg_hits')})")
        lines.append("### Zero results")
        zero = list(insights.get("zero_results") or [])
        for row in zero[:20]:
            if isinstance(row, dict):
                lines.append(f"- `{row.get('query_text')}` ×{row.get('c')}")
        lines.append("### Kliky")
        for row in insights.get("clicks") or []:
            if isinstance(row, dict):
                lines.append(f"- {row.get('clicked_type')}: {row.get('c')}")
        fit = insights.get("fitment") or {}
        lines.append(f"- Produkty s `_sg_fit_make` (DB): {fit.get('with_make')}")
    elif insights and insights.get("error"):
        lines += ["", f"## Search insights: {insights['error']}"]
    else:
        lines += [
            "",
            "## Search insights",
            "Chýba `SG_INSIGHTS_KEY` v `.env`. Po deployi 1.5.0 skopíruj WP option `sg_insights_key` do `.env`.",
        ]

    lines += ["", "## Čo zazmluvniť / doplniť"]
    for h in contract_hints(zero, catalog.get("top_categories") or []):
        lines.append(f"- {h}")

    lines += [
        "",
        "## Poznámka pre Cursor",
        "Otvor tento report + https://smartgaraz.sk a vyhodnoť UX, search, taxonómiu, fitment coverage.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=str(ROOT / "reports" / f"search-insights-{datetime.now().strftime('%Y%m%d')}.md"),
    )
    args = parser.parse_args()

    if not env("WC_URL") or not env("WC_CONSUMER_KEY"):
        raise SystemExit("Chýba WC_URL / WC_CONSUMER_KEY v bot/.env")

    insights = fetch_search_insights()
    catalog = catalog_snapshot()
    md = render_md(insights, catalog)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
