#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LKQ SK — https://www.lkq.sk/

DÔLEŽITÉ: LKQ beží na proprietárnej Angular SPA platforme ("Auto Kelly" /
`akApp`) — produkty sa NIKDY neobjavia v statickom HTML (žiadny <a href>
na produkt), všetko sa načítava cez interné JSON API AŽ PO prihlásení a AŽ PO
otvorení konkrétnej kategórie (server si "pamätá" poslednú kategóriu v
session). Preto klasický BeautifulSoup <a> crawler (GenericSupplier) na tomto
webe vždy vráti 0 produktov.

Zistený funkčný postup (reverzované z /bundles/js bundlu):
  1) POST /account/logonmobilenow  {UserNameM, PasswordM}  — mobilný login
     formulár, jednoduchší než plný desktopový (ten má iné/menené názvy polí).
  2) GET  <category_url>  — ľubovoľná kategória z www.lkq.sk/Catalog/... .
     Toto nastaví server-side session "aktuálny katalóg", ownId sa z URL
     cesty ('39849642;38979037;51314417' na konci) prenesie automaticky.
  3) POST /ProductList/Items/AllInOne/{page}
     (header X-Requested-With: XMLHttpRequest, Referer=<category_url>)
     → JSON {"Items": [...], "Paging": {"Page","Size","Total","TotalCount"}}
     Každá ďalšia stránka (`page` = 1, 2, 3, …) vráti ďalších `Size` (~20) ks.

LKQ_LOGIN_URL / kategória sa dá nastaviť cez credentials["category_url"]
(env LKQ_CATEGORY_URL) — ak chýba, použije sa predvolená kategória
"Osobný automobil > Náplne > Výber motorového oleja" (viď DEFAULT_CATEGORY_URL).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .base import LoginMixin, ProductRaw, parse_price
from .generic import GenericSupplier
from brands import detect_true_brand

LOGGER = logging.getLogger("suppliers.lkq")

# Predvolené = motorové oleje (veľký oil import). Viac kategórií cez
# LKQ_CATEGORY_URL / credentials["category_url"] — čiarkou oddelené.
DEFAULT_CATEGORY_URL = (
    "https://www.lkq.sk/Catalog/osobny-automobil-naplne-vyber-motoroveho-oleja/"
    "39849642;38979037;51314417"
)

# Seed URL-ky pre autodiely (Heijunka / --category-url). NIE default crawl —
# bare `--supplier lkq` ostáva na olejoch (DEFAULT_CATEGORY_URL).
# Path ID = navigácia; Woo kategória = taxonomy.py.
DEFAULT_PARTS_CATEGORY_URLS: list[str] = [
    # Príklady overené 2026-08 (Octavia III / Golf VII) — pozri data/parts_pilot_seeds.json
    # "https://www.lkq.sk/Catalog/…-filtre/…",
]

# Fitment ingest poznámka (overené live 2026-08):
# - Default oil kategória: Items.Attributes=[], ProductList/Attributes→[],
#   Product/Codes→Oem=[] — fitment ostáva prázdny (správne pre univerzáne náplne).
# - Diely pod vehicle Catalog path: Attributes často stále prázdne/badge
#   („Novinka“); OE čísla sú v POST /Product/Codes/{id} → Oem[].
# - Make/model/years berieme ako voľný text zo slug kategórie (bez ;path IDs).
# - Nikdy neukladáme GaragePath / Catalog numeric IDs / kType.
#
# Cenotvorba (overené live 2026-08):
# - ProductList Price/PriceVat = podľa UI prepínača (Maloobchod/VOC) — NESPOĽAHLIVÉ
#   ako nákupná. Session môže byť na „Maloobchod“ → 16,77 namiesto Vaša cena 11,41.
# - Nákup (cost) = POST /UserPrice/Data → PriceVat („Vaša cena“ s DPH).
# - Predaj hint (MOC) = POST /RetailPrice/Data → PriceVat / PriceVatValue.
# - pricing.py zarovná predaj na retail_hint (ako PRO-TEC), cost ostáva B2B.


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _lkq_attrs_to_map(raw: Any) -> Dict[str, Any]:
    from fitment import normalize_attributes_map

    return normalize_attributes_map(raw)


