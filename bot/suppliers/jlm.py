#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JLM Lubricants — oficiálny importér SR (jlmaditiva.sk / AWD Auto).

Zdroj: Shoptet XML marketing feed (`JLM_FEED_URL`).

  python bot/import_pipeline.py --supplier jlm --limit 50 --cheap --force-daytime

Merch (tričká, samolepky, stojany, POS…) sa **preskakuje**.
PRICE_VAT vo feede = verejná MOC. Nákup odhad = MOC × JLM_COST_RATIO
(default 0.80, kým nie je VO cenník). Predaj = MOC (pass-through ako PRO-TEC).
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, Optional
from xml.etree.ElementTree import Element

from .base import BaseSupplier, ProductRaw, parse_price

LOGGER = logging.getLogger("suppliers.jlm")

DEFAULT_BASE = "https://www.jlmaditiva.sk"

# POS / merch / doplnky — nie chemické produkty pre e-shop koncového zákazníka.
_MERCH_NAME_RE = re.compile(
    r"("
    r"tri[cč]ko|polo\s*ko[sš]e[lľ]a|samolep|"
    r"stojan|šiltov|siltov|ruksak|banner|plag[aá]t|"
    r"visa[cč]ka|rukavic|katal[oó]g(\s+produkt)?|"
    r"mikrovlákn|mikrovlakn|vôňa\s+do\s+auta|vona\s+do\s+auta|"
    r"vrecko|podlo[zž]|protism|roll[\s-]?up|vintage|"
    r"predajň|do\s+predajne|oble[cč]enie|merch"
    r")",
    re.I,
)


def _text(el: Optional[Element]) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _child_text(parent: Element, tag: str) -> str:
    return _text(parent.find(tag))


def is_jlm_merch(name: str, categories: List[str], code: str = "") -> bool:
    """True = vynechať (merch / POS / doplnky bez chemického zmyslu)."""
    cats = [c or "" for c in categories]
    if cats and all("doplnkov" in c.lower() for c in cats):
        return True
    blob = f"{name} {code}"
    if _MERCH_NAME_RE.search(blob):
        return True
    return False


