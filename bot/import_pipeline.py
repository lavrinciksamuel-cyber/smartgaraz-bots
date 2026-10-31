#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
SMARTGARÁŽ IMPORT PIPELINE — crawl ľubovoľný e-shop → AI → WooCommerce
===============================================================================

Príklady (default = cheap / 0 € Claude):
  python import_pipeline.py --supplier wurth --limit 40 --fast --in-stock
  python import_pipeline.py --supplier lkq --limit 40 --fast --in-stock \\
      --category-url 'https://www.lkq.sk/Catalog/...'
  python import_pipeline.py --url https://www.pro-tec-slovakia.sk/nejaky-produkt/

  # Full Claude (drahé) — len manuálne, malá dávka + potvrdenie:
  python import_pipeline.py --supplier pro-tec --limit 10 --full-ai --full-ai-confirm

  # Fitment pilot (LKQ) — dry-run ukáže make/OE:
  python import_pipeline.py --supplier lkq --limit 5 --dry-run \\
      --category-url 'https://www.lkq.sk/Catalog/<slug>/<path-ids>'

Každý produkt ide cez ai_engine.process_product_with_ai():
  kategorizácia → SK názov → copywriting → HD fotka → SEO/EAN
  (+ fitment meta _sg_fit_* z ProductRaw make/model/OE, ak dodávateľ dodá)

`--fast`: existujúce SKU s dobrým obsahom → len cena/sklad (bez Anthropic).
`--cheap` / AI_MODE=cheap / default: 0 € Anthropic — keyword + template, fotka len z dodávateľa,
`--full-ai`: zapne Claude (inak cheap).
  skip 20–208 L sudov. Plný AI copy neskôr len na top predaje.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_engine import (  # noqa: E402
    ClaudeContentEngine,
    Config,
    ExternalResearcher,
    HDImageFinder,
    WooCommerceClient,
    is_bulk_pack,
    is_supplier_in_stock,
    process_product_with_ai,
    setup_logging,
)
from suppliers import AVAILABLE_SUPPLIERS, get_supplier  # noqa: E402
from suppliers.generic import GenericSupplier  # noqa: E402

LOGGER = logging.getLogger("import_pipeline")


def _known_skus() -> set:
    """Všetky SKU vo WC — z HQ (/api/sync/existing-skus), fallback WC REST."""
    from hq_skus import existing
    skus, _urls, src = existing()
    global _KNOWN_SRC
    _KNOWN_SRC = src
    return set(skus)


_KNOWN_SRC = ""


