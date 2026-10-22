#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Szekop (Nextis/TecDoc) — https://szekopsk.nextis.cz/

DEFAULT = VEREJNÝ režim (bez loginu):
  - crawl anonymného HTML katalógu
  - nákupná cena z `data-flex-purchase-price`
  - maloobchod z `data-flex-retail-price`
  - sklad z `.flex-stocks` (súčet BA + distribučný, ak sú)

B2B login (nižšia nákupná) zapni len explicitne: env `SZEKOP_B2B=1`
(+ platné `SZEKOP_USER` / `SZEKOP_PASSWORD`). Inak sa login vôbec neskúša
(žiadny spam „Neplatné užívateľské meno…“).

DÔLEŽITÉ o štruktúre stránky: detail produktu vyzerá takto:

  /sk/katalog/univerzalne-diely/<slug-kategorie>/<slug-produktu>/<vyrobca>/
      <interny-kod>/<id-cesty-kategorii-oddelene-vlnovkou>/<ciselne-id-produktu>

Produkt sa pozná podľa KONCOVKY cesty: `/<čísla~oddelené~vlnovkou>/<čisté číslo>`.
Zoznam kategórií cez `category_url` / env `SZEKOP_CATEGORY_URL` (čiarkou viac).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .base import LoginMixin, OfferSnapshot, ProductRaw, absolutize, extract_b2b_signals, parse_price, same_host
from .generic import GenericSupplier

LOGGER = logging.getLogger("suppliers.szekop")

# Koncovka cesty typická pre Szekop produkt: .../19~20~42~363/18255396
_PRODUCT_PATH_RE = re.compile(r"/\d+(?:~\d+)*/\d+/?$")
# TecDoc detail: …/{mfr}/{model}/{engine}/{cat}/{articleLink}/{groupId}
_TECDOC_PRODUCT_PATH_RE = re.compile(
    r"/tecdoc/.+/\d+/\d+/\d+/\d+/-?\d+/\d+/?$",
    re.I,
)
_STOCK_KS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*ks", re.I)
_PRODUCT_ID_RE = re.compile(r"/(\d+)/?$")
_TECDOC_SVC = "/TecDoc/TecDocSvcLegacy"

# Fitment ingest poznámka:
# - Default oil kategórie: žiadny make/model v HTML; OE tab často drží
#   olejové špecifikácie (WSS-… pod CAS) — filter v extract_oe_from_szekop_html
#   ich zahodí → fitment ostáva prázdny (správne pre univerzáne náplne).
# - Diely: GetProductDetailOENumbers vracia flex-manufacturer + flex-number;
#   berieme len čísla pri známych auto-značkách. Vehicle application tree /
#   TecDoc® číslo / path ID (~) neukladáme.
# - TecDoc leaf: SSR HTML + GetTecDocConstructionGroupsSubcategories expand;
#   fitment zo slugov skoda/octavia-iii/1-6-tdi (žiadne path ID do Woo).

# Motorové oleje — všetky bežné viskozity (+ značkové listy ako doplnok).
# Path ID z verejného katalógu Nextis (overené 2026-08).
DEFAULT_CATEGORY_URLS = [
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/0w20/19~20~42~363/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/0w30/19~20~42~43/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/0w40/19~20~42~44/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/5w20/19~20~42~296/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/5w30/19~20~42~45/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/5w40/19~20~42~46/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/10w40/19~20~42~47/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/10w60/19~20~42~178/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/15w40/19~20~42~48/",
    # Značkové listy (verejné; doplnia balenia mimo viskozitných listov)
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/castrol/19~20~42/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/mobil/19~20~28/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/shell/19~20~21/",
    "https://szekopsk.nextis.cz/sk/katalog/univerzalne-diely/total/19~20~49/",
]

# TecDoc vozidlové diely — entry (nie default crawl). Leaf cez --category-url.
# Top category-id mapa: bot/data/parts_pilot_seeds.json
PARTS_TECDOC_ENTRY = "https://szekopsk.nextis.cz/sk/katalog/tecdoc/osobne"
# Overený quiet-drip leaf (Octavia III 1.6 TDI — olejové filtre), 2026-08-03
DEFAULT_PARTS_LEAF_URL = (
    "https://szekopsk.nextis.cz/sk/katalog/tecdoc/osobne/skoda/"
    "octavia-iii-5e3-nl3-nr3/1-6-tdi/olejovy-filter/106/11195/58760/100259/"
    "?path=100005~100259"
)
DEFAULT_PARTS_CATEGORY_URLS: list[str] = []


