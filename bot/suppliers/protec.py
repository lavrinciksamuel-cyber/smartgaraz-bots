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

# Zmení sa, keď sa zmení výber produktu zo Shoptet trackingu. Sync/reprice
# starších riadkov (banner P1241) sa nesmie spoľahnúť na uložený cost, kým
# produkt nemá túto značku po novom parse.
PARSER_VERSION = "pcode-v1"


def _norm_code(value: object) -> str:
    return re.sub(r"[\s._]+", "", str(value or "")).strip().lower()


def page_product_code(soup) -> str:
    """Kód z hlavného detailu (`.p-code`), nie z banneru v trackingu."""
    el = soup.select_one(".p-code, [itemprop='sku']")
    if not el:
        return ""
    text = el.get_text(" ", strip=True)
    text = re.sub(
        r"^(kód|kod|code|katalógové\s*číslo|katalogove\s*cislo|sku)\s*:?\s*",
        "",
        text,
        flags=re.I,
    )
    return text.strip()


def select_tracked_product(products: dict, page_code: str) -> dict:
    """Položka trackingu, ktorej id / content_ids / code sedí s `.p-code`.

    `list(products.values())[0]` je na Shoptete banner (P1241 Diesel System
    Super Clean) na každej stránke — bez zhody kódu ho neberieme.
    """
    items = [(str(key), item) for key, item in (products or {}).items() if isinstance(item, dict)]
    if not items:
        return {}
    code = _norm_code(page_code)
    if not code:
        return items[0][1] if len(items) == 1 else {}
    for key, item in items:
        ids = item.get("content_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        candidates = [key, item.get("code"), item.get("id"), item.get("product_id")]
        if isinstance(ids, list):
            candidates.extend(ids)
        if any(_norm_code(c) == code for c in candidates if c not in (None, "")):
            return item
    return {}


def detail_price_amount(soup) -> float:
    """Cena z bloku hlavného produktu, nie z cudzieho banneru."""
    for sel in (
        ".p-detail .price-final",
        ".p-detail-inner .price-final",
        ".product-detail .price-final",
        ".price-final-holder .price-final",
        ".p-final-price .price-final",
    ):
        el = soup.select_one(sel)
        if not el:
            continue
        val = parse_price(el.get_text())
        if val:
            return float(val)
    return 0.0


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
        page_code = page_product_code(soup)
        shoptet: dict = {}
        tracking = soup.find("script", id="trackingScript")
        if tracking and tracking.get("data-products"):
            try:
                dp = json.loads(tracking["data-products"])
                if dp.get("products"):
                    shoptet = select_tracked_product(dp["products"], page_code)
            except Exception:
                pass

        h1 = soup.find("h1")
        h1_name = h1.get_text(strip=True) if h1 else ""
        # Tracking len keď sedí s kódom stránky. Inak názov z H1, nie z banneru.
        name = (shoptet.get("content_name") or shoptet.get("name") or "") if shoptet else ""
        if not name:
            name = h1_name
        if not name:
            return None

        content_ids = shoptet.get("content_ids") or []
        tracked_sku = content_ids[0] if isinstance(content_ids, list) and content_ids else (shoptet.get("code") or "")
        sku = page_code or tracked_sku or ""
        if not sku:
            m = re.search(r"(?:Kód|Katalógové\s*číslo):?\s*([A-Za-z0-9\-]+)", soup.get_text(), re.I)
            if m:
                sku = m.group(1)

        ean = str(shoptet.get("ean") or "")
        if not ean:
            m = re.search(r"EAN:?\s*(\d{8,14})", soup.get_text(), re.I)
            if m:
                ean = m.group(1)

        tracked_value = float(shoptet.get("value") or 0) if shoptet else 0.0
        block_price = detail_price_amount(soup)
        meta_el = soup.find("meta", property="product:price:amount")
        meta_price = float(parse_price(meta_el.get("content")) or 0.0) if meta_el and meta_el.get("content") else 0.0
        # Zhoda s .p-code → value z trackingu (po logine je to B2B). Bez zhody nikdy banner.
        current = tracked_value if shoptet and tracked_value > 0 else (block_price or meta_price or 0.0)

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
                "protec_parser": PARSER_VERSION,
                "page_code": page_code,
            },
        )
