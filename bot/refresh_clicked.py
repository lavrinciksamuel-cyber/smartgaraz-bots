#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Denný full refresh produktov, na ktoré niekto klikol (za posledných 24 h).

Každý produkt max 1× za 24 h:
  1) živý B2B sync — cena, sklad, dodanie, akcia (stale sale_price preč)
  2) parse dodávateľskej stránky + AI — obrázok, copy, kategória, EAN/OE, bullshit preč
  3) _sg_last_refreshed = now

Použitie:
  python bot/refresh_clicked.py --dry-run --limit 5
  python bot/refresh_clicked.py --limit 30
  python bot/refresh_clicked.py --id 1234

Vyžaduje: WC_*, SG_INSIGHTS_KEY, ANTHROPIC_*, dodávateľské loginy podľa potreby.
Fronta: GET /wp-json/smartgaraz/v1/refresh-queue (po deployi child theme).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from ai_engine import (  # noqa: E402
    Config,
    WooCommerceClient,
    process_product_with_ai,
    setup_logging,
    wc_product_to_raw_data,
)
from secret_crawler import resolve_supplier, sync_one  # noqa: E402

LOGGER = logging.getLogger("refresh_clicked")

_LOGIN_OK: Set[str] = set()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _meta(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        m.get("key"): m.get("value")
        for m in (product.get("meta_data") or [])
        if isinstance(m, dict) and m.get("key")
    }


def fetch_refresh_queue(limit: int, hours: int = 24) -> List[Dict[str, Any]]:
    key = _env("SG_INSIGHTS_KEY")
    base = _env("WC_URL").rstrip("/")
    if not key or not base:
        raise RuntimeError("Chýba WC_URL alebo SG_INSIGHTS_KEY pre refresh-queue")
    r = requests.get(
        f"{base}/wp-json/smartgaraz/v1/refresh-queue",
        params={"limit": limit, "hours": hours},
        headers={"X-SG-Insights-Key": key},
        timeout=60,
    )
    if r.status_code in (401, 403):
        raise RuntimeError(f"refresh-queue auth failed ({r.status_code}) — skontroluj SG_INSIGHTS_KEY")
    if r.status_code == 404:
        raise RuntimeError(
            "refresh-queue 404 — child theme ešte nemá product-refresh.php (deploy najprv)"
        )
    r.raise_for_status()
    data = r.json()
    return list(data.get("items") or [])


def _ensure_supplier_login(supplier) -> None:
    name = getattr(supplier, "source_name", None) or getattr(supplier, "name", None) or supplier.__class__.__name__
    key = str(name).lower()
    if key in _LOGIN_OK:
        return
    if hasattr(supplier, "ensure_login"):
        ok = supplier.ensure_login()
        LOGGER.info("🔐 Login %s: %s", key, "OK" if ok else "skip/public")
    _LOGIN_OK.add(key)


def mark_refreshed(wc: WooCommerceClient, product_id: int, dry_run: bool) -> None:
    stamp = _utc_now()
    if dry_run:
        LOGGER.info("DRY _sg_last_refreshed=%s id=%s", stamp, product_id)
        return
    wc.upsert_product(
        product_id,
        {"meta_data": [{"key": "_sg_last_refreshed", "value": stamp}]},
    )


