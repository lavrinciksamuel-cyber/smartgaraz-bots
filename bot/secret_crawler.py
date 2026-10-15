#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
SECRET CRAWLER — B2B cenotvorba + sklad + rýchlosť dodania
===============================================================================

Prihlási sa k dodávateľovi (USER/PASSWORD), stiahne:
  • dodávateľskú (nákupnú) cenu
  • skladové zásoby (ks)
  • rýchlosť dodania (do 24h / 2–3 dni…)

Potom vypočíta predajnú MOC (marža + doprava) a zapíše do WooCommerce:
  • regular_price
  • stock_quantity / stock_status / manage_stock
  • meta: _cost_price, _supplier_stock, _delivery_days, _delivery_text, supplier_url

Použitie:
  python secret_crawler.py --limit 30
  python secret_crawler.py --supplier pro-tec --limit 20
  python secret_crawler.py --dry-run --limit 10
  python secret_crawler.py --url https://www.pro-tec-slovakia.sk/produkt/

Vyžaduje v .env:
  WC_* , ANTHROPIC nie je potrebný
  PROTEC_USER/PASSWORD, WURTH_*, LKQ_* podľa potreby
  MARGIN_PERCENT=35  SHIPPING_FLAT=0.60  PRICE_ENDING=0.90
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_engine import Config, WooCommerceClient, setup_logging  # noqa: E402
from pricing import price_from_offer  # noqa: E402
from suppliers import get_supplier  # noqa: E402
from suppliers.generic import GenericSupplier  # noqa: E402

LOGGER = logging.getLogger("secret_crawler")


def resolve_supplier(url: str = "", supplier_name: str = ""):
    if supplier_name:
        return get_supplier(supplier_name)
    host = (urlparse(url).netloc or "").lower()
    if "pro-tec" in host:
        return get_supplier("pro-tec")
    if "metabond" in host:
        return get_supplier("metabond")
    if "wurth" in host:
        return get_supplier("wurth")
    if "lkq" in host:
        return get_supplier("lkq")
    if "szekop" in host:
        return get_supplier("szekop")
    return GenericSupplier(
        base_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}",
        source_name=host or "generic",
    )


def build_wc_payload(offer, pricing) -> Dict:
    meta = [
        {"key": "_cost_price", "value": f"{pricing.cost_price:.2f}"},
        {"key": "_supplier_stock", "value": str(pricing.stock_qty if pricing.stock_qty is not None else "")},
        {"key": "_delivery_days", "value": str(pricing.delivery_days or "")},
        {"key": "_delivery_text", "value": pricing.delivery_text or ""},
        {"key": "supplier_url", "value": offer.supplier_url},
        {"key": "_supplier_source", "value": offer.source},
        {"key": "_last_b2b_sync", "value": time.strftime("%Y-%m-%d %H:%M:%S")},
    ]
    # Predaj = aktuálna MOC (pricing.retail_price). Nákupná (_cost_price) len v meta.
    sell = float(pricing.retail_price)
    if sell <= 0:
        raise ValueError("retail_price must be > 0")
    if pricing.cost_price and sell + 1e-9 < float(pricing.cost_price):
        # Nikdy nepredávaj pod nákup — dorovnaj na cost (jidoka; sync opraví neskôr).
        sell = float(pricing.cost_price)

    list_moc = getattr(offer, "regular_price", None)
    sale = getattr(offer, "sale_price", None)
    try:
        list_f = float(list_moc) if list_moc not in (None, "") else 0.0
    except (TypeError, ValueError):
        list_f = 0.0
    try:
        sale_f = float(sale) if sale not in (None, "") else 0.0
    except (TypeError, ValueError):
        sale_f = 0.0

    # Maloobchodná akcia: prečiarknutá pôvodná MOC + aktuálna predajná.
    on_sale = list_f > sell + 0.009 and list_f <= sell * 5 and sale_f > 0
    if on_sale:
        # Aktuálna predajná = sell (z retail_hint); regular = list.
        payload: Dict = {
            "regular_price": f"{list_f:.2f}",
            "sale_price": f"{sell:.2f}",
            "stock_status": pricing.stock_status,
            "manage_stock": pricing.stock_qty is not None,
            "meta_data": meta,
        }
    else:
        payload = {
            "regular_price": f"{sell:.2f}",
            "sale_price": "",
            "stock_status": pricing.stock_status,
            "manage_stock": pricing.stock_qty is not None,
            "meta_data": meta,
        }
    if pricing.stock_qty is not None:
        payload["stock_quantity"] = int(pricing.stock_qty)
    return payload