def is_tecdoc_product_url(url: str) -> bool:
    path = urlparse(url.split("?", 1)[0]).path
    return bool(_TECDOC_PRODUCT_PATH_RE.search(path))


def is_tecdoc_catalog_url(url: str) -> bool:
    path = urlparse(url.split("?", 1)[0]).path.lower()
    return "/katalog/tecdoc/" in path


def fitment_slug_from_tecdoc_url(url: str) -> str:
    """Poskladá `skoda-octavia-iii-…-1-6-tdi` zo segmentov TecDoc URL (bez ID)."""
    parts = [p for p in urlparse(url.split("?", 1)[0]).path.split("/") if p]
    # …/tecdoc/osobne/<make>/<model>/<engine>/…
    try:
        i = next(idx for idx, p in enumerate(parts) if p.lower() == "tecdoc")
    except StopIteration:
        return ""
    # osobne | make | model | engine
    segs = parts[i + 1 :]
    if len(segs) < 4:
        return ""
    # skip vehicleType slug (osobne) if present
    if segs[0].lower() in ("osobne", "nakladne", "1"):
        segs = segs[1:]
    if len(segs) < 3:
        return ""
    make_s, model_s, engine_s = segs[0], segs[1], segs[2]
    if not re.search(r"[a-z]", make_s, re.I):
        return ""
    if re.fullmatch(r"\d+", engine_s):
        return ""
    return f"{make_s}-{model_s}-{engine_s}".lower()


def _is_fluid_product_name(name: str) -> bool:
    """True pre náplne/oleje; False pre olejové filtre a diely s 'olej' v názve."""
    name_l = (name or "").lower()
    if re.search(r"filter|filtre|filtr\b", name_l):
        return False
    return any(
        k in name_l
        for k in (
            "olej", "oil", "kvapalin", "fluid", "aditív", "aditiv",
            "cleaner", "výplach", "vyplach", "antifreeze", "nemrzn",
        )
    )


def _b2b_enabled() -> bool:
    return os.getenv("SZEKOP_B2B", "").strip().lower() in ("1", "true", "yes", "on")


def _parse_stock_qty(soup) -> Tuple[Optional[float], str]:
    """Súčet kusov z `.flex-stocks` (BA + distribučný). Vráti (qty, text)."""
    block = soup.select_one(".flex-stocks")
    if not block:
        return None, ""
    text = block.get_text(" ", strip=True)
    total = 0.0
    found = False
    for m in _STOCK_KS_RE.finditer(text):
        try:
            total += float(m.group(1).replace(",", "."))
            found = True
        except ValueError:
            continue
    if not found:
        return None, text
    return total, text


