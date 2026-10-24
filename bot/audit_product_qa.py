#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CYKLICKÝ QA AUDIT produktov — obrázok + text + ks/ml.

Kontroluje publish katalóg (deterministicky, bez Claude):
  - obrázok: chýba / placeholder / generický stock / mismatch filename rodiny
  - text: cyrilika, Würth B2B dump (Opis Použitie…), prázdny short
  - balenie: oleje/kvapaliny bez ml/l; kusovka kde vieme vytiahnuť (N ks) a chýba v názve
  - taxonómia: stará Auto chémia / mimo strom

  python bot/audit_product_qa.py                  # full report
  python bot/audit_product_qa.py --fix --limit 40 # heijunka auto-oprava
  python bot/audit_product_qa.py --status publish --page-start 1
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from ai_engine import (  # noqa: E402
    Config,
    HDImageFinder,
    WooCommerceClient,
    build_sk_product_name,
    build_template_content,
    contains_cyrillic,
    extract_pack_piece_label,
    sanitize_sk_copy,
    setup_logging,
    strip_supplier_catalog_junk,
    wc_product_to_raw_data,
)
from taxonomy import match_keyword_category  # noqa: E402

LOGGER = logging.getLogger("audit_product_qa")

JUNK_MARKERS = (
    "Opis Použitie",
    "Zobraziť viac",
    "Zobraziť menej",
    "Ďalší tovar",
    "Katalóg spoločnosti Würth",
    "Hárky údajov",
    "Objednávacie číslo Würth",
    "Objednávacie císlo Würth",
    "Zahrnuté v dodávke",
)

LEGACY_BAD_CATS = {"auto chémia", "autochemia", "uncategorized", "nezaradené"}

VOLUME_HINT = re.compile(
    r"\b(olej|oil|kvapalin|fluid|adblue|chladiv|antifreeze|brake fluid|"
    r"aditív|aditiv|cleaner|čistič|cistic|sprej|spray|flush|booster)\b",
    re.I,
)
PCS_HINT = re.compile(
    r"\b(visačk|visack|nálep|nalep|krúžok|kruzok|filter|filtre|doštič|"
    r"dostick|kotúč|kotuc|skrutk|tesnen|gasket|žiarov|ziarov|stierač|stierac|"
    r"senzor|čidlo|cidlo|hadic|adaptér|adapter)\b",
    re.I,
)


def _blob(product: Dict[str, Any]) -> str:
    return " ".join(
        str(product.get(k) or "")
        for k in ("name", "short_description", "description")
    )


def _cat_names(product: Dict[str, Any]) -> List[str]:
    return [html.unescape((c.get("name") or "").strip()) for c in (product.get("categories") or [])]


def _has_volume_label(text: str) -> bool:
    return bool(
        re.search(r"\b\d{1,4}(?:[.,]\d+)?\s*(?:ml|l|liter|litrov)\b", text or "", re.I)
        or re.search(r"\(\d{1,4}(?:[.,]\d+)?\s*(?:ml|l)\)", text or "", re.I)
    )


def _has_pcs_label(text: str) -> bool:
    return bool(re.search(r"\(\d{1,5}\s*ks\)|\b\d{1,5}\s*ks\b", text or "", re.I))