def find_wc_product(wc: WooCommerceClient, sku: str = "", supplier_url: str = "") -> Optional[dict]:
    if sku:
        pid = wc.find_product_id_by_sku(sku) if hasattr(wc, "find_product_id_by_sku") else None
        if pid:
            try:
                return wc.get_product(pid)
            except Exception:
                pass
        resp = wc.api.get("products", params={"sku": sku})
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
    if supplier_url:
        # WooCommerce nevie filtrovať meta priamo — prejdeme recent pages (best-effort)
        for page in range(1, 6):
            resp = wc.api.get("products", params={"per_page": 100, "page": page, "search": sku or ""})
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for p in data:
                meta = {m["key"]: m["value"] for m in p.get("meta_data", []) if isinstance(m, dict)}
                if meta.get("supplier_url") == supplier_url:
                    return p
    return None


def sync_one(
    wc: WooCommerceClient,
    offer,
    dry_run: bool = False,
    product_id: Optional[int] = None,
    extra_meta: Optional[list] = None,
) -> str:
    pricing = price_from_offer(offer)
    if not pricing:
        return "no_cost"

    existing = {"id": int(product_id)} if product_id else find_wc_product(
        wc, sku=offer.sku, supplier_url=offer.supplier_url
    )
    payload = build_wc_payload(offer, pricing)
    if extra_meta:
        meta = payload.setdefault("meta_data", [])
        meta.extend(extra_meta)

    LOGGER.info(
        "💰 %s | nákup %.2f€ + doprava %.2f€ → MOC %.2f€ (zisk %.2f€) | sklad=%s | dodanie=%s",
        offer.name[:50],
        pricing.cost_price,
        pricing.shipping,
        pricing.retail_price,
        pricing.profit_eur,
        pricing.stock_qty if pricing.stock_qty is not None else pricing.stock_status,
        pricing.delivery_text or pricing.delivery_days,
    )

    if dry_run:
        return "dry_run"

    if existing:
        wc.upsert_product(existing["id"], payload)
        return f"updated:{existing['id']}"

    # Nový produkt — len cena/sklad draft (AI copy doplní continuous loop / import)
    payload.update({
        "name": offer.name or offer.sku or "Importovaný produkt",
        "sku": offer.sku or "",
        "status": "draft",
        "type": "simple",
    })
    created = wc.upsert_product(None, payload)
    return f"created:{created.get('id')}"


def _offer_from_product_raw(product) -> "OfferSnapshot":
    from suppliers.base import OfferSnapshot, parse_price

    extra = getattr(product, "raw_extra", None) or {}
    hint = parse_price(extra.get("retail_hint")) if isinstance(extra, dict) else None
    list_p = parse_price(extra.get("list_price")) if isinstance(extra, dict) else None
    sale = parse_price(product.sale_price)
    listed = list_p or parse_price(product.regular_price)
    current = hint or sale or listed
    cost = parse_price(product.cost_price)
    list_moc = listed if (listed and current and listed > current + 0.009 and listed <= current * 5) else None

    return OfferSnapshot(
        supplier_url=product.supplier_url,
        source=product.source,
        cost_price=cost,
        retail_hint=current,
        regular_price=list_moc or current,
        sale_price=current if list_moc else None,
        stock_status=product.stock_status,
        stock_qty=product.stock_qty,
        delivery_days=product.delivery_days,
        delivery_text=product.delivery_text,
        sku=product.sku,
        name=product.name,
    )