class SzekopSupplier(LoginMixin, GenericSupplier):
    name = "szekop"
    base_url = "https://szekopsk.nextis.cz"

    def __init__(self, http=None, credentials=None):
        creds = dict(credentials or {})
        creds.setdefault("login_url", "https://szekopsk.nextis.cz/")
        # Ak je nastavené api_url ako root, použi ho ako base
        base = (creds.get("api_url") or self.base_url).rstrip("/")
        if base.endswith("/api"):
            base = self.base_url
        super().__init__(base_url=base, http=http, credentials=creds, source_name="szekop")
        self._logged_in = False
        self._public_only = not _b2b_enabled()

    def _category_urls(self) -> List[str]:
        raw = (self.credentials or {}).get("category_url") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        return urls or list(DEFAULT_CATEGORY_URLS)

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        # Verejný default: bez loginu. B2B len pri SZEKOP_B2B=1.
        if not self._public_only:
            self.ensure_login()
        found: Set[str] = set()
        host = urlparse(self.base_url).netloc

        for seed in self._category_urls():
            if limit and len(found) >= limit:
                break
            try:
                soup = self.soup(seed)
            except Exception:
                continue
            for a in soup.find_all("a", href=True):
                href = absolutize(seed, a["href"]).split("#")[0]
                href_no_query = href.split("?")[0]
                if not same_host(href, host):
                    continue
                path = urlparse(href_no_query).path
                path_l = path.lower()
                if is_tecdoc_catalog_url(href_no_query):
                    # Len plný TecDoc detail (6× ID), nie engine/leaf category
                    if not is_tecdoc_product_url(href_no_query):
                        continue
                elif not _PRODUCT_PATH_RE.search(path_l):
                    continue
                found.add(href_no_query)
                if limit and len(found) >= limit:
                    break

        if not found:
            # Fallback: plytká homepage heuristika
            seeds = [self.base_url + "/", f"{self.base_url}/katalog", f"{self.base_url}/products"]
            for seed in seeds:
                try:
                    soup = self.soup(seed)
                except Exception:
                    continue
                for a in soup.find_all("a", href=True):
                    href = absolutize(seed, a["href"]).split("?")[0]
                    if not same_host(href, host):
                        continue
                    path = urlparse(href).path.lower()
                    if any(k in path for k in ("/product", "/produkt", "/tovar", "/item", "/detail")) or _PRODUCT_PATH_RE.search(path):
                        found.add(href)
                        if limit and len(found) >= limit:
                            return sorted(found)
        if not found:
            return super().discover_product_urls(limit=limit)
        ordered = sorted(found)
        # Offset medzi behmi (SG_DISCOVER_OFFSET) — hlbšie stránky / ďalšie SKU
        try:
            import os as _os
            offset = int(_os.environ.get("SG_DISCOVER_OFFSET") or "0")
        except ValueError:
            offset = 0
        if offset < 0:
            offset = 0
        if offset:
            ordered = ordered[offset:]
        return ordered[: limit or None]

    def ensure_login(self) -> bool:
        """B2B login len keď SZEKOP_B2B=1. Inak vždy False (verejný režim)."""
        if self._public_only:
            return False
        if self._logged_in:
            return True
        user = (self.credentials or {}).get("user")
        password = (self.credentials or {}).get("password")
        if not user or not password:
            LOGGER.warning("SZEKOP_B2B=1, ale chýba SZEKOP_USER/PASSWORD — pokračujem verejne.")
            return False
        try:
            self.http.get(self.base_url + "/")
            resp = self.http.post(
                self.base_url + "/MyAccount/Authentication/Login",
                data={"login": user, "password": password, "redirectUrl": "/"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            data = resp.json()
            self._logged_in = bool(data.get("IsAuthenticated"))
            if not self._logged_in:
                LOGGER.warning(
                    "Szekop B2B login zlyhal: %s — fallback na verejný katalóg",
                    data.get("ErrorText") or "neznáma chyba",
                )
            return self._logged_in
        except Exception as exc:
            LOGGER.warning("Szekop B2B login výnimka: %s — fallback na verejný katalóg", exc)
            self._logged_in = False
            return False

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        if not self._public_only:
            self.ensure_login()
        try:
            soup = self.soup(url)
        except Exception:
            return None

        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else ""
        if not name:
            return None

        brand_el = soup.select_one(".flex-manufacturer .flex-value")
        brand = brand_el.get_text(strip=True) if brand_el else "Szekop"

        sku = ""
        code_input = soup.select_one(".flex-code input.flex-value")
        if code_input and code_input.get("value"):
            sku = code_input["value"].strip()

        # TecDoc listing pages leak category/vehicle nodes as "products"
        # (e.g. "Filtre", "OCTAVIA II (1Z3)", "1.9 TDI") — no real SKU.
        if not sku:
            LOGGER.debug("Szekop skip (no SKU): %s", name[:80])
            return None
        if re.match(
            r"^(filtre|oleje|brzdy|podvozok|karos[eé]ria|elektrika|prevodovka|"
            r"chladiaci syst[eé]m|v[yý]fuk|\d+\.\d+\s*tdi|octavia|fabia|"
            r"golf|passat|rapid|superb)\b",
            name,
            re.I,
        ):
            LOGGER.info("Szekop skip category/vehicle node: %s", name[:80])
            return None

        price_el = soup.select_one(".flex-price-with-vat")
        purchase = None
        retail = None
        if price_el:
            purchase = parse_price(price_el.get("data-flex-purchase-price"))
            retail = parse_price(price_el.get("data-flex-retail-price") or price_el.get_text())
            if purchase is None and retail is not None:
                purchase = retail
            if retail is None and purchase is not None:
                retail = purchase

        desc_el = soup.select_one(".flex-description, .product-description, [itemprop=description]")
        description = desc_el.get_text("\n", strip=True) if desc_el else ""

        stock_qty, stock_text = _parse_stock_qty(soup)
        stock_status = "instock" if (stock_qty or 0) > 0 else "outofstock"
        if stock_qty is None:
            # fallback text
            blob = soup.get_text(" ", strip=True).lower()
            stock_status = "outofstock" if any(k in blob for k in ("nedostupné", "vypredané")) else "instock"

        # B2B: nákupná → cost_price (MOC dopočíta marža).
        # Verejný: nákupná aj retail sú v HTML — cost_price = purchase (meta/sync),
        # regular_price = retail (bez marže navrch; anonym často purchase==retail).
        if self._logged_in:
            regular_price = ""
            cost_price = str(purchase) if purchase else ""
        else:
            regular_price = str(retail) if retail else (str(purchase) if purchase else "")
            cost_price = str(purchase) if purchase else ""

        attrs = self._parse_basic_params(soup)
        images = self._parse_images(soup)
        product = ProductRaw(
            name=name,
            source=self.name,
            supplier_url=url,
            sku=sku,
            brand=brand,
            description=description,
            regular_price=regular_price,
            cost_price=cost_price,
            stock_status=stock_status,
            stock_qty=stock_qty,
            delivery_text=stock_text or "",
            existing_images=images,
            attributes=attrs,
        )
        # Dostupnosť SR/EÚ: sklad BA (Bratislava) vs. distribučný (availability.py)
        try:
            from availability import parse_szekop_stocks
            _ba, _dist, _st = parse_szekop_stocks(soup)
            product.raw_extra = dict(product.raw_extra or {})
            product.raw_extra.update({"stock_ba": _ba, "stock_dist": _dist, "stock_text": _st})
        except Exception:  # noqa: BLE001
            pass

        try:
            b2b = extract_b2b_signals(soup)
            if b2b.get("cost_price") and self._logged_in and not product.cost_price:
                product.cost_price = str(b2b["cost_price"])
            if b2b.get("stock_qty") is not None and product.stock_qty is None:
                product.stock_qty = b2b["stock_qty"]
            if b2b.get("delivery_days") is not None:
                product.delivery_days = b2b["delivery_days"]
                product.delivery_text = b2b.get("delivery_text") or product.delivery_text
        except Exception:
            pass

        return self._apply_fitment(product, soup, url)

    def _parse_basic_params(self, soup) -> Dict[str, Any]:
        """flex-basic-params / label-value páry → attributes (bez TecDoc ID)."""
        out: Dict[str, Any] = {}
        root = soup.select_one(".flex-basic-params") or soup.select_one(".flex-informations")
        if not root:
            return out
        # Často: <div class="flex-label">Výrobca</div><div class="flex-value">…
        labels = root.select(".flex-label, .flex-name, dt")
        for lab in labels:
            key = lab.get_text(" ", strip=True)
            if not key:
                continue
            # Skip TecDoc vehicle / article DB labels — číslo dielu OK ako sku, nie fitment ID
            if re.search(r"tecdoc", key, re.I):
                continue
            sib = lab.find_next_sibling()
            val = sib.get_text(" ", strip=True) if sib else ""
            if key and val and key.lower() not in ("kód", "kod", "code"):
                out[key] = val
        return out

    def _parse_images(self, soup) -> List[str]:
        """TecDoc lazy obrázky (`data-flex-async-image-src`) + bežné src."""
        found: List[str] = []
        seen: Set[str] = set()
        for img in soup.select("img"):
            raw = (
                img.get("data-flex-async-image-src")
                or img.get("data-src")
                or img.get("src")
                or ""
            ).strip()
            if not raw or raw.startswith("data:"):
                continue
            if any(x in raw.lower() for x in ("blank.gif", "logo-szekop", "placeholder", "spinner")):
                continue
            if raw.startswith("//"):
                raw = "https:" + raw
            elif raw.startswith("/"):
                raw = self.base_url.rstrip("/") + raw
            if not raw.startswith("http"):
                continue
            if raw in seen:
                continue
            seen.add(raw)
            found.append(raw)
            if len(found) >= 8:
                break
        return found

    def _fetch_oe_numbers(self, product_id: str, page_html: str = "") -> List[str]:
        # Nextis 2026: endpoint chce groupID + articleNumber + brandId z volania
        # getProductDetailOENumbers('..', 'GROUP', 'ARTICLE', 'BRAND') v HTML detailu.
        # S "-1" vracia 404 → importy boli bez OE.
        article, brand = "-1", "-1"
        m = re.search(r"OENumbers\(\s*'[^']*'\s*,\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'(\d+)'", page_html or "")
        if m:
            product_id, article, brand = m.group(1), m.group(2), m.group(3)
        if not product_id:
            return []
        try:
            from fitment import extract_oe_from_szekop_html

            resp = self.http.post(
                f"{self.base_url}/Products/ProductSvcLegacy/GetProductDetailOENumbers",
                data={"groupID": product_id, "articleNumber": article, "brandId": brand},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            return extract_oe_from_szekop_html(resp.text or "")
        except Exception as exc:
            LOGGER.debug("Szekop OE numbers skip: %s", exc)
            return []

    def _apply_fitment(self, product: ProductRaw, soup, url: str) -> ProductRaw:
        from fitment import (
            extract_fitment_from_attributes,
            merge_fitment_fields,
            parse_vehicle_suitability_text,
        )

        from_attrs = extract_fitment_from_attributes(product.attributes or {})
        suit_lines = []
        for line in (product.description or "").splitlines():
            if re.search(r"(?i)(?:vhodn[ée]\s+pre|použitie|kompatibilita|application|fits?)\s*[:\-]", line):
                suit_lines.append(line.strip())
        from_text = parse_vehicle_suitability_text("\n".join(suit_lines)) if suit_lines else {}

        m = _PRODUCT_ID_RE.search(urlparse(url).path)
        # Univerzáne oleje/chémia — OE tab = špecifikácie, nie fitment dielu.
        # Olejový filter ≠ náplň → OE berieme.
        is_fluid = _is_fluid_product_name(product.name or "")
        oe = [] if is_fluid else self._fetch_oe_numbers(m.group(1) if m else "", str(soup))

        from_slug = {}
        if is_tecdoc_catalog_url(url):
            from fitment import parse_fitment_from_catalog_slug

            slug = fitment_slug_from_tecdoc_url(url)
            if slug:
                from_slug = parse_fitment_from_catalog_slug(slug)

        # Engine codes from listing/detail (CLHA, …) — len do description attrs,
        # nie ako cudzie vehicle ID.
        eng_box = soup.select_one(".tecdoc-engines strong")
        if eng_box and from_slug.get("engine") and not product.engine:
            pass  # keep slug engine; codes stay in page text for humans

        merged = merge_fitment_fields(
            {
                "make": product.make,
                "model": product.model,
                "engine": product.engine,
                "year_from": product.year_from,
                "year_to": product.year_to,
                "oe_numbers": oe,
            },
            from_slug,
            from_attrs,
            from_text,
        )
        # Oleje/chémia: make z „Výrobca=CASTROL“ nie je auto — drop ak nie je v hierarchii
        from fitment import load_vehicle_hierarchy, normalize_make

        makes = load_vehicle_hierarchy().get("makes") or {}
        cand_make = normalize_make(merged.get("make") or "")
        if cand_make and cand_make not in makes:
            merged["make"] = ""
            # model/engine bez make nemá zmysel z attributes výrobcu oleja
            if (product.attributes or {}).get("Výrobca") or (product.attributes or {}).get("Vyrobca"):
                merged["model"] = ""
                merged["engine"] = ""

        product.make = merged.get("make") or ""
        product.model = merged.get("model") or ""
        product.engine = merged.get("engine") or ""
        product.year_from = merged.get("year_from")
        product.year_to = merged.get("year_to")
        product.oe_numbers = list(merged.get("oe_numbers") or [])
        return product

    def fetch_offer(self, url: str) -> Optional[OfferSnapshot]:
        product = self.parse_product(url)
        if not product:
            return None
        cost = parse_price(product.cost_price)
        retail = parse_price(product.regular_price)
        # Verejný sync: ak purchase≈retail, nech sync neaplikuje maržu navrch
        # (price_from_offer by inak nafúkol MOC nad ich verejnú cenu).
        # cost ostáva pre meta; retail_hint riadi predaj (viď pricing.py).
        return OfferSnapshot(
            supplier_url=url,
            source=self.name,
            cost_price=cost,
            retail_hint=retail,
            regular_price=retail if not self._logged_in else None,
            stock_status=product.stock_status,
            stock_qty=product.stock_qty,
            delivery_days=product.delivery_days,
            delivery_text=product.delivery_text,
            sku=product.sku,
            name=product.name,
        )

    def expand_tecdoc_subcategories(
        self,
        engine_url: str,
        category_id: str,
        *,
        path: str | None = None,
        manufacturer: str = "Škoda",
        model: str = "OCTAVIA III (5E3, NL3, NR3)",
        engine: str = "1.6 TDI",
        manufacturer_id: int | None = None,
        model_id: int | None = None,
        engine_id: int | None = None,
        vehicle_type: int = 1,
    ) -> List[Dict[str, str]]:
        """Rozbal TecDoc skupinu → listy (GetTecDocConstructionGroupsSubcategories).

        Vráti [{id, name, path, href, leaf_url, end}]. Path ID len navigácia.
        """
        from bs4 import BeautifulSoup

        ids = _parse_tecdoc_route_ids(engine_url)
        mfr_id = manufacturer_id if manufacturer_id is not None else ids.get("manufacturer_id")
        mod_id = model_id if model_id is not None else ids.get("model_id")
        eng_id = engine_id if engine_id is not None else ids.get("engine_id")
        if not (mfr_id and mod_id and eng_id):
            LOGGER.warning("TecDoc expand: chýbajú route ID v URL %s", engine_url)
            return []

        # Warm session on engine page (cookies)
        try:
            self.http.get(engine_url.split("?")[0])
        except Exception:
            pass

        payload = {
            "id": int(category_id),
            "manufacturerName": manufacturer,
            "manufacturerID": int(mfr_id),
            "modelName": model,
            "modelID": int(mod_id),
            "engineName": engine,
            "engineID": int(eng_id),
            "vehicleType": int(vehicle_type),
            "fullCategoryIDsPath": path or str(category_id),
            "isShortcut": False,
        }
        try:
            resp = self.http.post(
                f"{self.base_url}{_TECDOC_SVC}/GetTecDocConstructionGroupsSubcategories",
                json=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": engine_url,
                    "Origin": self.base_url,
                },
            )
            data = resp.json()
        except Exception as exc:
            LOGGER.warning("TecDoc expand fail: %s", exc)
            return []

        html = data.get("ItemsHTMLContent") or ""
        soup = BeautifulSoup(html, "html.parser")
        engine_base = _tecdoc_engine_slug_prefix(engine_url)
        out: List[Dict[str, str]] = []
        for a in soup.select("a[data-node-id]"):
            nid = (a.get("data-node-id") or "").strip()
            name_el = a.select_one(".name")
            name = name_el.get_text(strip=True) if name_el else ""
            full_path = (a.get("data-full-category-ids-path") or "").strip()
            end = (a.get("data-is-end-node") or "").lower() == "true"
            href = a.get("href") or ""
            leaf_slug = _slug_from_tecdoc_href(href, name)
            leaf_url = ""
            if end and engine_base and leaf_slug and nid:
                leaf_url = (
                    f"{engine_base}/{leaf_slug}/{mfr_id}/{mod_id}/{eng_id}/{nid}/"
                    f"?path={full_path or nid}"
                )
            out.append(
                {
                    "id": nid,
                    "name": name,
                    "path": full_path,
                    "href": href,
                    "leaf_url": leaf_url,
                    "end": "1" if end else "0",
                }
            )
        return out


def _parse_tecdoc_route_ids(engine_url: str) -> Dict[str, int]:
    """Vyber manufacturer/model/engine ID z konca TecDoc engine URL."""
    path = urlparse(engine_url.split("?", 1)[0]).path.rstrip("/")
    nums = re.findall(r"/(\d+)", path)
    if len(nums) < 3:
        return {}
    return {
        "manufacturer_id": int(nums[-3]),
        "model_id": int(nums[-2]),
        "engine_id": int(nums[-1]),
    }


def _tecdoc_engine_slug_prefix(engine_url: str) -> str:
    """…/tecdoc/osobne/skoda/octavia-…/1-6-tdi  (bez trailing IDs)."""
    path = urlparse(engine_url.split("?", 1)[0]).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    # drop trailing numeric ids
    while parts and parts[-1].isdigit():
        parts.pop()
    if not parts:
        return ""
    return "https://szekopsk.nextis.cz/" + "/".join(parts)


def _slug_from_tecdoc_href(href: str, name: str) -> str:
    """Z API href alebo mena → leaf slug (olejovy-filter)."""
    path = urlparse(href.split("?", 1)[0]).path
    segs = [p for p in path.split("/") if p]
    # Segment tesne pred ID reťazcom (…/olejovy-filter/106/11195/…)
    for i, seg in enumerate(segs):
        if seg.isdigit() and i > 0:
            prev = segs[i - 1]
            if re.fullmatch(r"[a-z0-9-]+", prev, re.I) and any(c.isalpha() for c in prev):
                if prev.lower() not in ("sk", "katalog", "tecdoc", "osobne", "1"):
                    return prev.lower()
            break
    import unicodedata

    raw = unicodedata.normalize("NFKD", name or "")
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")