def import_products(
    products,
    sleep: float = 0.3,
    fast: bool = False,
    cheap: bool = False,
    in_stock_only: bool = False,
) -> List[dict]:
    config = Config()
    missing = config.validate(require_anthropic=not cheap)
    if missing:
        raise RuntimeError(f"Chýbajú env premenné: {', '.join(missing)}")

    # Klienti 1× na beh — category cache + (voliteľne) Anthropic + SKU cache
    wc_client = WooCommerceClient(config)
    ai_engine = None if cheap else ClaudeContentEngine(config)
    researcher = None if cheap else ExternalResearcher(config)
    image_finder = HDImageFinder(config, wc_client)
    # Predhrej kategórie (1 paginácia namiesto N)
    wc_client._load_all_categories()

    results = []
    n = len(products)
    skipped_bulk = 0
    skipped_oos = 0
    for idx, product in enumerate(products, 1):
        raw = product.to_ai_dict() if hasattr(product, "to_ai_dict") else product
        LOGGER.info("--- [%d/%d] %s (%s) ---", idx, n, raw.get("name"), raw.get("source"))
        if in_stock_only and not is_supplier_in_stock(raw):
            skipped_oos += 1
            LOGGER.info(
                "⏭  Skip mimo sklad (qty=%s status=%s): %s",
                raw.get("stock_qty"),
                raw.get("stock_status"),
                raw.get("name"),
            )
            results.append({
                "name": raw.get("name"),
                "success": False,
                "skipped": True,
                "reason": "out_of_stock",
                "cheap_path": cheap,
            })
            continue
        if cheap and is_bulk_pack(raw):
            skipped_bulk += 1
            LOGGER.info(
                "⏭  Skip bulk sud/balenie (20–208 L): %s",
                raw.get("name"),
            )
            results.append({
                "name": raw.get("name"),
                "success": False,
                "skipped": True,
                "reason": "bulk_pack",
                "cheap_path": True,
            })
            continue
        try:
            from shop_guard import wait_if_slow
            wait_if_slow()
        except Exception:
            pass
        t0 = time.monotonic()
        try:
            result = process_product_with_ai(
                raw,
                config=config,
                wc_client=wc_client,
                ai_engine=ai_engine,
                researcher=researcher,
                image_finder=image_finder,
                fast=fast,
                cheap=cheap,
            )
            results.append(result)
            wc_id = result.get("wc_product_id")
            wc_status = result.get("wc_status") or "?"
            label = "PUBLIC" if wc_status == "publish" else ("KONCEPT" if wc_status == "draft" else wc_status)
            if result.get("fast_path"):
                mode = "FAST"
            elif result.get("cheap_path"):
                mode = "CHEAP"
            else:
                mode = "AI"
            LOGGER.info(
                "OK [%s %.1fs] id=%s status=%s (%s) cats=%s name=%s stock=%s",
                mode,
                time.monotonic() - t0,
                wc_id,
                wc_status,
                label,
                " > ".join(result.get("category_path") or []),
                result.get("product_name_sk") or result.get("name"),
                raw.get("stock_qty"),
            )
            if idx % 10 == 0:
                ok_n = sum(1 for r in results if r.get("success") or r.get("wc_product_id"))
                fast_n = sum(1 for r in results if r.get("fast_path"))
                cheap_n = sum(1 for r in results if r.get("cheap_path") and not r.get("skipped"))
                LOGGER.info(
                    "📊 STAV po %d ks [%s]: úspešných=%d / %d (fast=%d, cheap=%d, "
                    "bulk_skip=%d, oos_skip=%d, posledný=%s)",
                    idx,
                    raw.get("source") or "?",
                    ok_n,
                    idx,
                    fast_n,
                    cheap_n,
                    skipped_bulk,
                    skipped_oos,
                    label,
                )
        except Exception as exc:
            LOGGER.exception("Import zlyhal: %s", exc)
            results.append({"name": raw.get("name"), "success": False, "error": str(exc)})
        if idx < n and sleep > 0:
            time.sleep(sleep)
    if skipped_bulk:
        LOGGER.info("⏭  Celkom skip bulk balení: %d", skipped_bulk)
    if skipped_oos:
        LOGGER.info("⏭  Celkom skip mimo sklad: %d", skipped_oos)
    return results


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="SmartGaráž multi-supplier import")
    parser.add_argument("--supplier", choices=AVAILABLE_SUPPLIERS, help="Zdrojový e-shop/API")
    parser.add_argument("--base-url", help="Pre generic: root URL cudzieho e-shopu")
    parser.add_argument("--url", help="Import jedného produktového URL (auto Generic/Pro-Tec)")
    parser.add_argument(
        "--category-url",
        help="Pre lkq/szekop (JS/SPA weby): URL konkrétnej kategórie na katalógu dodávateľa "
        "(napr. https://www.lkq.sk/Catalog/... alebo https://szekopsk.nextis.cz/sk/katalog/...)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max produktov z katalógu")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.3,
        help="Pauza medzi produktmi (default 0.3s)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Existujúce SKU s OK obsahom → len cena/sklad (bez Anthropic)",
    )
    parser.add_argument(
        "--cheap",
        action="store_true",
        help="0 € Claude (default aj bez flagu): keyword + template, fotka z dodávateľa, skip sudov",
    )
    parser.add_argument(
        "--full-ai",
        action="store_true",
        help="Zapni Claude copy/kategorizáciu (drahé). Default je cheap/0 €.",
    )
    parser.add_argument(
        "--full-ai-confirm",
        action="store_true",
        help="Potvrdenie pre --full-ai pri --limit > 20 (ochrana peňaženky)",
    )
    parser.add_argument(
        "--in-stock",
        action="store_true",
        help="Importuj len položky na sklade u dodávateľa (stock_qty>0 / instock)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Len crawl + JSON dump, bez WC/AI")
    parser.add_argument("--dump", help="Ulož surové ProductRaw do JSON súboru")
    parser.add_argument(
        "--force-daytime",
        action="store_true",
        help="Cez deň (SK 07–20) povoliť plný live import (inak quiet — web speed)",
    )
    parser.add_argument(
        "--quiet-drip",
        action="store_true",
        help="Denný quiet drip: malá dávka (≤8) + sleep≥2s, homepage-safe",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Preskoč SKU čo už sú vo WC — vhodné na pomalé dopĺňanie (žiarovky…)",
    )
    args = parser.parse_args()
    if os.environ.get("SG_SKIP_EXISTING") == "1":
        # supplier-import (HQ): existujúce produkty sa nesmú meniť — len nové SKU.
        args.skip_existing = True
        args.fast = False

    if not args.dry_run and not args.force_daytime:
        try:
            from daytime_quiet import (
                clamp_for_quiet_drip,
                is_daytime_quiet,
                quiet_drip_enabled,
                quiet_message,
            )

            if is_daytime_quiet():
                allow_drip = args.quiet_drip and quiet_drip_enabled()
                if allow_drip:
                    capped_limit, capped_sleep = clamp_for_quiet_drip(
                        args.limit, args.sleep
                    )
                    if capped_limit != args.limit or capped_sleep != args.sleep:
                        LOGGER.info(
                            "QUIET_DRIP clamp limit %s→%s sleep %s→%s",
                            args.limit,
                            capped_limit,
                            args.sleep,
                            capped_sleep,
                        )
                    args.limit = capped_limit
                    args.sleep = capped_sleep
                    LOGGER.info(
                        "QUIET_DRIP Europe/Bratislava — limit=%s sleep=%ss (web speed)",
                        args.limit,
                        args.sleep,
                    )
                else:
                    LOGGER.error(
                        "%s | live bulk import blokovaný. "
                        "Použi --quiet-drip (pomalý drip), --dry-run, "
                        "alebo --force-daytime / SG_DAYTIME_QUIET=force_run.",
                        quiet_message(),
                    )
                    sys.exit(3)
            elif args.quiet_drip:
                capped_limit, capped_sleep = clamp_for_quiet_drip(args.limit, args.sleep)
                args.limit = capped_limit
                args.sleep = capped_sleep
                LOGGER.info(
                    "QUIET_DRIP (mimo quiet okna) — limit=%s sleep=%ss",
                    args.limit,
                    args.sleep,
                )
        except SystemExit:
            raise
        except Exception:
            pass
    elif args.quiet_drip and not args.dry_run:
        try:
            from daytime_quiet import clamp_for_quiet_drip

            args.limit, args.sleep = clamp_for_quiet_drip(args.limit, args.sleep)
        except Exception:
            args.limit = min(args.limit, 8)
            args.sleep = max(args.sleep, 2.0)

    env_cheap = (os.getenv("AI_MODE") or "").strip().lower() in {
        "cheap", "template", "budget", "zero",
    }
    # Default cheap (muda/0 €). Explicit --full-ai zapne Claude.
    cheap = True
    if env_cheap or args.cheap:
        cheap = True
    if args.full_ai:
        cheap = False
    if args.full_ai and args.limit > 20 and not args.full_ai_confirm and not args.dry_run:
        LOGGER.error(
            "STOP: --full-ai s limit=%s > 20. Použi --full-ai-confirm alebo zníž limit "
            "(alebo nechaj default cheap / AI_MODE=cheap).",
            args.limit,
        )
        sys.exit(2)
    if not cheap:
        LOGGER.warning(
            "⚠️  FULL AI MODE — Anthropic náklady. Odhad ≤%s Claude generate_content volaní.",
            args.limit,
        )

    in_stock_only = bool(args.in_stock) or (os.getenv("IMPORT_IN_STOCK") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    # Cheap autonómne: default in-stock (menej OOS balastu), vypni: IMPORT_IN_STOCK=0
    if cheap and not args.in_stock:
        env_oos = (os.getenv("IMPORT_IN_STOCK") or "").strip().lower()
        if env_oos not in {"0", "false", "no", "off"}:
            in_stock_only = True

    products = []
    if args.url:
        # Auto-detekcia podľa hostiteľa
        host = args.url.lower()
        if "pro-tec" in host:
            supplier = get_supplier("pro-tec")
        elif "metabond" in host:
            supplier = get_supplier("metabond")
        elif "jlmaditiva" in host:
            supplier = get_supplier("jlm")
        elif "wurth" in host:
            supplier = get_supplier("wurth")
        elif "lkq" in host:
            supplier = get_supplier("lkq")
        elif "szekop" in host:
            supplier = get_supplier("szekop")
        elif "efautoparts" in host or "emilfrey" in host:
            supplier = get_supplier("efautoparts")
        else:
            supplier = GenericSupplier(base_url=args.url.rsplit("/", 1)[0], source_name="generic")
        product = supplier.parse_product(args.url)
        if not product:
            LOGGER.error("Nepodarilo sa sparovať URL: %s", args.url)
            sys.exit(1)
        products = [product]
    elif args.supplier:
        supplier = get_supplier(args.supplier, base_url=args.base_url, category_url=args.category_url)
        if hasattr(supplier, "ensure_login"):
            ok = supplier.ensure_login()
            LOGGER.info("🔐 Login %s: %s", args.supplier, "OK" if ok else "skip/public")
        # Pri --in-stock crawl viac kandidátov, potom orež (cap 2× / 400 — menej muda).
        crawl_limit = args.limit
        if in_stock_only and args.limit > 0:
            crawl_limit = min(max(args.limit * 2, args.limit), 400)
        if args.skip_existing and args.limit > 0:
            # Hľadaj nové SKU hlbšie v kategórii (žiarovky drip).
            # SG_CRAWL_CAP: koľko riadkov listingu prejsť pri skip-existing (predtým napevno 400 →
            # diely za 400. riadkom auta × kategórie sa nikdy nedostali do obchodu).
            crawl_limit = min(max(args.limit * 15, 60), int(os.environ.get("SG_CRAWL_CAP", "400")))
        LOGGER.info(
            "🕷️  Crawl %s (limit=%s%s)...",
            args.supplier,
            crawl_limit,
            f", cieľ na sklade={args.limit}" if in_stock_only else "",
        )
        products = list(supplier.iter_catalog(limit=crawl_limit))
    else:
        parser.print_help()
        sys.exit(1)

    if in_stock_only:
        before = len(products)
        kept = []
        for p in products:
            raw = p.to_ai_dict() if hasattr(p, "to_ai_dict") else p
            if is_supplier_in_stock(raw if isinstance(raw, dict) else {}):
                kept.append(p)
        products = kept
        LOGGER.info("📦 Na sklade: %d / %d", len(products), before)

    if args.skip_existing and not args.dry_run:
        before = len(products)
        cfg = Config()
        wc = WooCommerceClient(cfg)
        fresh = []
        known = _known_skus()
        for p in products:
            raw = p.to_ai_dict() if hasattr(p, "to_ai_dict") else {}
            sku = str((raw.get("sku") if isinstance(raw, dict) else None) or getattr(p, "sku", "") or "").strip()
            if sku and sku in known:
                continue
            # nie je v cache → živá kontrola (len kandidáti, nie celý crawl)
            # HQ zoznam je takmer real-time a WC aj tak odmietne duplicitné SKU → bez pomalého live GET.
            if sku and _KNOWN_SRC == "wc" and wc.find_product_id_by_sku(sku):
                continue
            fresh.append(p)
        products = fresh
        LOGGER.info("📦 Nové SKU (skip-existing): %d / %d", len(products), before)

    if args.limit > 0 and (in_stock_only or args.skip_existing):
        products = products[: args.limit]

    mode_bits = []
    if args.fast:
        mode_bits.append("FAST")
    if cheap:
        mode_bits.append("CHEAP/0€")
    if in_stock_only:
        mode_bits.append("IN-STOCK")
    LOGGER.info(
        "📦 Nájdených produktov: %d%s",
        len(products),
        f" [{'+'.join(mode_bits)}]" if mode_bits else "",
    )
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump([p.to_ai_dict() for p in products], fh, ensure_ascii=False, indent=2)
        LOGGER.info("Dump uložený: %s", args.dump)

    if args.dry_run:
        with_fit = 0
        bulk_n = 0
        in_stock_n = 0
        for p in products:
            raw = p.to_ai_dict() if hasattr(p, "to_ai_dict") else p
            if is_bulk_pack(raw if isinstance(raw, dict) else {"name": getattr(p, "name", "")}):
                bulk_n += 1
            if is_supplier_in_stock(raw if isinstance(raw, dict) else {}):
                in_stock_n += 1
            has_fit = bool(getattr(p, "make", None) or getattr(p, "oe_numbers", None))
            if has_fit:
                with_fit += 1
        LOGGER.info(
            "DRY fitment=%d/%d; bulk≈%d; na sklade=%d/%d",
            with_fit,
            len(products),
            bulk_n,
            in_stock_n,
            len(products),
        )
        for p in products[:5]:
            LOGGER.info(
                "DRY: %s | SKU=%s | stock=%s/%s | %s € | fit make=%r model=%r",
                p.name,
                p.sku,
                getattr(p, "stock_qty", None),
                getattr(p, "stock_status", ""),
                p.regular_price or p.cost_price,
                getattr(p, "make", "") or "",
                getattr(p, "model", "") or "",
            )
        LOGGER.info(
            "Po pilot importe over _sg_fit_make: python verify_fitment_meta.py --sample 20"
        )
        return

    results = import_products(
        products,
        sleep=args.sleep,
        fast=args.fast,
        cheap=cheap,
        in_stock_only=in_stock_only,
    )
    ok = sum(1 for r in results if r.get("success"))
    fast_n = sum(1 for r in results if r.get("fast_path"))
    cheap_n = sum(1 for r in results if r.get("cheap_path") and not r.get("skipped"))
    skip_n = sum(1 for r in results if r.get("skipped"))
    oos_n = sum(1 for r in results if r.get("reason") == "out_of_stock")
    LOGGER.info(
        "🎉 Hotovo: %d/%d úspešných (fast=%d, cheap=%d, skip=%d, oos_skip=%d)",
        ok,
        len(results),
        fast_n,
        cheap_n,
        skip_n,
        oos_n,
    )


if __name__ == "__main__":
    main()