class LkqSupplier(LoginMixin, GenericSupplier):
    name = "lkq"
    base_url = "https://www.lkq.sk"

    def __init__(self, http=None, credentials=None):
        creds = dict(credentials or {})
        # Vynúť správny mobilný login endpoint bez ohľadu na to, čo je (možno
        # zastarané, napr. LKQ_LOGIN_URL=".../Account/Login" z pôvodnej
        # generickej šablóny) nastavené v credentials/env — je len JEDEN
        # funkčný spôsob prihlásenia na LKQ a je zdokumentovaný v hlavičke
        # tohto súboru vyššie.
        creds["login_url"] = f"{self.base_url}/account/logonmobilenow"
        creds.setdefault("category_url", creds.get("category_url") or DEFAULT_CATEGORY_URL)
        super().__init__(base_url=self.base_url, http=http, credentials=creds, source_name="lkq")
        self._logged_in = False
        self._items_cache: Dict[str, Dict[str, Any]] = {}
        # Posledná category_url z discovery — slug = voľný text fitment kontext
        self._last_category_url: str = str(creds.get("category_url") or DEFAULT_CATEGORY_URL).split(",")[0].strip()
        self._fetch_codes: bool = True  # POST /Product/Codes/{id} pre OE

    # ---- Login (mobilný formulár, iné názvy polí ako generický mixin) ----

    def ensure_login(self) -> bool:
        if self._logged_in:
            return True
        user = (self.credentials or {}).get("user")
        password = (self.credentials or {}).get("password")
        if not user or not password:
            return False
        login_url = self.credentials.get("login_url") or f"{self.base_url}/account/logonmobilenow"
        # Lockout guard: po 1 neúspešnom logine v behu už LKQ nikto neskúša.
        import os as _os
        marker = _os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed")
        if _os.path.exists(marker) or _os.environ.get("SG_LKQ_DISABLED") == "1":
            return False
        # Jeden login na beh: session cookies zdieľané medzi jobmi (lokálny súbor
        # na runneri, nikdy sa neuploaduje). Ak session stále platí → bez POST loginu.
        import pickle as _pickle
        from requests.utils import cookiejar_from_dict, dict_from_cookiejar
        cookie_file = _os.environ.get("SG_LKQ_COOKIE_FILE", "/tmp/sg_lkq_session.pkl")
        if _os.path.exists(cookie_file):
            try:
                with open(cookie_file, "rb") as fh:
                    saved = _pickle.load(fh)
                if isinstance(saved, dict):
                    self.http.session.cookies.update(cookiejar_from_dict(saved))
                else:
                    # legacy CookieJar pickle
                    for c in list(saved):
                        try:
                            self.http.session.cookies.set_cookie(c)
                        except Exception:
                            pass
                body = (self.http.get(self.base_url + "/").text or "").lower()
                # rovnaké kritérium ako POST login (+ bez mobilného login formulára)
                if "logoff" in body or "odhlás" in body or (
                    'name="usernamem"' not in body and ("autokelly" in body or "lkq" in body)
                ):
                    self._logged_in = True
                    LOGGER.info("LKQ: zdieľaná session (bez nového loginu)")
                    return True
                LOGGER.info("LKQ: cookie súbor neplatný — nový login")
            except Exception as exc:
                LOGGER.info("LKQ: cookie restore zlyhal (%s) — nový login", exc)
        try:
            # 2026-10: GET /account/logonmobilenow = 404 (len POST); session nadviaž cez homepage.
            self.http.get(self.base_url + "/")
            resp = self.http.post(
                login_url,
                data={"UserNameM": str(user).strip(), "PasswordM": str(password).strip()},
                headers={"Referer": self.base_url + "/", "Origin": self.base_url},
            )
            # AutoKellyE60 cookie dostane aj anonym — úspech = zmizol login formulár / je odhlásenie.
            body = (resp.text or "").lower()
            self._logged_in = resp.status_code < 400 and (
                "logoff" in body or "odhlás" in body or 'name="usernamem"' not in body
            )
        except Exception:
            self._logged_in = False
        if self._logged_in:
            try:
                with open(cookie_file, "wb") as fh:
                    _pickle.dump(dict_from_cookiejar(self.http.session.cookies), fh)
                _os.chmod(cookie_file, 0o600)
            except Exception:
                pass
        if not self._logged_in:
            open(marker, "w").write("failed\n")
            LOGGER.error("LKQ login zlyhal — guard: žiadne ďalšie pokusy v tomto behu")
        return self._logged_in

    # ---- Discovery cez interné ProductList/Items JSON API -----------------

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        api_url = (self.credentials or {}).get("api_url")
        api_token = (self.credentials or {}).get("api_token")
        if api_url and api_token:
            try:
                resp = self.http.get(api_url, headers={"Authorization": f"Bearer {api_token}"})
                data = resp.json()
                items = data if isinstance(data, list) else data.get("items") or data.get("products") or []
                urls = []
                for item in items:
                    url = item.get("url") or item.get("detailUrl")
                    if url:
                        urls.append(url)
                    if limit and len(urls) >= limit:
                        break
                if urls:
                    return urls
            except Exception:
                pass

        if not self.ensure_login():
            # Soft-fail: VAG / Stellantis / Renault–Nissan diely idú aj cez
            # verejný Szekop (`--supplier szekop`). Bez LKQ_* nepadá celý pipeline.
            LOGGER.warning(
                "LKQ: bez prihlásenia — 0 produktov. "
                "Doplň LKQ_USER/LKQ_PASSWORD, alebo použi --supplier szekop (public)."
            )
            return []

        raw_cats = (self.credentials or {}).get("category_url") or DEFAULT_CATEGORY_URL
        category_urls = [u.strip() for u in str(raw_cats).split(",") if u.strip()]
        if not category_urls:
            category_urls = [DEFAULT_CATEGORY_URL]

        urls: List[str] = []
        seen: set = set()
        for category_url in category_urls:
            if limit and len(urls) >= limit:
                break
            self._last_category_url = category_url
            try:
                self.http.get(category_url)
            except Exception as exc:
                raise RuntimeError(f"LKQ: nepodarilo sa otvoriť kategóriu {category_url}: {exc}")

            page = 1
            max_pages = 40  # ~20 ks/stránka → až ~800 na kategóriu (oil import)
            while (limit is None or len(urls) < limit) and page <= max_pages:
                try:
                    resp = self.http.post(
                        f"{self.base_url}/ProductList/Items/AllInOne/{page}",
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": category_url},
                    )
                    data = resp.json()
                except Exception:
                    break
                items = data.get("Items") or []
                if not items:
                    break

                # LKQ vedia interná cenová služba občas vrátiť prázdne Price/PriceVat
                # pre CELÚ stránku výsledkov — retry max 2×.
                for _retry in range(2):
                    if any(item.get("Price") for item in items):
                        break
                    try:
                        resp = self.http.post(
                            f"{self.base_url}/ProductList/Items/AllInOne/{page}",
                            headers={"X-Requested-With": "XMLHttpRequest", "Referer": category_url},
                        )
                        retry_data = resp.json()
                        retry_items = retry_data.get("Items") or []
                        if retry_items:
                            items = retry_items
                    except Exception:
                        break

                # Doplň Attributes z ProductList/Attributes (list item Attributes[]
                # býva prázdne aj keď detail badge/spec existuje).
                self._enrich_items_attributes(items)
                # B2B UserPrice + Maloobchod RetailPrice — nie session PriceType.
                self._enrich_items_prices(items)

                for item in items:
                    link = item.get("Link")
                    if not link:
                        continue
                    # Po enrich: stačí UserPrice/RetailPrice; list Price môže byť prázdne
                    # (LoadUserPrice) alebo maloobchod podľa UI prepínača.
                    has_cost = bool(
                        item.get("_sg_user_price_vat")
                        or item.get("PriceVat")
                        or item.get("Price")
                    )
                    if not has_cost:
                        continue
                    full_url = urljoin(self.base_url, link)
                    if full_url in seen:
                        continue
                    seen.add(full_url)
                    item = dict(item)
                    item["_sg_category_url"] = category_url
                    self._items_cache[full_url] = item
                    urls.append(full_url)
                    if limit and len(urls) >= limit:
                        break
                paging = data.get("Paging") or {}
                total_pages = paging.get("Total")
                if total_pages and page >= total_pages:
                    break
                page += 1
        return urls

    def _json_post(self, path: str, payload: Dict[str, Any]) -> Any:
        resp = self.http.post(
            f"{self.base_url}{path}",
            json=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return resp.json()

    def _enrich_items_attributes(self, items: List[Dict[str, Any]]) -> None:
        """POST /ProductList/Attributes — batch; injectne do item['Attributes']."""
        ids = [it.get("Id") for it in items if it.get("Id") is not None]
        if not ids:
            return
        try:
            payload = self._json_post("/ProductList/Attributes", {"productIds": ids}) or {}
        except Exception as exc:
            LOGGER.debug("LKQ ProductList/Attributes skip: %s", exc)
            return
        by_id = {
            row.get("ProductId"): row.get("Attributes") or []
            for row in (payload.get("attributes") or [])
            if isinstance(row, dict)
        }
        for it in items:
            pid = it.get("Id")
            # JSON niekedy vráti int, Search2 string — skús oboje
            extra = by_id.get(pid)
            if extra is None:
                try:
                    extra = by_id.get(int(pid))
                except (TypeError, ValueError):
                    extra = None
            if extra:
                # Merge: pôvodné (ak neprázdne) + batch
                base = list(it.get("Attributes") or [])
                it["Attributes"] = base + list(extra)

    def _enrich_items_prices(self, items: List[Dict[str, Any]]) -> None:
        """Batch UserPrice (nákup) + RetailPrice (MOC) → _sg_* polia na itemoch."""
        ids: List[Any] = []
        seen_ids: set = set()
        for it in items:
            pid = it.get("Id")
            if pid is None:
                continue
            key = str(pid)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            # API preferuje int productIds
            try:
                ids.append(int(pid))
            except (TypeError, ValueError):
                ids.append(pid)
        if not ids:
            return

        user_by: Dict[str, Dict[str, Any]] = {}
        retail_by: Dict[str, Dict[str, Any]] = {}
        try:
            user_payload = self._json_post("/UserPrice/Data", {"productIds": ids}) or {}
            for row in user_payload.get("Content") or []:
                if isinstance(row, dict) and row.get("ProductId") is not None:
                    user_by[str(row["ProductId"])] = row
        except Exception as exc:
            LOGGER.debug("LKQ UserPrice/Data skip: %s", exc)
        try:
            retail_payload = self._json_post("/RetailPrice/Data", {"productIds": ids}) or {}
            for row in retail_payload.get("Content") or []:
                if isinstance(row, dict) and row.get("ProductId") is not None:
                    retail_by[str(row["ProductId"])] = row
        except Exception as exc:
            LOGGER.debug("LKQ RetailPrice/Data skip: %s", exc)

        for it in items:
            pid = it.get("Id")
            if pid is None:
                continue
            key = str(pid)
            urow = user_by.get(key)
            if urow:
                cost = parse_price(urow.get("PriceVat")) or parse_price(urow.get("Price"))
                if cost is not None:
                    it["_sg_user_price_vat"] = cost
                    it["_sg_user_price"] = parse_price(urow.get("Price"))
                    it["_sg_user_discount"] = str(urow.get("Discount") or "").strip()
            rrow = retail_by.get(key)
            if rrow:
                # Preferuj zobrazený string "16,77 €" (PriceVatValue=16.7649 → 16.76).
                retail = parse_price(rrow.get("PriceVat"))
                if retail is None and rrow.get("PriceVatValue") is not None:
                    try:
                        retail = float(rrow.get("PriceVatValue"))
                    except (TypeError, ValueError):
                        retail = None
                if retail is None:
                    retail = parse_price(rrow.get("Price"))
                if retail is not None:
                    it["_sg_retail_price_vat"] = round(float(retail), 2)
                    it["_sg_retail_price"] = parse_price(rrow.get("PriceValue") or rrow.get("Price"))

    def _ensure_item_prices(self, item: Dict[str, Any]) -> None:
        """Jednorazový enrich cien pre item (refresh / parse mimo discovery)."""
        if item.get("_sg_user_price_vat") is not None and item.get("_sg_retail_price_vat") is not None:
            return
        if item.get("Id") is None:
            return
        self._enrich_items_prices([item])

    def _fetch_oe_codes(self, product_id: Any) -> List[str]:
        if not self._fetch_codes or product_id is None:
            return []
        try:
            from fitment import extract_oe_from_lkq_codes

            resp = self.http.post(
                f"{self.base_url}/Product/Codes/{product_id}",
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            )
            return extract_oe_from_lkq_codes(resp.json())
        except Exception as exc:
            LOGGER.debug("LKQ Product/Codes/%s skip: %s", product_id, exc)
            return []

    def _apply_fitment(self, product: ProductRaw, item: Dict[str, Any], category_url: str = "") -> ProductRaw:
        """Vyplň ProductRaw make/model/engine/years/OE z attrs + OE codes + catalog slug."""
        from fitment import (
            extract_fitment_from_attributes,
            extract_oe_from_lkq_codes,
            merge_fitment_fields,
            parse_fitment_from_catalog_slug,
            parse_vehicle_suitability_text,
        )

        attrs = product.attributes or {}
        from_attrs = extract_fitment_from_attributes(attrs)
        cat = category_url or item.get("_sg_category_url") or self._last_category_url or ""
        from_slug = parse_fitment_from_catalog_slug(cat)
        # Description: len explicitný „vhodné pre:“ riadok (nie olejové WSS/Ford specs)
        suit_lines = []
        for line in (product.description or "").splitlines():
            if re.search(r"(?i)(?:vhodn[ée]\s+pre|použitie|kompatibilita|application|fits?)\s*[:\-]", line):
                suit_lines.append(line.strip())
        from_text = parse_vehicle_suitability_text("\n".join(suit_lines)) if suit_lines else {}

        oe = list(product.oe_numbers or [])
        if not oe:
            oe = extract_oe_from_lkq_codes((product.raw_extra or {}).get("lkq_codes"))
        if not oe and item.get("Id") is not None:
            oe = self._fetch_oe_codes(item.get("Id"))
            product.raw_extra = dict(product.raw_extra or {})
            product.raw_extra["lkq_codes"] = {"OemFetched": True, "count": len(oe)}

        merged = merge_fitment_fields(
            {
                "make": product.make,
                "model": product.model,
                "engine": product.engine,
                "year_from": product.year_from,
                "year_to": product.year_to,
                "oe_numbers": oe,
            },
            from_attrs,
            from_slug,
            from_text,
        )
        product.make = merged.get("make") or ""
        product.model = merged.get("model") or ""
        product.engine = merged.get("engine") or ""
        product.year_from = merged.get("year_from")
        product.year_to = merged.get("year_to")
        product.oe_numbers = list(merged.get("oe_numbers") or [])
        if cat:
            product.raw_extra = dict(product.raw_extra or {})
            product.raw_extra["catalog_url"] = cat
        return product

    # ---- Parse (z cache naplnenej pri discovery, fallback na generic) ----

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        item = self._items_cache.get(url)
        if item is None:
            self.ensure_login()
            # Prefer Id z URL → UserPrice/RetailPrice (SPA detail HTML nemá ceny).
            m = re.search(r"/Product/[^/]+/(\d+)", url)
            if m:
                fake_item: Dict[str, Any] = {
                    "Id": int(m.group(1)),
                    "Text": "",
                    "Code": "",
                    "Link": url.replace(self.base_url, "") if url.startswith(self.base_url) else url,
                }
                # Doplň meno/SKU z Search2 ak vieme kód z URL
                code_m = re.search(r"/Product/([^/]+)/\d+", url)
                if code_m:
                    code_guess = code_m.group(1).replace("-", " ")
                    fake_item["Code"] = code_guess
                    fake_item["Text"] = code_guess
                self._ensure_item_prices(fake_item)
                # Ak máme aspoň cenu, postav ProductRaw aj bez list cache
                if fake_item.get("_sg_user_price_vat") or fake_item.get("_sg_retail_price_vat"):
                    # Skús doplniť Text2 meno
                    try:
                        search_code = str(fake_item.get("Code") or "").strip()
                        if search_code:
                            payload = self._json_post(
                                "/Search2/Product/Data",
                                {"SearchText": search_code},
                            ) or {}
                            for row in payload.get("products") or []:
                                if str(row.get("Id")) == str(fake_item["Id"]):
                                    fake_item["Text"] = row.get("Name") or fake_item.get("Text")
                                    fake_item["Code"] = row.get("Code") or fake_item.get("Code")
                                    fake_item["Brand"] = row.get("Brand") or ""
                                    img = row.get("Image") or row.get("UrlImage")
                                    if img:
                                        fake_item["Image"] = {"Path": img, "PathBig": img}
                                    break
                    except Exception as exc:
                        LOGGER.debug("LKQ Search2 enrich skip: %s", exc)
                    self._items_cache[url] = fake_item
                    return self._product_from_item(fake_item, url)

            product = super().parse_product(url)
            if product:
                product.brand = product.brand or "LKQ"
                product.source = self.name
                fake_item2: Dict[str, Any] = {"Id": int(m.group(1))} if m else {}
                if fake_item2.get("Id") is not None:
                    self._ensure_item_prices(fake_item2)
                    cost = fake_item2.get("_sg_user_price_vat")
                    retail = fake_item2.get("_sg_retail_price_vat")
                    if cost is not None:
                        product.cost_price = str(cost)
                    if retail is not None:
                        product.regular_price = str(retail)
                        product.raw_extra = dict(product.raw_extra or {})
                        product.raw_extra["retail_hint"] = retail
                product = self._apply_fitment(product, fake_item2, self._last_category_url)
            return product
        return self._product_from_item(item, url)

    def _product_from_item(self, item: Dict[str, Any], url: str) -> Optional[ProductRaw]:
        self._ensure_item_prices(item)
        name = _strip_html(item.get("Text") or item.get("Name") or "")
        if not name:
            return None
        description = _strip_html(item.get("Description") or "")
        brand_raw = str(item.get("Brand") or "").strip()
        brand = detect_true_brand(name, description, fallback=brand_raw or "LKQ")

        # Nákup = UserPrice (Vaša cena s DPH). List PriceVat môže byť Maloobchod
        # podľa UI prepínača — nepoužívať ako cost, keď máme UserPrice.
        cost = item.get("_sg_user_price_vat")
        if cost is None:
            cost = parse_price(item.get("PriceVat")) or parse_price(item.get("Price"))
        retail = item.get("_sg_retail_price_vat")
        if retail is None:
            retail = parse_price(item.get("RetailPriceVat")) or parse_price(item.get("RetailPrice"))

        image = ((item.get("Image") or {}).get("PathBig")
                 or (item.get("Image") or {}).get("Path") or "")

        disponibility = item.get("Disponibility") or {}
        stock_amount = item.get("StorageStockAmount")
        is_orderable = bool(item.get("IsOrderable", True))
        # StorageStockAmount = kusy na sklade LKQ. Orderable bez zásoby ≠ „na sklade“
        # (dôležité pre --in-stock filter — predtým sa orderable tvárilo ako instock).
        try:
            qty_f = float(stock_amount) if stock_amount is not None else None
        except (TypeError, ValueError):
            qty_f = None
        if qty_f is not None:
            stock_status = "instock" if is_orderable and qty_f > 0 else "outofstock"
        else:
            stock_status = "instock" if is_orderable else "outofstock"

        attrs = _lkq_attrs_to_map(item.get("Attributes") or [])
        # ReferenceCode niekedy drží OE / cross — len voľný text, nie vehicle ID
        ref = str(item.get("ReferenceCode") or "").strip()
        if ref and "reference" not in {k.lower() for k in attrs}:
            attrs["OE"] = ref

        product = ProductRaw(
            name=name,
            source=self.name,
            supplier_url=url,
            sku=str(item.get("Code") or "").strip(),
            ean="",
            brand=brand,
            description=description,
            regular_price=str(retail) if retail else "",
            cost_price=str(cost) if cost else "",
            stock_status=stock_status,
            stock_qty=float(stock_amount) if stock_amount is not None else None,
            delivery_text=str(disponibility.get("Text") or ""),
            existing_images=[image] if image else [],
            attributes=attrs,
            raw_extra={
                "lkq_item": {
                    k: v for k, v in item.items()
                    if k not in ("Description", "_sg_category_url")
                },
                "catalog_url": item.get("_sg_category_url") or self._last_category_url,
                "retail_hint": retail,
                "user_price_vat": cost,
                "user_discount": item.get("_sg_user_discount") or "",
            },
        )
        return self._apply_fitment(
            product,
            item,
            str(item.get("_sg_category_url") or self._last_category_url or ""),
        )

    def fetch_offer(self, url: str):
        product = self.parse_product(url)
        if not product:
            return None
        from .base import OfferSnapshot

        cost = parse_price(product.cost_price)
        retail = None
        extra = product.raw_extra or {}
        if extra.get("retail_hint") not in (None, ""):
            retail = parse_price(extra.get("retail_hint"))
        if retail is None:
            retail = parse_price(product.regular_price)
        return OfferSnapshot(
            supplier_url=url,
            source=self.name,
            cost_price=cost,
            retail_hint=retail,
            stock_status=product.stock_status,
            stock_qty=product.stock_qty,
            delivery_text=product.delivery_text,
            sku=product.sku,
            name=product.name,
        )

    def parse_api_item(self, item: dict) -> ProductRaw:
        price = parse_price(item.get("price") or item.get("Price") or item.get("retailPrice"))
        return ProductRaw(
            name=str(item.get("name") or item.get("Name") or item.get("title") or ""),
            source=self.name,
            supplier_url=str(item.get("url") or item.get("detailUrl") or ""),
            sku=str(item.get("sku") or item.get("code") or item.get("oe") or ""),
            ean=str(item.get("ean") or item.get("gtin") or ""),
            brand=str(item.get("brand") or item.get("manufacturer") or "LKQ"),
            description=str(item.get("description") or ""),
            regular_price=str(price) if price is not None else "",
            stock_status="instock" if item.get("inStock", True) else "outofstock",
            stock_qty=float(item["qty"]) if item.get("qty") is not None else None,
            existing_images=[item["image"]] if item.get("image") else [],
            attributes=item.get("attributes") or {},
            raw_extra=item,
        )
