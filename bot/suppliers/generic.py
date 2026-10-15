#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Univerzálny B2B/HTML crawler pre **autorizované** dodávateľské URL.

NIE je to nástroj na kopírovanie konkurenčných maloobchodných katalógov
ani TecDoc databáz bez licencie — pozri ``bot/data/CATALOG_LICENSING.md``.
Zakázané hostitele blokuje ``catalog_licensing.assert_allowed_catalog_url``.

Stratégia (v poradí priority):
1) JSON-LD schema.org/Product
2) OpenGraph + meta product:*
3) Bežné Shoptet / WooCommerce / Magento HTML selektory
4) Heuristika z textu stránky (EAN, SKU, cena, sklad)

Stačí dať product URL alebo category URL **vlastného dodávateľa**.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from .base import BaseSupplier, ProductRaw, absolutize, parse_price, same_host, extract_b2b_signals


class GenericSupplier(BaseSupplier):
    name = "generic"

    def __init__(self, base_url: str = "", http=None, credentials=None, source_name: str = "generic"):
        super().__init__(http=http, credentials=credentials)
        self.base_url = base_url.rstrip("/") if base_url else ""
        if self.base_url:
            import sys
            from pathlib import Path

            _bot = Path(__file__).resolve().parent.parent
            if str(_bot) not in sys.path:
                sys.path.insert(0, str(_bot))
            from catalog_licensing import assert_allowed_catalog_url

            assert_allowed_catalog_url(self.base_url, context="GenericSupplier.base_url")
        self.name = source_name or (urlparse(self.base_url).netloc if self.base_url else "generic")

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        if not self.base_url:
            return []
        found: Set[str] = set()
        seeds = [self.base_url]
        visited: Set[str] = set()
        host = urlparse(self.base_url).netloc

        while seeds and (limit is None or len(found) < limit):
            url = seeds.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                soup = self.soup(url)
            except Exception:
                continue

            for a in soup.find_all("a", href=True):
                href = absolutize(url, a["href"]).split("#")[0].split("?")[0]
                if not same_host(href, host):
                    continue
                path = urlparse(href).path.lower()
                if any(x in path for x in ("/kosik", "/cart", "/ucet", "/account", "/login", "/blog", "/kontakt")):
                    continue
                # Pravdepodobný produkt: hlbšia cesta alebo typické slugy
                if self._looks_like_product(href, a):
                    found.add(href)
                    if limit and len(found) >= limit:
                        break
                elif self._looks_like_category(href) and href not in visited and len(visited) < 80:
                    seeds.append(href)

        return sorted(found)[: limit or None]

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        soup = self.soup(url)
        data = self._from_jsonld(soup) or {}
        data.update({k: v for k, v in self._from_opengraph(soup).items() if v})
        data.update({k: v for k, v in self._from_html_selectors(soup, url).items() if v and not data.get(k)})

        name = (data.get("name") or "").strip()
        if not name:
            return None

        price = parse_price(data.get("price"))
        sale = parse_price(data.get("sale_price"))
        regular = price
        sale_price = ""
        if sale and price and sale < price:
            regular, sale_price = price, str(sale)
        elif sale and not price:
            regular = sale
        images = data.get("images") or []
        if isinstance(images, str):
            images = [images]

        stock = self._detect_stock(soup.get_text(" ", strip=True), data.get("availability"))
        b2b = extract_b2b_signals(soup)

        cost = b2b.get("cost_price")
        retail_hint = b2b.get("retail_hint")
        # Po prihlásení: ak nie je explicitná B2B cena, ber page price ako cost
        if cost is None and getattr(self, "_logged_in", False) and regular is not None:
            cost = regular
            regular = retail_hint

        return ProductRaw(
            name=name,
            source=self.name,
            supplier_url=url,
            sku=str(data.get("sku") or ""),
            ean=str(data.get("ean") or data.get("gtin") or data.get("gtin13") or ""),
            brand=str(data.get("brand") or ""),
            description=str(data.get("description") or ""),
            short_description=str(data.get("short_description") or ""),
            regular_price=str(regular) if regular is not None else (str(retail_hint) if retail_hint else ""),
            sale_price=str(sale_price) if sale_price else "",
            cost_price=str(cost) if cost is not None else "",
            stock_status=b2b.get("stock_status") or stock,
            stock_qty=b2b.get("stock_qty"),
            delivery_days=b2b.get("delivery_days"),
            delivery_text=b2b.get("delivery_text") or "",
            existing_images=[absolutize(url, i) for i in images if i],
            attributes=data.get("attributes") or {},
            raw_extra={"parser": "generic", "b2b": b2b},
        )

    # ---- discovery heuristics -------------------------------------------

    def _looks_like_product(self, href: str, anchor) -> bool:
        path = urlparse(href).path.strip("/")
        if not path:
            return False
        # single-segment slug often = product on Shoptet
        if "/" not in path and len(path) > 3:
            classes = " ".join(anchor.get("class") or []).lower()
            parent_classes = " ".join((anchor.parent.get("class") if anchor.parent else []) or []).lower()
            blob = classes + " " + parent_classes + " " + path
            if any(k in blob for k in ("product", "p-item", "woocommerce", "tovar", "katalog")):
                return True
            # Shoptet product pages typically have no extension and short path
            return True
        if any(k in path.lower() for k in ("/product/", "/produkt/", "/p/", "/tovar/")):
            return True
        return False

    def _looks_like_category(self, href: str) -> bool:
        path = urlparse(href).path.lower()
        return any(k in path for k in ("/kategoria", "/category", "/katalog", "/shop", "/c/"))

    # ---- parsers --------------------------------------------------------

    def _from_jsonld(self, soup) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or "")
            except Exception:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") in ("Product", ["Product"]) or item.get("@type") == "Product":
                    out["name"] = item.get("name")
                    out["description"] = item.get("description")
                    out["sku"] = item.get("sku")
                    out["ean"] = item.get("gtin13") or item.get("gtin") or item.get("ean")
                    brand = item.get("brand")
                    if isinstance(brand, dict):
                        out["brand"] = brand.get("name")
                    elif brand:
                        out["brand"] = brand
                    offers = item.get("offers")
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        out["price"] = offers.get("price")
                        out["availability"] = offers.get("availability")
                    img = item.get("image")
                    if isinstance(img, list):
                        out["images"] = img
                    elif img:
                        out["images"] = [img]
                    return out
        return out

    def _from_opengraph(self, soup) -> Dict[str, Any]:
        def meta(*keys):
            for key in keys:
                tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
                if tag and tag.get("content"):
                    return tag["content"]
            return ""

        return {
            "name": meta("og:title", "twitter:title"),
            "description": meta("og:description", "description"),
            "images": [meta("og:image")] if meta("og:image") else [],
            "price": meta("product:price:amount", "og:price:amount"),
        }

    def _from_html_selectors(self, soup, url: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        h1 = soup.find("h1")
        if h1:
            out["name"] = h1.get_text(strip=True)

        for sel in (
            ".p-detail-inner", "#description", ".product-detail-description",
            ".woocommerce-product-details__short-description", ".product-description",
            "[itemprop=description]",
        ):
            el = soup.select_one(sel)
            if el:
                out["description"] = el.get_text("\n", strip=True)
                break

        text = soup.get_text("\n", strip=True)
        sku_match = re.search(r"(?:SKU|Kód|Katalógové\s*číslo|Obj\.?\s*číslo)\s*[:#]?\s*([A-Za-z0-9\-_./]+)", text, re.I)
        if sku_match:
            out["sku"] = sku_match.group(1)
        ean_match = re.search(r"(?:EAN|GTIN)\s*[:#]?\s*(\d{8,14})", text, re.I)
        if ean_match:
            out["ean"] = ean_match.group(1)

        price_el = soup.select_one(".price-final, .woocommerce-Price-amount, [itemprop=price], .price")
        if price_el:
            out["price"] = price_el.get_text(strip=True)

        imgs = []
        for img in soup.select(".woocommerce-product-gallery__image img, .p-image img, .product-image img, img[itemprop=image]"):
            for attr in ("data-large_image", "data-src", "src"):
                if img.get(attr):
                    imgs.append(absolutize(url, img[attr]))
                    break
        if imgs:
            out["images"] = imgs
        return out

    def _detect_stock(self, text: str, availability: Any) -> str:
        blob = f"{text} {availability or ''}".lower()
        if any(k in blob for k in ("vypredané", "vypredane", "nedostupné", "outofstock", "out of stock", "sold out")):
            return "outofstock"
        if "instock" in blob or "skladom" in blob or "na sklade" in blob:
            return "instock"
        return "instock"