def crawl_supplier_catalog(supplier_name: str, limit: int, wc: WooCommerceClient, dry_run: bool):
    supplier = get_supplier(supplier_name)
    # Vynúť login ak sú credentials
    if hasattr(supplier, "ensure_login"):
        ok = supplier.ensure_login()
        LOGGER.info("🔐 Login %s: %s", supplier_name, "OK" if ok else "preskočený/verejný")

    stats = {"updated": 0, "created": 0, "skipped": 0, "failed": 0}
    delay = float(os.getenv("CRAWL_DELAY", "0.25"))
    for idx, product in enumerate(supplier.iter_catalog(limit=limit), 1):
        LOGGER.info("--- [%d] %s ---", idx, product.name)
        # iter_catalog už parsoval stránku — nechodiť druhýkrát, ak máme cenu
        offer = _offer_from_product_raw(product)
        if offer.cost_price is None and product.supplier_url:
            offer = supplier.fetch_offer(product.supplier_url) or offer
        try:
            result = sync_one(wc, offer, dry_run=dry_run)
            if result.startswith("updated"):
                stats["updated"] += 1
            elif result.startswith("created"):
                stats["created"] += 1
            elif result == "no_cost":
                stats["skipped"] += 1
                LOGGER.warning("⚠️  Chýba nákupná cena pre %s", product.name)
            else:
                stats["skipped"] += 1
        except Exception as exc:
            LOGGER.exception("Zlyhalo: %s", exc)
            stats["failed"] += 1
        time.sleep(delay)
    return stats


_ONLY_HOST = os.getenv("SG_ONLY_SUPPLIER_HOST", "").strip().lower()