def audit_one(product: Dict[str, Any], finder: HDImageFinder) -> Dict[str, Any]:
    """Vráti issues[] + severity (high/med/low)."""
    pid = product["id"]
    name = product.get("name") or ""
    sku = product.get("sku") or ""
    short = product.get("short_description") or ""
    desc = product.get("description") or ""
    blob = _blob(product)
    cats = _cat_names(product)
    images = [img.get("src", "") for img in (product.get("images") or []) if img.get("src")]
    raw = wc_product_to_raw_data(product)

    issues: List[str] = []

    # --- image ---
    if not images:
        issues.append("image:missing")
    else:
        if any(finder._looks_like_placeholder(u) for u in images):
            issues.append("image:placeholder")
        if any(finder._looks_like_generic_stock(u) for u in images):
            issues.append("image:generic_stock")
        if any(finder._image_mismatches_product(u, raw) for u in images):
            issues.append("image:family_mismatch")

    # --- text ---
    if contains_cyrillic(blob):
        issues.append("text:cyrillic")
    seo_blob = ""
    for m in product.get("meta_data") or []:
        if m.get("key") in ("rank_math_description", "rank_math_title"):
            seo_blob += " " + str(m.get("value") or "")
    if any(m in blob or m in seo_blob for m in JUNK_MARKERS):
        issues.append("text:supplier_junk")
    short_plain = re.sub(r"<[^>]+>", " ", short)
    if len(short_plain.strip()) < 40:
        issues.append("text:short_thin")

    # --- pack / volume ---
    pack = extract_pack_piece_label(raw)
    if VOLUME_HINT.search(name) and not _has_volume_label(name):
        # oleje/chémia bez ml/l v názve
        if not _has_volume_label(blob):
            issues.append("pack:missing_volume")
        else:
            issues.append("pack:volume_not_in_name")
    if pack and pack.lower() not in name.lower():
        issues.append("pack:pcs_not_in_name")
    elif PCS_HINT.search(name) and not pack and not _has_pcs_label(name):
        # kusovka bez (N ks) — soft, len ak vieme že by mala mať
        if re.search(r"\bx\s*\d+\s*ks\b", blob, re.I):
            issues.append("pack:pcs_extractable")

    # --- taxonomy ---
    cat_l = {c.lower() for c in cats}
    if not cats or cat_l & LEGACY_BAD_CATS:
        issues.append("tax:legacy_or_empty")
    expected = match_keyword_category(raw)
    if expected and expected[-1].lower() not in cat_l:
        issues.append(f"tax:mismatch>{expected[-1]}")

    high = {"image:missing", "image:placeholder", "image:family_mismatch", "text:cyrillic", "text:supplier_junk"}
    severity = "ok"
    if issues:
        severity = "high" if any(i.split(">")[0] in high or i in high for i in issues) else "med"

    return {
        "id": pid,
        "sku": sku,
        "name": name[:80],
        "permalink": product.get("permalink") or "",
        "issues": issues,
        "severity": severity,
        "pack": pack,
        "expected_tax": " › ".join(expected) if expected else "",
        "cats": " › ".join(cats),
        "image": images[0] if images else "",
    }


def _fix_one(wc: WooCommerceClient, product: Dict[str, Any], row: Dict[str, Any]) -> List[str]:
    """Auto-opraviteľné veci. Vracia zoznam aplikovaných akcií."""
    actions: List[str] = []
    issues = set(row["issues"])
    raw = wc_product_to_raw_data(product)
    payload: Dict[str, Any] = {}

    # Text: cyrilika + junk → sanitize / strip + cheap template
    if "text:cyrillic" in issues or "text:supplier_junk" in issues or "text:short_thin" in issues:
        raw["short_description"] = strip_supplier_catalog_junk(
            sanitize_sk_copy(raw.get("short_description") or "")
        )
        raw["description"] = strip_supplier_catalog_junk(
            sanitize_sk_copy(raw.get("description") or "")
        )
        path = match_keyword_category(raw)
        content = build_template_content(raw, path)
        payload["name"] = content["product_name_sk"]
        short = content["short_description"]
        if short and "<" not in short:
            short = f"<p>{html.escape(short)}</p>"
        payload["short_description"] = short
        payload["description"] = content["full_description"]
        actions.append("rebuild_copy")
        seo = content.get("seo_rank_math") or {}
        meta_data = []
        if seo.get("focus_keyword"):
            meta_data.append({"key": "rank_math_focus_keyword", "value": seo["focus_keyword"]})
        if seo.get("meta_title"):
            meta_data.append({"key": "rank_math_title", "value": seo["meta_title"]})
        if seo.get("meta_description"):
            meta_data.append({"key": "rank_math_description", "value": seo["meta_description"]})
        if meta_data:
            payload["meta_data"] = meta_data
            actions.append("refresh_seo")
        if path:
            ids = []
            for i in range(len(path)):
                cid = wc.ensure_category_path(path[: i + 1])
                if cid:
                    ids.append(cid)
            if ids:
                payload["categories"] = [{"id": i} for i in ids]
                actions.append("set_taxonomy")

    # Pack pcs do názvu
    if "pack:pcs_not_in_name" in issues or "pack:pcs_extractable" in issues:
        name = payload.get("name") or product.get("name") or ""
        pack = row.get("pack") or extract_pack_piece_label(raw)
        if pack and pack.lower() not in name.lower():
            payload["name"] = f"{name} {pack}".strip()
            actions.append("add_pack_pcs")

    # Volume do názvu (ak je v blob)
    if "pack:volume_not_in_name" in issues:
        name = payload.get("name") or product.get("name") or ""
        fixed = build_sk_product_name({**raw, "name": name})
        if fixed != name:
            payload["name"] = fixed
            actions.append("normalize_volume_name")

    # Tax only (bez text rebuild)
    if "tax:legacy_or_empty" in issues or any(i.startswith("tax:mismatch") for i in issues):
        if "set_taxonomy" not in actions:
            path = match_keyword_category(raw)
            if path:
                ids = []
                for i in range(len(path)):
                    cid = wc.ensure_category_path(path[: i + 1])
                    if cid:
                        ids.append(cid)
                if ids:
                    payload["categories"] = [{"id": i} for i in ids]
                    actions.append("set_taxonomy")

    # Cyrillic-only sanitize without full rebuild
    if "text:cyrillic" in issues and "rebuild_copy" not in actions:
        for field in ("name", "short_description", "description"):
            val = product.get(field) or ""
            cleaned = sanitize_sk_copy(val)
            if cleaned != val:
                payload[field] = cleaned
                actions.append(f"sanitize_{field}")

    if payload:
        wc.api.put(f"products/{product['id']}", payload).json()
    return actions


