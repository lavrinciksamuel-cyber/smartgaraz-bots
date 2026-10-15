#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
SYNC TAXONÓMIE — vytvorí pevný strom + remap produktov (bez plného AI refresh)
===============================================================================

1) Zabezpečí existenciu celého CATEGORY_TREE vo WooCommerce
2) Prejde produkty a priradí leaf kategóriu:
   - keyword rules → legacy map → (voliteľne) AI categorize
3) Voliteľne skryje / vyprázdni junk kategórie mimo stromu

  python sync_taxonomy.py --ensure-only
  python sync_taxonomy.py --limit 200
  python sync_taxonomy.py --limit 200 --use-ai
  python sync_taxonomy.py --status draft --supplier wurth --limit 50
  python sync_taxonomy.py --hide-junk --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_engine import Config, WooCommerceClient, setup_logging  # noqa: E402
from taxonomy import (  # noqa: E402
    CATEGORY_TREE,
    PART_LEAVES,
    all_allowed_names,
    format_category_tree,
    map_legacy_category,
    match_keyword_category,
    sanitize_category_path,
)

LOGGER = logging.getLogger("sync_taxonomy")


def ensure_full_tree(wc: WooCommerceClient, dry_run: bool = False) -> Dict[str, int]:
    """Vytvorí všetky hlavné + podkategórie (+ 3. úroveň PART_LEAVES)."""
    leaf_map: Dict[str, int] = {}
    LOGGER.info("Zabezpečujem pevný strom:\n%s", format_category_tree())
    for main, subs in CATEGORY_TREE.items():
        path = [main]
        if dry_run:
            LOGGER.info("[dry-run] ensure %s", " > ".join(path))
            leaf_id = 0
        else:
            leaf_id = wc.ensure_category_path(path) or 0
        leaf_map[main] = leaf_id
        for sub in subs:
            full = [main, sub]
            key = f"{main}>{sub}"
            if dry_run:
                LOGGER.info("[dry-run] ensure %s", " > ".join(full))
                leaf_map[key] = 0
            else:
                leaf_map[key] = wc.ensure_category_path(full) or 0
                LOGGER.info("OK  %s  (id=%s)", " > ".join(full), leaf_map[key])
            for leaf in PART_LEAVES.get(sub) or []:
                triple = [main, sub, leaf]
                tkey = f"{main}>{sub}>{leaf}"
                if dry_run:
                    LOGGER.info("[dry-run] ensure %s", " > ".join(triple))
                    leaf_map[tkey] = 0
                else:
                    leaf_map[tkey] = wc.ensure_category_path(triple) or 0
                    LOGGER.info("OK  %s  (id=%s)", " > ".join(triple), leaf_map[tkey])
    return leaf_map


def resolve_path_for_product(
    product: Dict[str, Any],
    use_ai: bool,
    claude: Any = None,
) -> Optional[List[str]]:
    import html as html_lib

    name = product.get("name") or ""
    cats = product.get("categories") or []
    raw = {
        "name": name,
        "sku": product.get("sku") or "",
        "brand": "",
        "description": product.get("description") or "",
        "short_description": product.get("short_description") or "",
    }
    for m in product.get("meta_data") or []:
        if isinstance(m, dict) and m.get("key") == "supplier_url":
            raw["supplier_url"] = m.get("value") or ""
            break

    # 1) Keyword
    kw = match_keyword_category(raw)
    if kw:
        return kw

    # 2) Legacy remap z aktuálnych WC kategórií (nie dump „Auto chémia“)
    for c in cats:
        cname = html_lib.unescape((c.get("name") or "").strip())
        if cname.lower() in {"auto chémia", "auto chemia"}:
            continue
        legacy = map_legacy_category(cname)
        if legacy:
            return legacy

    # 3) Už je v strome? — ponechaj leaf (main+sub ak je)
    allowed = {n.lower() for n in all_allowed_names()}
    in_tree = []
    for c in cats:
        cname = html_lib.unescape((c.get("name") or "").strip())
        if cname.lower() in allowed:
            in_tree.append({**c, "name": cname})
    if in_tree:
        # Preferuj podkategóriu (nie top-level)
        mains = {m.lower() for m in CATEGORY_TREE.keys()}
        subs = [c for c in in_tree if (c.get("name") or "").strip().lower() not in mains]
        pick = (subs or in_tree)[0]
        pick_name = (pick.get("name") or "").strip()
        for main, sublist in CATEGORY_TREE.items():
            if pick_name == main:
                return sanitize_category_path([main, sublist[0]] if sublist else [main])
            if pick_name in sublist:
                return [main, pick_name]
            for leaf in PART_LEAVES.get(pick_name) or []:
                if pick_name == leaf:
                    return sanitize_category_path([main, pick_name])
        # Leaf 3. úrovne (Svetlomety & svetlá, …)
        for main, sublist in CATEGORY_TREE.items():
            for sub in sublist:
                if pick_name in (PART_LEAVES.get(sub) or []):
                    return [main, sub, pick_name]
                if pick_name == sub:
                    return [main, sub]
        return sanitize_category_path([pick_name])

    # 4) AI (voliteľné)
    if use_ai and claude is not None:
        return claude.categorize(raw)

    return None