def sync_existing_wc(
    wc: WooCommerceClient,
    limit: int,
    dry_run: bool,
    sleep: float,
    status: str = "publish",
    prefer_no_cost: bool = False,
):
    """Prejde existujúce WC produkty so supplier_url a prepočíta ceny zo živého B2B.

    prefer_no_cost: najprv produkty bez ``_cost_price`` (draft fronta / jidoka),
    potom zvyšok. Scan-cap ~4× limit, aby heijunka našla chýbajúce nákupné.
    """
    stats = {"updated": 0, "skipped": 0, "failed": 0}
    page = 1
    processed = 0
    # 1 login / supplier host na celý beh
    supplier_by_host: Dict[str, object] = {}
    status_param = (status or "publish").strip().lower()
    if status_param not in ("publish", "draft", "pending", "private", "any"):
        status_param = "publish"

    def _has_cost(product: dict) -> bool:
        meta = {
            m["key"]: m["value"]
            for m in product.get("meta_data", [])
            if isinstance(m, dict)
        }
        raw = meta.get("_cost_price")
        try:
            return float(str(raw).replace(",", ".")) > 0
        except (TypeError, ValueError):
            return False

    # Pri prefer_no_cost najprv zozbieraj kandidátov, no-cost first
    queue: List[dict] = []
    scan_cap = max(200, (limit or 30) * 4) if prefer_no_cost else 0
    scanned = 0
    while True:
        params = {"per_page": 50, "page": page, "orderby": "id", "order": "desc"}
        if status_param != "any":
            params["status"] = status_param
        resp = wc.api.get("products", params=params)
        products = resp.json()
        if not isinstance(products, list) or not products:
            break
        if prefer_no_cost:
            for product in products:
                scanned += 1
                meta = {
                    m["key"]: m["value"]
                    for m in product.get("meta_data", [])
                    if isinstance(m, dict)
                }
                if not (meta.get("supplier_url") or ""):
                    continue
                queue.append(product)
                if scanned >= scan_cap:
                    break
            if scanned >= scan_cap or len(products) < 50:
                break
            page += 1
            continue
        # Legacy streaming path (bez preferencie)
        for product in products:
            if limit and processed >= limit:
                return stats
            processed += 1
            meta = {m["key"]: m["value"] for m in product.get("meta_data", []) if isinstance(m, dict)}
            supplier_url = meta.get("supplier_url") or ""
            if not supplier_url or (_ONLY_HOST and _ONLY_HOST not in (urlparse(supplier_url).netloc or "").lower()):
                stats["skipped"] += 1
                continue
            try:
                host = (urlparse(supplier_url).netloc or "").lower()
                supplier = supplier_by_host.get(host)
                if supplier is None:
                    supplier = resolve_supplier(supplier_url, meta.get("_supplier_source") or "")
                    if hasattr(supplier, "ensure_login"):
                        supplier.ensure_login()
                    supplier_by_host[host] = supplier
                offer = supplier.fetch_offer(supplier_url)
                if not offer:
                    stats["failed"] += 1
                    continue
                offer.sku = offer.sku or product.get("sku") or ""
                offer.name = offer.name or product.get("name") or ""
                result = sync_one(wc, offer, dry_run=dry_run, product_id=int(product["id"]))
                if result.startswith("updated") or result.startswith("created"):
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                LOGGER.warning("ID %s zlyhalo: %s", product.get("id"), exc)
                stats["failed"] += 1
            time.sleep(sleep)
        page += 1
        if not prefer_no_cost:
            continue

    if prefer_no_cost:
        # Len bez nákupnej — neplň limit bulk sudmi, ktoré už cost majú.
        no_cost = [p for p in queue if not _has_cost(p)]
        no_cost.sort(key=lambda p: -int(p.get("id") or 0))
        if not no_cost:
            LOGGER.info("prefer-no-cost: v scan okne nie sú drafty bez _cost_price")
            return stats
        queue = no_cost
        LOGGER.info("prefer-no-cost: kandidátov bez cost=%s (limit=%s)", len(queue), limit)
        for product in queue:
            if limit and processed >= limit:
                break
            processed += 1
            meta = {
                m["key"]: m["value"]
                for m in product.get("meta_data", [])
                if isinstance(m, dict)
            }
            supplier_url = meta.get("supplier_url") or ""
            if not supplier_url or (_ONLY_HOST and _ONLY_HOST not in (urlparse(supplier_url).netloc or "").lower()):
                stats["skipped"] += 1
                continue
            try:
                host = (urlparse(supplier_url).netloc or "").lower()
                supplier = supplier_by_host.get(host)
                if supplier is None:
                    supplier = resolve_supplier(
                        supplier_url, meta.get("_supplier_source") or ""
                    )
                    if hasattr(supplier, "ensure_login"):
                        supplier.ensure_login()
                    supplier_by_host[host] = supplier
                offer = supplier.fetch_offer(supplier_url)
                if not offer:
                    stats["failed"] += 1
                    continue
                offer.sku = offer.sku or product.get("sku") or ""
                offer.name = offer.name or product.get("name") or ""
                result = sync_one(
                    wc, offer, dry_run=dry_run, product_id=int(product["id"])
                )
                if result.startswith("updated") or result.startswith("created"):
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                LOGGER.warning("ID %s zlyhalo: %s", product.get("id"), exc)
                stats["failed"] += 1
            time.sleep(sleep)
    return stats


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Secret B2B crawler + cenotvorba")
    parser.add_argument("--supplier", help="pro-tec | metabond | wurth | lkq | szekop")
    parser.add_argument("--url", help="Jedno produktové URL u dodávateľa")
    parser.add_argument("--existing", action="store_true", help="Sync existujúcich WC produktov so supplier_url")
    parser.add_argument(
        "--status",
        default="publish",
        help="WC status filter pre --existing: publish|draft|pending|private|any (default publish)",
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument(
        "--prefer-no-cost",
        action="store_true",
        help="Pri --existing najprv sync produktov bez _cost_price (draft jidoka)",
    )
    args = parser.parse_args()

    config = Config()
    # Secret crawler potrebuje len WC, nie Anthropic
    wc_missing = [k for k in ("WC_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET") if not os.getenv(k)]
    if wc_missing:
        # Config.validate chce aj Anthropic — tu stačí WC
        if not config.wc_url or not config.wc_consumer_key or not config.wc_consumer_secret:
            LOGGER.error("Chýbajú WC_* údaje v .env")
            sys.exit(1)

    wc = WooCommerceClient(config)
    LOGGER.info(
        "⚙️  Cenotvorba: marža=%s%% doprava=%s€ ending=.%s",
        os.getenv("MARGIN_PERCENT", "35"),
        os.getenv("SHIPPING_FLAT", "0.60"),
        os.getenv("PRICE_ENDING", "0.90"),
    )

    if args.url:
        supplier = resolve_supplier(args.url)
        if hasattr(supplier, "ensure_login"):
            supplier.ensure_login()
        offer = supplier.fetch_offer(args.url)
        if not offer:
            LOGGER.error("Nepodarilo sa načítať ponuku z %s", args.url)
            sys.exit(1)
        print(sync_one(wc, offer, dry_run=args.dry_run))
        return

    if args.supplier:
        stats = crawl_supplier_catalog(args.supplier, args.limit, wc, args.dry_run)
    else:
        # Default: sync existujúceho katalógu
        stats = sync_existing_wc(
            wc,
            args.limit,
            args.dry_run,
            args.sleep,
            status=args.status,
            prefer_no_cost=bool(args.prefer_no_cost),
        )

    LOGGER.info("🎉 Hotovo: %s", stats)


if __name__ == "__main__":
    main()