def write_report(rows: List[Dict[str, Any]], path: Path, scanned: int, fixed: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_issue: Counter = Counter()
    for r in rows:
        for i in r["issues"]:
            by_issue[i.split(">")[0]] += 1
    high = [r for r in rows if r["severity"] == "high"]
    med = [r for r in rows if r["severity"] == "med"]

    lines = [
        f"# Product QA audit — {now}",
        "",
        f"- Scanned publish: **{scanned}**",
        f"- With issues: **{len(rows)}** (high={len(high)}, med={len(med)})",
        f"- Auto-fixed this run: **{fixed}**",
        "",
        "## Issue counts",
        "",
    ]
    for k, v in by_issue.most_common():
        lines.append(f"- `{k}`: {v}")
    lines += ["", "## High severity (max 40)", ""]
    for r in high[:40]:
        lines.append(
            f"- #{r['id']} `{r['sku']}` {r['name']} — {', '.join(r['issues'])}"
        )
        if r.get("permalink"):
            lines.append(f"  - {r['permalink']}")
    lines += ["", "## Medium (max 30)", ""]
    for r in med[:30]:
        lines.append(
            f"- #{r['id']} `{r['sku']}` {r['name']} — {', '.join(r['issues'])}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Report → %s", path)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Cyclic product QA: image + text + pack")
    parser.add_argument("--status", default="publish")
    parser.add_argument(
        "--limit-scan",
        type=int,
        default=400,
        help="Max produktov na scan (0 = až 80 strán; default 400 = heijunka)",
    )
    parser.add_argument("--fix", action="store_true", help="Auto-fix heijunka batch")
    parser.add_argument("--limit", type=int, default=40, help="Max auto-fixes per run")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument(
        "--report",
        default="",
        help="Report path (default bot/reports/product-qa-YYYYMMDD.md)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = Config()
    missing = config.validate()
    if missing:
        LOGGER.error("Chýbajú env: %s", ", ".join(missing))
        sys.exit(1)

    wc = WooCommerceClient(config)
    finder = HDImageFinder(config, wc)

    rows: List[Dict[str, Any]] = []
    by_id: Dict[int, Dict[str, Any]] = {}
    scanned = 0
    page = 1
    while True:
        resp = wc.api.get(
            "products",
            params={
                "status": args.status,
                "per_page": 100,
                "page": page,
                "orderby": "id",
                "order": "asc",
            },
        )
        products = resp.json()
        if not isinstance(products, list) or not products:
            break
        for p in products:
            scanned += 1
            by_id[int(p["id"])] = p
            row = audit_one(p, finder)
            if row["issues"]:
                rows.append(row)
            if args.limit_scan and scanned >= args.limit_scan:
                break
        if args.limit_scan and scanned >= args.limit_scan:
            break
        page += 1
        if page > 80:
            break

    rows.sort(key=lambda r: (0 if r["severity"] == "high" else 1, r["id"]))

    fixed = 0
    if args.fix:
        import time

        for row in rows:
            if fixed >= args.limit:
                break
            # Image mismatch/missing — necháme na force-image (DDG/API); tu text/tax/pack
            autoable = [
                i
                for i in row["issues"]
                if i.startswith("text:")
                or i.startswith("pack:")
                or i.startswith("tax:")
            ]
            if not autoable:
                continue
            if args.dry_run:
                LOGGER.info("dry-run would fix #%s %s %s", row["id"], row["sku"], autoable)
                fixed += 1
                continue
            product = by_id.get(int(row["id"])) or wc.api.get(f"products/{row['id']}").json()
            actions = _fix_one(wc, product, row)
            if actions:
                LOGGER.info("✅ #%s %s → %s", row["id"], row["sku"], ", ".join(actions))
                fixed += 1
                time.sleep(args.sleep)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    report_path = Path(args.report) if args.report else ROOT / "reports" / f"product-qa-{stamp}.md"
    write_report(rows, report_path, scanned, fixed)
    LOGGER.info(
        "Done. scanned=%d issues=%d high=%d fixed=%d",
        scanned,
        len(rows),
        sum(1 for r in rows if r["severity"] == "high"),
        fixed,
    )


if __name__ == "__main__":
    main()
