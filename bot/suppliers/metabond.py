#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Metabond — verejný shop (Next.js + api.metabond.sk) + voliteľný B2B partner.

Verejný: https://metabond.sk/
API list: https://api.metabond.sk/api/products
Partner (login): https://partner.metabond.sk/
  METABOND_USER / METABOND_PASSWORD v .env

Starý HTML link-crawl bral landing stránky (/tricka, /login, /predajne…) ako
produkty → drafty bez ceny. Teraz:
  1) discovery cez API (paginácia),
  2) len chemické/olejové URL (metabond-vyrobky, mazacie-oleje),
  3) cena/SKU z Next.js pageProps (variant.price).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from .base import LoginMixin, ProductRaw, absolutize, parse_price
from .generic import GenericSupplier

LOGGER = logging.getLogger("supplier.metabond")

API_PRODUCTS = "https://api.metabond.sk/api/products"
PUBLIC_ORIGIN = "https://metabond.sk"

# Landing / merch — NIE sú to chemické produkty na import.
_SKIP_PATH_PARTS = (
    "/oblecenie",
    "/obleceniee",
    "/pracovne-oblecenie",
    "/tricka",
    "/kosele",
    "/ciapky",
    "/utierky",
    "/predajne",
    "/servisy",
    "/login",
    "/blog",
    "/blogy",
    "/kontakt",
    "/o-nas",
    "/nezavazna-registracia",
    "/cerpacie-stanice",
    "/autoelektronika",
    "/motosport",  # landing sekcie — produkty majú hlbší path
)

_ALLOW_PATH_PARTS = (
    "/produkty/metabond-vyrobky/",
    "/mazacie-oleje/",
)


