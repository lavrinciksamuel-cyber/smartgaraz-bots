#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ideo s.r.o. (ideo.sk, Bratislava) — náradie a chémia pre detailerov / wrap / PPF.

Dealer login (IDEO_USER / IDEO_PASSWORD, Nette form). Produkt = rodina s variantmi
(tabuľka `table-variant`) → každý variant = samostatný SmartGaráž koncept (SKU ART…).
Cena: „Cena za ks bez DPH“ + tooltip s DPH → náklad = cena s DPH (nie sme platcovia),
predaj = náklad × 1,30, nahor na ,90 (pricing.calculate_retail_ideo).
Sklad: „Skladom“ → SR (2–3 prac. dni), „Na objednávku“ → onbackorder (cheap import
ho preskočí). Kategórie: len explicitná mapa nižšie, inak bez kategórie (koncept).
Tempo: ≥1,5 s medzi požiadavkami, žiadne obchádzanie ochrán.
"""
from __future__ import annotations

import html as _html
import os
import re
import time
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseSupplier, ProductRaw

BASE = "https://ideo.sk"
# Seed kategórie relevantné pre detailing (nie fólie na metre, nie reklama/tlač).
SEEDS = (
    "/carwrap-a-tuning/autokozmetika/",
    "/prislusenstvo/chemia-a-cistenie/",
    "/prislusenstvo/aplikacne-nastroje/",
    "/yellotools/",
    "/prislusenstvo/rezanie/",
    "/prislusenstvo/pomocky-rozne/",
)
L1 = "Autokozmetika & Detail"
# Poradie = priorita. Bez zhody → žiadna kategória (žiadny fallback).
RULES: Tuple[Tuple[str, str], ...] = (
    (r"microfib|mikrovl|utierk|handri|cloth|towel", "Mikrovlákno & utierky"),
    (r"riwax|vosk|wax|sealant(?!.*edge)|clay|lešt|polish|compound|pad\b", "Leštenie & vosky"),
    (r"primer|adhesion promoter|aplika[čc]n[ýy] gel|ppf.*gel|edge seal|seal-it|3950|ipa sealant|"
     r"isoprop|alkohol|adhesive remover|odstra[ňn]ova|vhb cleaner|industry cleaner|wrap care", "Chémia pre fólie & PPF"),
    (r"odstra[ňn]ova[čc] (lepid|zvy[šs]k)|surface prep|montážn|aplika[čc]n[áa] (chémia|tekut)|šampón na aplik|slide-?on|film-?on|right ?o(n|ff)|ad-gel|industrial cleaner", "Chémia pre fólie & PPF"),
    (r"cleaner|[čc]isti[čc]|power clean", "Exteriér & umývanie"),
    (r"stierk|squeeg|aplik[áa]tor|roller|handroller|tweezer|pinzet|cutter|n[ôo][žz]|[čc]epel|blade|"
     r"reza[čc]|olfa|knifeless|edge finisher|wrapstick|gecko|wrapdragon|yellotwin|rubberlift|angelracer|"
     r"sassercut|škrabk|skrabk|rukavic|^rozpra[šs]ova|striekačk|jetspray|pravítk|pravitk|filcov|nožnic|noznic", "Detailingové náradie"),
)


def ideo_category(name: str) -> Optional[List[str]]:
    n = (name or "").lower()
    for pat, sub in RULES:
        if re.search(pat, n):
            return [L1, sub]
    return None


# Podkategórie ideo, ktoré sú zároveň „rodinou“ s variantmi rôznych značiek → nie sú súčasťou názvu.
GENERIC_FAMILY = re.compile(
    r"^(?:čistiaca|aplikačná) chémia$|^stierky\b|^utierky\b|^aplikátory$|^rezače\b|^náhradné čepele$|^nožnice\b|"
    r"^pravítka$|^rozprašovače$|^rukavice$|^iné pomôcky$|^filcové náhrady$|^škrabky$|^odstraňovanie fólií$|"
    r"^olfa príslušenstvo$|^rezacia podložka", re.I)


def clean_name(family: str, vname: str, spec: str) -> Tuple[str, str]:
    """(názov, značka). Generická rodina → „Popis (skrátený) Variant“, inak „Rodina Variant Balenie“."""
    spec = re.sub(r"\s+", " ", spec or "").strip()
    if GENERIC_FAMILY.search((family or "").strip()):
        head = re.split(r"\s[–-]\s|[.;,(]", spec, maxsplit=1)[0].strip()
        if len(head) > 70:
            head = head[:70].rsplit(" ", 1)[0]
        head = head[:1].upper() + head[1:] if head else ""
        name = " ".join(x for x in (head, vname) if x)
        if vname and head and vname.lower() in head.lower():
            name = head
        brand = re.split(r"[-\s]", vname or "", maxsplit=1)[0]
        brand = brand if len(brand) >= 3 and brand[:1].isupper() and brand.isalpha() else ""
        return name.strip() or vname or family, brand
    extra = vname if vname and vname.lower() not in family.lower() else ""
    return " ".join(x for x in (family, extra, spec) if x).strip(), (family.split()[0] if family else "")


def _num(s: str) -> Optional[float]:
    m = re.search(r"(\d[\d\s]*[.,]\d{2})", s or "")
    return float(m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")) if m else None


class IdeoSupplier(BaseSupplier):
    name = "ideo"

    def __init__(self, http=None, credentials=None):
        super().__init__(http=http, credentials=credentials or {})
        self.http.delay = max(float(getattr(self.http, "delay", 0) or 0), float(os.getenv("IDEO_DELAY", "1.5")))
        self._logged_in = False
        self._page_cache: Dict[str, str] = {}

    # ------------------------------------------------------------ login
    def ensure_login(self) -> bool:
        if self._logged_in:
            return True
        u, p = self.credentials.get("user"), self.credentials.get("password")
        if not u or not p:
            return False
        url = BASE + SEEDS[0]
        page = self.http.get(url).text or ""
        m = re.search(r'name="_token_" value="([^"]+)"', page)
        data = {"email": u, "password": p, "do": "loginForm-submit"}
        if m:
            data["_token_"] = m.group(1)
        r = self.http.session.post(url, data=data, timeout=30)
        self._logged_in = "odhlás" in (r.text or "").lower()
        return self._logged_in

    def _get(self, path: str) -> str:
        url = urljoin(BASE, path)
        if url not in self._page_cache:
            self._page_cache[url] = self.http.get(url).text or ""
        return self._page_cache[url]

    @staticmethod
    def _content(h: str) -> str:
        i = h.find("<h1")
        j = h.find("ideo s.r.o., Vajnorská", i)
        return h[i:j] if i >= 0 else ""

    # ------------------------------------------------------------ crawl
    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        self.ensure_login()
        seen, out = set(), []
        stack = [(s, 0) for s in SEEDS]
        while stack:
            path, depth = stack.pop(0)
            if path in seen:
                continue
            seen.add(path)
            h = self._get(path)
            if "table-variant" in h and depth > 0:
                out.append(urljoin(BASE, path))
                if limit and len(out) >= limit:
                    break
                continue
            body = self._content(h)
            for l in re.findall(r'href="(/[a-z0-9][^"#?]*)"', body):
                if l.endswith(".html") or l in seen or l.startswith(("/nakupny-kosik", "/porovnat")):
                    continue
                if depth < 3:
                    stack.append((l, depth + 1))
        return out

    def iter_catalog(self, limit: Optional[int] = None) -> Iterator[ProductRaw]:
        n = 0
        for url in self.discover_product_urls(None):
            for p in self.parse_variants(url):
                yield p
                n += 1
                if limit and n >= limit:
                    return

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        """URL `…#ART12345` → presne ten variant (cena/sklad sync). Bez fragmentu len 1-variantová rodina."""
        base, _, art = url.partition("#")
        v = self.parse_variants(base)
        if art:
            return next((p for p in v if p.sku == f"IDEO-{art}"), None)
        return v[0] if len(v) == 1 else None

    def parse_variants(self, url: str) -> List[ProductRaw]:
        url = url.split("#", 1)[0]
        h = self._get(url)
        soup = BeautifulSoup(h, "html.parser")
        h1 = soup.find("h1", attrs={"itemprop": "name"}) or soup.find("h1")
        family = h1.get_text(" ", strip=True) if h1 else ""
        desc = ""
        if h1:
            nxt = h1.find_next_sibling("p") or h1.find_next("p")
            desc = nxt.get_text(" ", strip=True)[:1200] if nxt else ""
        out: List[ProductRaw] = []
        table = soup.select_one("table.table-variant")
        if not table:
            return out
        for tr in table.select("tbody tr[data-search-string]"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            strong = tds[1].find("strong")
            art = (strong.get("title") or "").strip() if strong else ""
            vname = strong.get_text(" ", strip=True) if strong else ""
            status = (tr.get("data-product-status") or "").strip()
            spec = tds[2].get_text(" ", strip=True)
            price_el = tds[3].find(attrs={"itemprop": "price"})
            net = _num(price_el.get_text(" ", strip=True)) if price_el else None
            gross = _num(price_el.get("title") or "") if price_el else None
            if gross is None and net is not None:
                gross = round(net * 1.23, 2)
            tier = tds[3].find("span")
            tier_txt = tier.get_text(" ", strip=True) if tier else ""
            img = tds[0].find("img")
            img_url = urljoin(BASE, img.get("data-original") or img.get("src") or "") if img else ""
            if not art or gross is None:
                continue
            name, brand = clean_name(family, vname, spec)
            instock = status.lower().startswith("skladom")
            cat = ideo_category(f"{family} {vname} {spec}")
            out.append(ProductRaw(
                name=name, source="ideo", supplier_url=f"{url}#{art}", sku=f"IDEO-{art}", brand=brand,
                description=desc, short_description=desc[:240], cost_price=f"{gross:.2f}",
                stock_status="instock" if instock else "onbackorder",
                delivery_text="2–3 pracovné dni" if instock else "na objednávku",
                attributes={"Balenie": spec} if spec else {},
                existing_images=[img_url] if img_url else [],
                raw_extra={"_sg_supplier": "ideo", "_sg_supplier_art_no": art, "catalog_url": url,
                           "price_basis": f"ideo dealer {net} € bez DPH / {gross} € s DPH ({tier_txt})",
                           "unit_cost": gross, "stock_text": status, "sg_category_path": cat or [],
                           "media_source": "ideo.sk", "media_allowed": "pending"},
            ))
        return out