class JlmSupplier(BaseSupplier):
    name = "jlm"
    base_url = DEFAULT_BASE

    def __init__(self, http=None, credentials=None):
        super().__init__(http=http, credentials=credentials or {})
        self._by_url: Dict[str, ProductRaw] = {}
        self._ordered_urls: List[str] = []
        self._loaded = False

    def _feed_url(self) -> str:
        url = (
            (self.credentials.get("feed_url") or "").strip()
            or os.getenv("JLM_FEED_URL", "").strip()
            or os.getenv("JLM_XML_URL", "").strip()
        )
        if not url:
            raise RuntimeError(
                "Chýba JLM_FEED_URL (XML feed z jlmaditiva.sk — hash ostáva v secrets)."
            )
        return url

    def _cost_ratio(self) -> float:
        try:
            ratio = float(os.getenv("JLM_COST_RATIO", "0.80"))
        except ValueError:
            ratio = 0.80
        return min(max(ratio, 0.40), 0.99)

    def _load_feed(self) -> None:
        if self._loaded:
            return
        feed_url = self._feed_url()
        LOGGER.info("JLM XML feed: %s…", feed_url.split("hash=")[0] + "hash=***")
        resp = self.http.get(feed_url)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ratio = self._cost_ratio()
        skipped_merch = 0
        skipped_bad = 0

        for item in root.findall("SHOPITEM"):
            raw = self._item_to_product(item, ratio)
            if raw is None:
                # None = merch alebo nepoužiteľné
                name = _child_text(item, "NAME")
                cats = self._categories(item)
                if is_jlm_merch(name, cats, _child_text(item, "CODE")):
                    skipped_merch += 1
                else:
                    skipped_bad += 1
                continue
            url = raw.supplier_url
            if not url or url in self._by_url:
                continue
            self._by_url[url] = raw
            self._ordered_urls.append(url)

        self._loaded = True
        LOGGER.info(
            "JLM feed: %d produktov (skip merch=%d, skip bad=%d)",
            len(self._ordered_urls),
            skipped_merch,
            skipped_bad,
        )

    @staticmethod
    def _categories(item: Element) -> List[str]:
        cats_el = item.find("CATEGORIES")
        if cats_el is None:
            return []
        out: List[str] = []
        for c in cats_el:
            if c.tag == "CATEGORY" and (c.text or "").strip():
                out.append(c.text.strip())
        return out

    def _item_to_product(self, item: Element, cost_ratio: float) -> Optional[ProductRaw]:
        name = _child_text(item, "NAME")
        code = _child_text(item, "CODE") or _child_text(item, "EXTERNAL_CODE")
        cats = self._categories(item)
        if not name:
            return None
        if is_jlm_merch(name, cats, code):
            return None

        moc = parse_price(_child_text(item, "PRICE_VAT")) or 0.0
        if not code or moc <= 0.05:
            return None

        url = _child_text(item, "ORIG_URL") or f"{self.base_url}/?code={code}"
        ean = _child_text(item, "EAN")
        brand = _child_text(item, "MANUFACTURER") or "JLM"
        short = _child_text(item, "SHORT_DESCRIPTION")
        desc = _child_text(item, "DESCRIPTION") or short

        images: List[str] = []
        imgs = item.find("IMAGES")
        if imgs is not None:
            for im in imgs.findall("IMAGE"):
                u = _text(im)
                if u and u not in images:
                    images.append(u)
        ref = _child_text(item, "IMAGE_REF")
        if ref and ref not in images:
            images.insert(0, ref)

        stock_el = item.find("STOCK")
        qty: Optional[float] = None
        if stock_el is not None:
            qty = parse_price(_child_text(stock_el, "AMOUNT"))
        if qty is None:
            qty = 0.0
        if qty > 0:
            stock_status = "instock"
            delivery = _child_text(item, "AVAILABILITY_IN_STOCK") or "Skladom"
        else:
            stock_status = "onbackorder"
            delivery = _child_text(item, "AVAILABILITY_OUT_OF_STOCK") or "Na objednávku"

        action = parse_price(_child_text(item, "ACTION_PRICE")) or 0.0
        standard = parse_price(_child_text(item, "STANDARD_PRICE")) or 0.0
        retail_hint = moc
        sale = ""
        regular = f"{moc:.2f}"
        if action > 0 and action < moc:
            retail_hint = action
            sale = f"{action:.2f}"
            regular = f"{moc:.2f}"
        elif standard > moc + 0.009 and standard <= moc * 5:
            retail_hint = moc
            sale = f"{moc:.2f}"
            regular = f"{standard:.2f}"

        cost = round(float(retail_hint) * cost_ratio, 2)
        default_cat = ""
        cats_el = item.find("CATEGORIES")
        if cats_el is not None:
            default_cat = _child_text(cats_el, "DEFAULT_CATEGORY") or (cats[0] if cats else "")

        return ProductRaw(
            name=name,
            source=self.name,
            supplier_url=url,
            sku=code,
            ean=ean,
            brand=brand,
            description=desc,
            short_description=short,
            regular_price=regular,
            sale_price=sale,
            cost_price=f"{cost:.2f}",
            stock_status=stock_status,
            stock_qty=qty,
            delivery_text=delivery,
            existing_images=images,
            status="draft",
            currency=_child_text(item, "CURRENCY") or "EUR",
            attributes={
                "supplier_category": default_cat or (cats[0] if cats else ""),
                "categories": cats,
            },
            raw_extra={
                "_sg_supplier": "jlm",
                "_sg_supplier_art_no": code,
                "retail_hint": retail_hint,
                "media_source": "jlm-xml",
                "media_original_url": images[0] if images else "",
                "catalog_url": url,
                "jlm_cost_ratio": cost_ratio,
                "jlm_cost_estimated": True,
            },
        )

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        self._load_feed()
        urls = list(self._ordered_urls)
        if limit and limit > 0:
            return urls[: int(limit)]
        return urls

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        self._load_feed()
        key = (url or "").strip()
        if key in self._by_url:
            return self._by_url[key]
        # fallback: match by code query / trailing slug
        for u, product in self._by_url.items():
            if key.rstrip("/") == u.rstrip("/"):
                return product
            if product.sku and f"code={product.sku}" in key:
                return product
        return None

    def iter_catalog(self, limit: Optional[int] = None) -> Iterator[ProductRaw]:
        for url in self.discover_product_urls(limit=limit):
            product = self._by_url.get(url)
            if product and product.name:
                yield product