class MetabondSupplier(LoginMixin, GenericSupplier):
    name = "metabond"
    base_url = "https://partner.metabond.sk"
    public_url = PUBLIC_ORIGIN

    def __init__(self, http=None, credentials=None):
        creds = dict(credentials or {})
        creds.setdefault("login_url", "https://partner.metabond.sk/partner/login")
        super().__init__(base_url=self.base_url, http=http, credentials=creds, source_name="metabond")
        self._logged_in = False

    # ------------------------------------------------------------------ discovery

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        """Z API vytiahni url_key chemických produktov → verejné URL."""
        found: List[str] = []
        seen: Set[str] = set()
        page = 1
        while page <= 40:
            try:
                resp = self.http.get(API_PRODUCTS, params={"page": page})
                payload = resp.json()
            except Exception as exc:
                LOGGER.warning("Metabond API page=%s zlyhalo: %s", page, exc)
                break
            batch = payload.get("data") or []
            if not batch:
                break
            for item in batch:
                url_key = (item.get("url_key") or "").strip().lstrip("/")
                if not url_key:
                    continue
                path = "/" + url_key.lower()
                if not any(p in path for p in _ALLOW_PATH_PARTS):
                    continue
                if any(s in path for s in _SKIP_PATH_PARTS):
                    continue
                # Dávkovače / merch s nulovou cenou na liste často nemajú zmysel —
                # stále ich necháme, parse_product ich odfiltruje ak nemajú cenu.
                url = urljoin(PUBLIC_ORIGIN + "/", url_key)
                if url in seen:
                    continue
                seen.add(url)
                found.append(url)
                if limit and len(found) >= limit:
                    return found
            meta = payload.get("meta") or {}
            last = int(meta.get("last_page") or page)
            if page >= last:
                break
            page += 1

        if not found:
            LOGGER.warning("Metabond API nevrátilo produkty — fallback na HTML seeds")
            return self._discover_via_html_seeds(limit=limit)
        return found[: limit or None]

    def _discover_via_html_seeds(self, limit: Optional[int] = None) -> List[str]:
        seeds = (
            f"{PUBLIC_ORIGIN}/produkty",
            f"{PUBLIC_ORIGIN}/prisada-do-olejov",
            f"{PUBLIC_ORIGIN}/do-pohonnych-hmot",
            f"{PUBLIC_ORIGIN}/",
        )
        found: Set[str] = set()
        for seed in seeds:
            try:
                soup = self.soup(seed)
            except Exception:
                continue
            for a in soup.find_all("a", href=True):
                href = absolutize(seed, a["href"]).split("#")[0].split("?")[0]
                path = urlparse(href).path.lower()
                if any(p in path for p in _ALLOW_PATH_PARTS) and not any(s in path for s in _SKIP_PATH_PARTS):
                    found.add(href.rstrip("/"))
                if limit and len(found) >= limit:
                    return sorted(found)
        return sorted(found)[: limit or None]

    # ------------------------------------------------------------------ parse

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        # Partner login je bonus (B2B cost) — public page stačí na MOC.
        try:
            self.ensure_login()
        except Exception:
            pass

        path = urlparse(url).path.lower()
        if not any(p in path for p in _ALLOW_PATH_PARTS):
            LOGGER.info("Preskakujem non-product Metabond URL: %s", url)
            return None
        if any(s in path for s in _SKIP_PATH_PARTS) and "/metabond-vyrobky/" not in path and "/mazacie-oleje/" not in path:
            return None

        resource = self._load_next_resource(url)
        if not resource:
            # Fallback na generický HTML parser + našu price heuristiku
            product = super().parse_product(url)
            if not product:
                return None
            if not product.regular_price:
                product.regular_price = self._price_from_primary_span(url) or ""
            if not product.regular_price:
                LOGGER.info("Metabond bez ceny, skip: %s", url)
                return None
            product.brand = "Metabond"
            product.source = self.name
            if product.name and "metabond" not in product.name.lower():
                product.name = f"Metabond {product.name}"
            return product

        variant = self._pick_variant(resource)
        if not variant:
            LOGGER.info("Metabond bez predajného variantu: %s", url)
            return None

        price = parse_price(variant.get("price") or variant.get("formated_price"))
        if not price or price <= 0:
            LOGGER.info("Metabond cena 0, skip: %s", url)
            return None
        # Jidoka: scrapované tisíce € (napr. 10700) = zlý variant / mena / tisíce
        if price > 200:
            LOGGER.warning("Preskakujem Metabond absurdnú cenu %.2f: %s", price, url)
            return None

        name = (variant.get("name") or resource.get("name") or "").strip()
        sku = str(variant.get("sku") or "")
        # Dávkovače / drobný merch nie sú chemické produkty (a majú smiešne ceny).
        blob = f"{name} {url}".lower()
        if any(x in blob for x in ("dávkovač", "davkovac", "adagol", "trakové", "montérk", "kabát")):
            LOGGER.info("Preskakujem Metabond príslušenstvo/merch: %s", name)
            return None
        if price < 5:
            LOGGER.info("Preskakujem Metabond príliš nízku cenu %.2f: %s", price, name)
            return None
        images: List[str] = []
        for img in variant.get("images") or []:
            if isinstance(img, dict):
                for key in ("large_image_url", "original_image_url", "medium_image_url"):
                    if img.get(key):
                        images.append(img[key])
                        break
        if not images:
            base = resource.get("base_image") or {}
            if isinstance(base, dict):
                for key in ("large_image_url", "original_image_url", "medium_image_url"):
                    if base.get(key):
                        images.append(base[key])
                        break

        ean = str(
            variant.get("ean")
            or variant.get("gtin")
            or resource.get("ean")
            or resource.get("gtin")
            or ""
        )

        # Public cena = oficiálny maloobchod (retail_hint). Kým partner B2B
        # nevráti nákupnú, odhad cost ≈ 72 % MOC — publish QA potrebuje _cost_price
        # a pricing drží predaj na MOC (is_metabond_source pass-through).
        cost = round(price * 0.72, 2)
        if getattr(self, "_logged_in", False):
            pass

        return ProductRaw(
            name=name if name.lower().startswith("metabond") else f"Metabond {name}",
            source=self.name,
            supplier_url=url.split("?")[0],
            sku=sku,
            ean=ean,
            brand="Metabond",
            description=str(resource.get("description") or resource.get("short_description") or ""),
            short_description=str(resource.get("short_description") or ""),
            regular_price=f"{price:.2f}",
            sale_price="",
            cost_price=f"{cost:.2f}",
            stock_status="instock",
            existing_images=images,
            attributes={
                "package_volume": variant.get("package_volume") or resource.get("package_volume"),
                "package_weight": variant.get("package_weight") or resource.get("package_weight"),
                "metabond_id": resource.get("id"),
                "variant_id": variant.get("id"),
            },
            raw_extra={
                "parser": "metabond-next",
                "variant": variant.get("id"),
                "retail_hint": price,
                "cost_estimated": True,
            },
        )

    def _load_next_resource(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.http.get(url)
        except Exception as exc:
            LOGGER.warning("Metabond GET %s: %s", url, exc)
            return None
        html = resp.text or ""
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None
        try:
            payload = json.loads(m.group(1))
        except Exception:
            return None

        # Hľadaj resource s url_key / configuration.variants
        best = None
        for node in self._walk(payload):
            if not isinstance(node, dict):
                continue
            if "url_key" in node and ("configuration" in node or "formated_price" in node) and "name" in node:
                # Preferuj ten, čo sedí na URL
                uk = (node.get("url_key") or "").strip().lstrip("/")
                if uk and uk in url:
                    return node
                if best is None:
                    best = node
        return best

    @staticmethod
    def _walk(obj: Any):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from MetabondSupplier._walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from MetabondSupplier._walk(v)

    @staticmethod
    def _pick_variant(resource: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cfg = resource.get("configuration") or {}
        variants = cfg.get("variants") if isinstance(cfg, dict) else None
        if not variants:
            variants = resource.get("variants") or []
        priced: List[Dict[str, Any]] = []
        for v in variants or []:
            if not isinstance(v, dict):
                continue
            price = parse_price(v.get("price") or v.get("formated_price"))
            if price and price > 0:
                priced.append(v)
        if not priced:
            # Simple product? top-level price
            price = parse_price(resource.get("price") or resource.get("formated_price"))
            if price and price > 0:
                return {
                    "id": resource.get("id"),
                    "name": resource.get("name"),
                    "sku": resource.get("sku") or "",
                    "price": price,
                    "formated_price": resource.get("formated_price"),
                    "images": [],
                }
            return None
        # Prefer 250 ml balenie ak existuje (najčastejší retail)
        for v in priced:
            name = (v.get("name") or "").lower()
            vol = str(v.get("package_volume") or "").lower()
            if "250" in name or "250" in vol:
                return v
        return priced[0]

    def _price_from_primary_span(self, url: str) -> Optional[str]:
        try:
            soup = self.soup(url)
        except Exception:
            return None
        # Verejný detail: <span class="... text-primary">46,90 €</span> hneď pod H1
        h1 = soup.find("h1")
        if h1:
            sib = h1.find_next("span")
            if sib:
                p = parse_price(sib.get_text(" ", strip=True))
                if p and p > 0:
                    return f"{p:.2f}"
        return None