def refresh_one(
    wc: WooCommerceClient,
    config: Config,
    product_id: int,
    dry_run: bool = False,
    skip_ai: bool = False,
    cheap: bool = False,
) -> str:
    product = wc.get_product(product_id)
    meta = _meta(product)
    supplier_url = (meta.get("supplier_url") or "").strip()
    source = (meta.get("_supplier_source") or "").strip()
    name = product.get("name") or f"#{product_id}"

    # Public repo: log only the supplier host, never the product URL.
    LOGGER.info("🔄 Refresh #%s %s | src=%s", product_id, name[:60], _host(supplier_url) or "—")

    offer = None
    raw_dict: Dict[str, Any] = wc_product_to_raw_data(product)
    raw_dict["wc_product_id"] = product_id

    if supplier_url:
        from suppliers.base import OfferSnapshot, parse_price

        supplier = resolve_supplier(supplier_url, source)
        _ensure_supplier_login(supplier)

        # Jeden HTTP parse → AI raw + B2B offer (fetch_offer by volal parse znova).
        parsed = None
        try:
            parsed = supplier.parse_product(supplier_url)
        except Exception as exc:
            LOGGER.warning("parse_product #%s: %s", product_id, exc)

        if parsed:
            raw_dict = parsed.to_ai_dict()
            raw_dict["wc_product_id"] = product_id
            raw_dict["status"] = product.get("status") or "publish"
            if not raw_dict.get("sku"):
                raw_dict["sku"] = product.get("sku") or ""
            cost = parse_price(parsed.cost_price)
            retail = parse_price(parsed.regular_price)
            if cost is None and retail is not None and getattr(supplier, "_logged_in", False):
                cost = retail
                retail = None
            offer = OfferSnapshot(
                supplier_url=supplier_url,
                source=getattr(parsed, "source", None) or source or getattr(supplier, "name", "") or "",
                cost_price=cost,
                retail_hint=retail,
                regular_price=retail,
                sale_price=parse_price(parsed.sale_price) or None,
                stock_status=parsed.stock_status,
                stock_qty=parsed.stock_qty,
                delivery_days=parsed.delivery_days,
                delivery_text=parsed.delivery_text or "",
                currency=parsed.currency,
                sku=parsed.sku or product.get("sku") or "",
                name=parsed.name or name,
            )

        if offer and skip_ai:
            try:
                stamp_meta = [{"key": "_sg_last_refreshed", "value": _utc_now()}]
                b2b = sync_one(
                    wc,
                    offer,
                    dry_run=dry_run,
                    product_id=product_id,
                    extra_meta=stamp_meta,
                )
                LOGGER.info("💰 B2B #%s → %s", product_id, b2b)
                if dry_run:
                    return "dry_run"
                # no_cost neurobí PUT — označ refresh samostatne, nech fronta nespamuje.
                if not (b2b.startswith("updated") or b2b.startswith("created")):
                    mark_refreshed(wc, product_id, dry_run=False)
                return "ok" if b2b.startswith("updated") or b2b.startswith("created") else b2b
            except Exception as exc:
                LOGGER.warning("B2B sync #%s: %s", product_id, exc)
    else:
        LOGGER.warning("#%s bez supplier_url — len AI cleanup z WC dát", product_id)

    # Doplň čerstvé B2B polia do AI raw (cena/sklad/dodanie)
    if offer:
        if offer.cost_price is not None:
            raw_dict["cost_price"] = str(offer.cost_price)
        if offer.retail_hint is not None:
            raw_dict["regular_price"] = str(offer.retail_hint)
        if offer.sale_price is not None:
            raw_dict["sale_price"] = str(offer.sale_price) if offer.sale_price else ""
        else:
            # žiadna akcia u dodávateľa → zruš starú akciu v AI payloadi
            raw_dict["sale_price"] = ""
        if offer.stock_qty is not None:
            raw_dict["stock_qty"] = offer.stock_qty
        if offer.stock_status:
            raw_dict["stock_status"] = offer.stock_status
        if offer.delivery_days is not None:
            raw_dict["delivery_days"] = offer.delivery_days
        if offer.delivery_text:
            raw_dict["delivery_text"] = offer.delivery_text
        raw_dict["supplier_url"] = offer.supplier_url or supplier_url
        if offer.source:
            raw_dict["source"] = offer.source

    if dry_run:
        LOGGER.info(
            "DRY AI-ready #%s sku=%s imgs=%d has_cost=%s has_sale=%s",
            product_id,
            raw_dict.get("sku"),
            len(raw_dict.get("existing_images") or []),
            raw_dict.get("cost_price") not in (None, ""),
            bool(raw_dict.get("sale_price")),
        )
        mark_refreshed(wc, product_id, dry_run=True)
        return "dry_run"

    stamp_meta = [{"key": "_sg_last_refreshed", "value": _utc_now()}]
    stamped = False

    if not skip_ai:
        try:
            report = process_product_with_ai(
                raw_dict,
                config=config,
                wc_client=wc,
                extra_meta=stamp_meta,
                cheap=cheap,
            )
            stamped = bool(isinstance(report, dict) and report.get("success"))
            LOGGER.info(
                "🤖 AI #%s ok warnings=%s",
                product_id,
                report.get("warnings") if isinstance(report, dict) else None,
            )
        except Exception as exc:
            LOGGER.exception("AI refresh #%s zlyhal: %s", product_id, exc)
            # Aj pri AI faile označíme refresh ak B2B prebehlo — inak by sme spamovali
            mark_refreshed(wc, product_id, dry_run=False)
            return f"ai_fail:{exc}"

    # B2B stock PO AI (AI payload neukladá stock_quantity); stamp len ak ešte nie je v AI PUT.
    if offer and supplier_url:
        try:
            sync_one(
                wc,
                offer,
                dry_run=False,
                product_id=product_id,
                extra_meta=None if stamped else stamp_meta,
            )
            return "ok"
        except Exception as exc:
            LOGGER.warning("post-AI B2B #%s: %s", product_id, exc)

    if not stamped:
        mark_refreshed(wc, product_id, dry_run=False)
    return "ok"


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Full refresh kliknutých produktov (max 1×/24h)")
    ap.add_argument("--limit", type=int, default=30, help="Max produktov (pilot ≤50)")
    ap.add_argument("--hours", type=int, default=24, help="Okno klikov / refresh cooldown")
    ap.add_argument("--id", type=int, action="append", dest="ids", help="Vynúť konkrétne WC ID (opakuj)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--skip-ai",
        action="store_true",
        help="Len B2B cena/sklad (bez Anthropic). Default pre GHA schedule.",
    )
    ap.add_argument(
        "--cheap",
        action="store_true",
        help="0 € template AI (AI_MODE=cheap) namiesto full Claude",
    )
    ap.add_argument("--sleep", type=float, default=0.35)
    args = ap.parse_args()

    # Autonómne: REFRESH_SKIP_AI=1 alebo AI_MODE=cheap → žiadny full Claude.
    env_skip = (os.getenv("REFRESH_SKIP_AI") or "").strip().lower() in {"1", "true", "yes", "on"}
    env_cheap = (os.getenv("AI_MODE") or "").strip().lower() in {
        "cheap", "template", "budget", "zero",
    }
    skip_ai = bool(args.skip_ai) or env_skip
    use_cheap = bool(args.cheap) or env_cheap
    if skip_ai:
        use_cheap = False

    limit = max(1, min(50, args.limit))
    config = Config()
    if not skip_ai and not use_cheap:
        missing = config.validate()
        if missing:
            LOGGER.error("Chýbajú env: %s", ", ".join(missing))
            sys.exit(1)
    else:
        if not config.wc_url or not config.wc_consumer_key:
            LOGGER.error("Chýbajú WC_*")
            sys.exit(1)
        LOGGER.info(
            "Mode: %s",
            "B2B-only --skip-ai (0 €)" if skip_ai else "cheap template (0 € Claude)",
        )

    wc = WooCommerceClient(config)
    ids: List[int] = []
    if args.ids:
        ids = [int(i) for i in args.ids]
    else:
        try:
            queue = fetch_refresh_queue(limit=limit, hours=args.hours)
        except Exception as exc:
            LOGGER.error("%s", exc)
            sys.exit(2)
        ids = [int(item["id"]) for item in queue if item.get("id")]
        LOGGER.info("📋 Fronta: %d produktov (hours=%s)", len(ids), args.hours)

    if not ids:
        LOGGER.info("Fronta prázdna — hotovo.")
        return

    stats = {"ok": 0, "dry_run": 0, "fail": 0}
    for i, pid in enumerate(ids, 1):
        LOGGER.info("——— [%d/%d] ———", i, len(ids))
        try:
            result = refresh_one(
                wc,
                config,
                pid,
                dry_run=args.dry_run,
                skip_ai=skip_ai,
                cheap=use_cheap,
            )
            if result == "ok":
                stats["ok"] += 1
            elif result == "dry_run":
                stats["dry_run"] += 1
            else:
                stats["fail"] += 1
        except Exception as exc:
            LOGGER.exception("Hard fail #%s: %s", pid, exc)
            stats["fail"] += 1
        time.sleep(max(0.0, args.sleep))

    LOGGER.info("Hotovo: %s", stats)
    print(stats)


if __name__ == "__main__":
    main()
