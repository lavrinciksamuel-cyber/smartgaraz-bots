#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pro-Tec Slovakia (Shoptet) — https://www.pro-tec-slovakia.sk/

Verejný katalóg ide bez loginu. Ak doplníš PROTEC_USER + PROTEC_PASSWORD,
crawler sa prihlási a berie veľkoobchodné ceny (API netreba).
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import LoginMixin, ProductRaw, absolutize, parse_price, extract_b2b_signals
from .generic import GenericSupplier
from brands import detect_true_brand

LOGGER = logging.getLogger("suppliers.protec")


class ProtecSupplier(LoginMixin, GenericSupplier):
    name = "pro-tec"
    base_url = "https://www.pro-tec-slovakia.sk"

    def __init__(self, http=None, credentials=None):
        creds = dict(credentials or {})
        creds.setdefault("login_url", f"{self.base_url}/login/")
        super().__init__(base_url=self.base_url, http=http, credentials=creds, source_name="pro-tec")
        self._logged_in = False

    def _fetch_public_prices(self, url: str) -> tuple[float, float]:
        """Verejná stránka bez B2B cookies → (final_moc, list_moc).

        final = aktuálna MOC (`.price-final` / meta), napr. Hand Cleaning 33,86 €.
        list  = pôvodná maloobchodná (`.price-standard`), napr. 42,32 € — kopíruj
        ako Woo `regular_price` (prečiarknuté), final ako `sale_price`.
        """
        final = 0.0
        listed = 0.0
        try:
            resp = requests.get(
                url,
                timeout=25,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SmartGarazBot/1.0)",
                    "Accept-Language": "sk-SK,sk;q=0.9",
                },
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            meta = soup.find("meta", property="product:price:amount")
            if meta and meta.get("content"):
                final = float(parse_price(meta["content"]) or 0.0)
            if final <= 0:
                el = soup.select_one(".price-final-holder .price-final, .price-final")
                if el:
                    final = float(parse_price(el.get_text()) or 0.0)
            old_el = soup.select_one(".price-standard, .price-before-discount")
            if old_el:
                listed = float(parse_price(old_el.get_text()) or 0.0)
            # Nesúvisiaci scrap (335 € pri ~8 €) — nie je to „bola/je“ zľava.
            if listed > 0 and final > 0 and listed > final * 5:
                listed = 0.0
        except Exception:
            LOGGER.debug("public prices fetch failed for %s", url, exc_info=True)
        return final, listed

    def _fetch_public_retail(self, url: str) -> float:
        """Verejná MOC bez B2B cookies (Engine Clean = 8,49 €, nie partner 5,94)."""
        final, _listed = self._fetch_public_prices(url)
        return final

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        self.ensure_login()  # voliteľné — ak nie sú credentials, pokračuje verejne
        found: Set[str] = set()
        ignore = ("/kosik", "/ucet", "/kontakt", "/blog", "/o-nas", "/klient", "/stranka", "/kategoria-produktu")
        categories: Set[str] = set()

        try:
            soup = self.soup(self.base_url + "/")
            for a in soup.find_all("a", href=True):
                href = absolutize(self.base_url, a["href"])
                if "pro-tec-slovakia.sk" not in href or any(x in href for x in ignore):
                    continue
                path = urlparse(href).path.strip("/")
                if path and "/" in path:
                    categories.add(href.split("?")[0])
        except Exception:
            pass

        for cat in sorted(categories):
            for page in range(1, 40):
                page_url = f"{cat}?page={page}" if page > 1 else cat
                try:
                    soup = self.soup(page_url)
                except Exception:
                    break
                added = 0
                for a in soup.select(".product a, .p-name a, .product-item a, article a, .p a"):
                    href = absolutize(cat, a.get("href", "")).split("?")[0]
                    if "pro-tec-slovakia.sk" not in href:
                        continue
                    path = urlparse(href).path.strip("/")
                    if path and "/" not in path and not any(i in href for i in ignore):
                        if href not in found:
                            found.add(href)
                            added += 1
                            if limit and len(found) >= limit:
                                return sorted(found)
                if added == 0 and page > 1:
                    break
        return sorted(found)

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        self.ensure_login()
        soup = self.soup(url)
        shoptet: dict = {}
        tracking = soup.find("script", id="trackingScript")
        if tracking and tracking.get("data-products"):
            try:
                dp = json.loads(tracking["data-products"])
                if dp.get("products"):
                    shoptet = list(dp["products"].values())[0]
            except Exception:
                pass

        name = shoptet.get("content_name") or shoptet.get("name") or ""
        if not name:
            h1 = soup.find("h1")
            name = h1.get_text(strip=True) if h1 else ""
        if not name:
            return None

        content_ids = shoptet.get("content_ids") or []
        sku = content_ids[0] if isinstance(content_ids, list) and content_ids else (shoptet.get("code") or "")
        if not sku:
            m = re.search(r"(?:Kód|Katalógové\s*číslo):?\s*([A-Za-z0-9\-]+)", soup.get_text(), re.I)
            if m:
                sku = m.group(1)

        ean = str(shoptet.get("ean") or "")
        if not ean:
            m = re.search(r"EAN:?\s*(\d{8,14})", soup.get_text(), re.I)
            if m:
                ean = m.group(1)

        current = float(shoptet.get("value") or 0) or parse_price(
            (soup.find("meta", property="product:price:amount") or {}).get("content")
            if soup.find("meta", property="product:price:amount") else None
        ) or 0.0

        old_price = 0.0
        old_el = soup.select_one(".price-standard, .price-before-discount, del, s")
        if old_el:
            old_price = parse_price(old_el.get_text()) or 0.0

        # Zbadané naživo: ".price-standard" vie ukazovať nezmysel (napr. 335 €
        # pri ~8–20 €). Bežná „bola/je“ zľava nie je 10–30× — takú ignoruj.
        if old_price > 0 and current > 0 and old_price > current * 5:
            old_price = 0.0

        # Po B2B logine je `current` = VEĽKOOBCHOD (cost). Verejná MOC musí
        # prísť z anonymnej stránky (Engine Clean: B2B 5.94 / final 8.49 /
        # Hand Cleaning: list 42,32 / final 33,86).
        b2b = extract_b2b_signals(soup)
        retail_hint = float(b2b.get("retail_hint") or 0.0)
        list_price = 0.0  # pôvodná maloobchodná (prečiarknuté)

        if self._logged_in:
            public_final, public_list = self._fetch_public_prices(url)
            if public_final > 0:
                retail_hint = public_final
            elif old_price > current > 0:
                retail_hint = max(retail_hint, old_price)
            if public_list > retail_hint > 0:
                list_price = public_list
            elif old_price > retail_hint > 0 and old_price <= retail_hint * 5:
                list_price = old_price
            if list_price > retail_hint > 0:
                # Pôvodná MOC (42,32) → regular; aktuálna MOC → sale + hint
                regular = list_price
                sale = retail_hint
            else:
                regular = retail_hint if retail_hint > 0 else 0.0
                sale = 0.0
        else:
            # Verejná „akcia“: standard (42,32) = pôvodná MOC, final = predaj
            if old_price > current > 0:
                list_price = old_price
                retail_hint = current
                regular = old_price
                sale = current
            else:
                regular = current if current else (old_price or 0.0)
                retail_hint = regular
                sale = 0.0

        stock = "outofstock" if any(
            k in soup.get_text().lower() for k in ("vypredané", "vyprodané", "nedostupné")
        ) else "instock"

        img = ""
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img = absolutize(url, og["content"])

        desc_el = soup.select_one(".p-detail-inner, #description, .product-detail-description")
        description = desc_el.get_text("\n", strip=True) if desc_el else ""

        # PRO-TEC je distribútor, ktorý predáva aj cudzie originálne značky
        # (Mopar, Pennzoil, Mobil 1, Shell, ...) — najprv skús rozpoznať
        # SKUTOČNÉHO výrobcu z názvu/popisu, až potom padni na PRO-TEC.
        brand = detect_true_brand(name, description, fallback="PRO-TEC", sku=sku)

        cost = b2b.get("cost_price")
        if cost is None and self._logged_in and current:
            cost = current  # po logine je shoptet value B2B nákup
        elif cost is None and self._logged_in and regular:
            # nemáme oddelený B2B — nepoužívaj MOC ako cost
            cost = None

        if retail_hint:
            b2b = dict(b2b)
            b2b["retail_hint"] = retail_hint
        if list_price > 0:
            b2b = dict(b2b)
            b2b["list_price"] = list_price

        return ProductRaw(
            name=name,
            source=self.name,
            supplier_url=url,
            sku=sku,
            ean=ean,
            brand=brand,
            description=description,
            regular_price=str(regular) if regular else "",
            sale_price=str(sale) if sale else "",
            cost_price=str(cost) if cost else "",
            stock_status=b2b.get("stock_status") or stock,
            stock_qty=b2b.get("stock_qty"),
            delivery_days=b2b.get("delivery_days") or (1 if stock == "instock" else None),
            delivery_text=b2b.get("delivery_text") or ("do 24 hodín" if stock == "instock" else ""),
            existing_images=[img] if img else [],
            raw_extra={
                "b2b": b2b,
                "retail_hint": retail_hint or None,
                "list_price": list_price or None,
            },
        )