def hide_junk_categories(wc: WooCommerceClient, dry_run: bool = False) -> int:
    """Nastaví display=none na kategórie mimo stromu (nevymaže — bezpečnejšie)."""
    import html as html_lib

    allowed = {n.lower() for n in all_allowed_names()}
    # WooCommerce API často vráti &amp; namiesto &
    allowed |= {html_lib.unescape(n).lower() for n in allowed}
    protected = {"uncategorized", "nezaradené"}
    page = 1
    hidden = 0
    while True:
        resp = wc.api.get(
            "products/categories",
            params={"per_page": 100, "page": page, "hide_empty": False},
        )
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for cat in data:
            name = html_lib.unescape((cat.get("name") or "").strip())
            slug = (cat.get("slug") or "").strip().lower()
            if name.lower() in allowed or slug in protected or name.lower() in protected:
                # Obnov display default ak sme ju omylom skryli (HTML entity mismatch)
                if cat.get("display") == "none" and name.lower() in allowed:
                    LOGGER.info("Obnovujem povolenú kategóriu id=%s '%s'", cat["id"], name)
                    if not dry_run:
                        wc.api.put(f"products/categories/{cat['id']}", {"display": "default"})
                continue
            count = int(cat.get("count") or 0)
            if cat.get("display") == "none" and count == 0:
                continue
            LOGGER.info(
                "Junk kategória id=%s '%s' (count=%s) → display=none",
                cat["id"],
                name,
                count,
            )
            if not dry_run:
                wc.api.put(f"products/categories/{cat['id']}", {"display": "none"})
            hidden += 1
        page += 1
    return hidden


def _supplier_of(product: Dict[str, Any]) -> str:
    for m in product.get("meta_data") or []:
        if isinstance(m, dict) and m.get("key") == "_supplier_source":
            return str(m.get("value") or "").strip().lower()
    return ""


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Sync železnej taxonómie SmartGaráž")
    parser.add_argument("--ensure-only", action="store_true", help="Len vytvor strom, bez remap produktov")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--use-ai", action="store_true", help="Ak keyword/legacy nestačí, použij Claude")
    parser.add_argument("--hide-junk", action="store_true", help="Skry kategórie mimo stromu")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument(
        "--status",
        default="any",
        choices=("publish", "draft", "any"),
        help="WC status produktov (default any)",
    )
    parser.add_argument(
        "--supplier",
        default="",
        help="Filter _supplier_source (napr. wurth)",
    )
    parser.add_argument(
        "--skip-ensure",
        action="store_true",
        help="Preskoč create-full-tree (len remap; rýchle dávky draftov)",
    )
    args = parser.parse_args()

    config = Config()
    missing = [m for m in config.validate() if m.startswith("WC_")]
    if args.use_ai:
        missing = config.validate()
    if missing:
        LOGGER.error("Chýbajú env: %s", ", ".join(missing))
        sys.exit(1)

    wc = WooCommerceClient(config)
    if not args.skip_ensure:
        ensure_full_tree(wc, dry_run=args.dry_run)
    else:
        LOGGER.info("Preskakujem ensure_full_tree (--skip-ensure).")

    if args.hide_junk:
        n = hide_junk_categories(wc, dry_run=args.dry_run)
        LOGGER.info("Skrytých junk kategórií: %s", n)

    if args.ensure_only:
        LOGGER.info("Hotovo (--ensure-only).")
        return

    claude = None
    if args.use_ai:
        from ai_engine import ClaudeContentEngine  # noqa: WPS433

        claude = ClaudeContentEngine(config)

    updated = skipped = failed = already = 0
    page = 1
    scanned = 0
    supplier_filter = (args.supplier or "").strip().lower()

    while scanned < args.limit:
        batch = min(50, args.limit - scanned)
        params = {
            "per_page": batch,
            "page": page,
            "status": args.status,
            "orderby": "id",
            "order": "desc",
        }
        resp = wc.api.get("products", params=params)
        products = resp.json()
        if not isinstance(products, list) or not products:
            break

        for product in products:
            scanned += 1
            if supplier_filter and _supplier_of(product) != supplier_filter:
                continue
            pid = product["id"]
            path = resolve_path_for_product(product, use_ai=args.use_ai, claude=claude)
            if not path:
                LOGGER.warning(
                    "ID %s '%s' — neviem zaradiť (použi --use-ai)",
                    pid,
                    product.get("name"),
                )
                skipped += 1
                continue

            leaf_id = None if args.dry_run else wc.ensure_category_path(path)
            current_ids = {c.get("id") for c in (product.get("categories") or [])}
            if leaf_id and leaf_id in current_ids and len(current_ids) == 1:
                already += 1
                continue

            LOGGER.info(
                "ID %s '%s' → %s",
                pid,
                (product.get("name") or "")[:60],
                " > ".join(path),
            )
            if args.dry_run:
                updated += 1
                continue
            try:
                wc.api.put(f"products/{pid}", {"categories": [{"id": leaf_id}]})
                updated += 1
                time.sleep(args.sleep)
            except Exception as exc:  # noqa: PERF203
                LOGGER.error("Zlyhalo ID %s: %s", pid, exc)
                failed += 1

        page += 1

    LOGGER.info(
        "Hotovo. scanned=%s updated=%s already_ok=%s skipped=%s failed=%s",
        scanned,
        updated,
        already,
        skipped,
        failed,
    )


if __name__ == "__main__":
    main()
