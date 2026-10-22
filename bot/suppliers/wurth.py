#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Würth e-shop (Intershop) — https://eshop.wurth.sk/sk/SK/EUR/

Licencia / Jidoka: ``bot/data/CATALOG_LICENSING.md`` § Würth.

DEFAULT = VEREJNÝ režim (bez loginu):
  - crawl anonymného HTML katalógu (.sku linky) — len discovery SKU
  - B2B cena/sklad: zapni `WURTH_B2B=1` + `WURTH_USER` / `WURTH_PASSWORD`
  - Akamai: pri Challenge Validation bot skončí (žiadne obchádzanie ochrany)
  - Fotky z witglobal CDN: default OFF; zapni len `WURTH_MEDIA_OK=1`
    (písomný/B2B súhlas na rehost Würth médií)

Smieme: Art.-Nr./SKU, EAN, názov, B2B cena (po logine), sklad, DIN/ISO text.
Nesmieme: dump celého katalógu; rehost fotiek bez súhlasu; stierať značku Würth;
  obchádzať B2B login kvôli cenníku.

Pilot (Heijunka ≤20–50):
  python import_pipeline.py --supplier wurth --limit 20 --cheap --in-stock --dry-run
  python import_pipeline.py --supplier wurth --limit 30 --cheap --in-stock \\
      --category-url 'https://eshop.wurth.sk/Produktove-kategorie/Cistic-strojov-a-dielov/3143300707.cyid/3143.cgid/sk/SK/EUR/'
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import (
    HttpClient,
    LoginMixin,
    ProductRaw,
    absolutize,
    extract_b2b_signals,
    parse_price,
    same_host,
    session_cookie_names,
)
from .generic import GenericSupplier
from .wurth_clean import (
    build_description_v3,
    known_pack_qty,
    sale_title,
    translations_sk,
    build_clean_html,
    clean_description_lines,
    pack_from_supplier_block,
    pack_title,
    parse_oe_numbers,
    parse_origin,
    parse_pack_qty,
    parse_price_basis,
)

try:
    from media_licensing import wurth_media_allowed
except ImportError:  # pragma: no cover — package import bez bot/ na path
    import sys
    from pathlib import Path

    _bot = Path(__file__).resolve().parent.parent
    if str(_bot) not in sys.path:
        sys.path.insert(0, str(_bot))
    from media_licensing import wurth_media_allowed

LOGGER = logging.getLogger("suppliers.wurth")

HOST = "eshop.wurth.sk"
BASE = "https://eshop.wurth.sk/sk/SK/EUR"
DEFAULT_LOGIN = "https://eshop.wurth.sk/sk/SK/EUR/login"
SMALL_LAYER = (
    "https://eshop.wurth.sk/is-bin/INTERSHOP.enfinity/WFS/3143-B1-Site/"
    "sk_SK/-/EUR/ViewModelDetail-ShowProductDetailInSmallLayer"
)

# Auto / dielňa — nie celý sortiment (stavebnina, poľnohospodárstvo…).
DEFAULT_CATEGORY_SEEDS = (
    "https://eshop.wurth.sk/Produktove-kategorie/Chemicke-a-technicke-vyrobky/314330.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Male-nahradne-diely-pre-vozidla/314365.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Rucne-naradie/314360.cyid/3143.cgid/sk/SK/EUR/",
    "https://eshop.wurth.sk/Produktove-kategorie/Bezpecnost-prace/314305.cyid/3143.cgid/sk/SK/EUR/",
)

SKU_URL_RE = re.compile(
    r"https?://eshop\.wurth\.sk/[^\"'\s]+?\.sku/sk/SK/EUR/",
    re.I,
)
REL_SKU_URL_RE = re.compile(r"/[^\"'\s]+?\.sku/sk/SK/EUR/", re.I)
SKU_IN_URL_RE = re.compile(r"/(\d+)\.sku/", re.I)
CYID_RE = re.compile(r"/(\d+)\.cyid/", re.I)
SKUS_FILTER_RE = re.compile(
    r"skusOfProductsToDisplayWithoutFilters\\*\"?\s*:\s*\\*\[(.*?)\]",
    re.S,
)


def _cyid(url: str) -> str:
    m = CYID_RE.search(url or "")
    return m.group(1) if m else ""


def _norm_sku_url(url: str) -> str:
    return (url or "").split("?")[0].split("#")[0].rstrip("/") + "/"


def extract_sku_urls(html: str, base_url: str = f"https://{HOST}") -> Set[str]:
    """Vyber SEO product URL (`…/{sku}.sku/sk/SK/EUR/`) z HTML."""
    found: Set[str] = set()
    for m in SKU_URL_RE.findall(html or ""):
        found.add(_norm_sku_url(m))
    for m in REL_SKU_URL_RE.findall(html or ""):
        found.add(_norm_sku_url(urljoin(base_url, m)))
    return found


def extract_sku_codes(html: str) -> Set[str]:
    """SKU z JS configu / data-productNos (aj keď chýba SEO link)."""
    skus: Set[str] = set()
    for block in SKUS_FILTER_RE.findall(html or ""):
        skus.update(re.findall(r"(\d{6,14})", block.replace("\\", "")))
    for block in re.findall(r"data-productNos=['\"]([^'\"]+)['\"]", html or ""):
        skus.update(re.findall(r"(\d{6,14})", block))
    for m in SKU_IN_URL_RE.findall(html or ""):
        skus.add(m)
    return skus


def extract_model_category_ids(html: str) -> Set[str]:
    return set(re.findall(r'data-categoryname="(\d+)"', html or ""))


def model_page_url(category_id: str) -> str:
    # Intershop akceptuje aj dummy slug — dôležité je {id}.cyid
    return (
        f"https://{HOST}/Produktove-kategorie/x/"
        f"{category_id}.cyid/3143.cgid/sk/SK/EUR/"
    )


def child_category_urls(page_url: str, html: str) -> Set[str]:
    """Hlbšie .cyid pod aktuálnou vetvou (prefix cyid)."""
    parent = _cyid(page_url)
    out: Set[str] = set()
    if not parent:
        return out
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        href = absolutize(page_url, a["href"]).split("?")[0].split("#")[0]
        if not same_host(href, HOST):
            continue
        child = _cyid(href)
        if child.startswith(parent) and len(child) > len(parent):
            out.add(href if href.endswith("/") else href + "/")
    return out


def _b2b_enabled() -> bool:
    return os.getenv("WURTH_B2B", "").strip().lower() in ("1", "true", "yes", "on")


# Akamai Bot Manager: pri challenge stránke bot končí — žiadne obchádzanie.


def is_akamai_challenge(html: str) -> bool:
    low = (html or "").lower()
    return (
        "challenge validation" in low
        or 'id="sec-cpt-if"' in low
        or "sec-if-cpt-container" in low
        or "cp_clge_done" in low
    )


def has_wurth_login_form(html: str) -> bool:
    return "LoginWithUserNameForm.UserName" in (html or "") or "LoginForm.Login" in (html or "")


def _wurth_http(delay: Optional[float] = None) -> HttpClient:
    """Bežný HTTP klient (bez falšovania TLS odtlačku), slušné tempo ~2 s/req."""
    d = float(os.getenv("WURTH_CRAWL_DELAY", "2.0")) if delay is None else max(delay, 2.0)
    return HttpClient(delay=d)


class WurthSupplier(LoginMixin, GenericSupplier):
    name = "wurth"
    base_url = BASE

    def __init__(self, http=None, credentials=None):
        creds = dict(credentials or {})
        # HQ often stores website root (eshop.wurth.sk/) — Intershop login is /sk/SK/EUR/login.
        raw_login = (creds.get("login_url") or "").strip()
        if (not raw_login) or raw_login.rstrip("/").endswith("eshop.wurth.sk") or "/login" not in raw_login:
            creds["login_url"] = DEFAULT_LOGIN
        # Vlastný klient s pokojným tempom (bez obchádzania ochrany).
        http = _wurth_http(delay=getattr(http, "delay", None) if http is not None else None)
        super().__init__(base_url=self.base_url, http=http, credentials=creds, source_name="wurth")
        self._logged_in = False
        self._blocked = False
        self._sku_url_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ login

    def _get_login_page(self, login_url: str):
        """Jeden GET login stránky. Pri challenge/403 žiadne obchádzanie — len pauza a 1 opakovanie."""
        page = None
        # WURTH_CHALLENGE_PAUSE=0 → pri ochrane webu žiadne čakanie ani opakovanie (rýchly skip).
        retry = int(os.getenv("WURTH_CHALLENGE_PAUSE", "300") or 0)
        self._blocked = False
        for pause in ((0, retry) if retry > 0 else (0,)):
            if pause:
                LOGGER.warning("Würth: ochrana webu (challenge/403) — pauza %ss", pause)
                time.sleep(pause)
            try:
                page = self.http.get(login_url)
            except Exception:
                LOGGER.debug("Würth: login GET zlyhal", exc_info=True)
                continue
            if has_wurth_login_form(page.text) and not is_akamai_challenge(page.text):
                return page
        if page is not None and (page.status_code in (403, 429) or is_akamai_challenge(page.text)):
            self._blocked = True
        return page

    def probe_blocked(self) -> bool:
        """Jeden bežný GET verejnej stránky (bez obchádzania). True = web vracia challenge/403."""
        try:
            r = self.http.get(f"{BASE}/")
        except Exception:
            return False
        blocked = r.status_code in (403, 429) or is_akamai_challenge(r.text or "")
        self._blocked = blocked
        return blocked

    def ensure_login(self) -> bool:
        if getattr(self, "_logged_in", False):
            return True
        force = bool(getattr(self, "_login_fallback", False))
        if not _b2b_enabled() and not force:
            LOGGER.info("Würth: public režim (WURTH_B2B nie je 1) — bez loginu")
            return False
        user = (self.credentials or {}).get("user")
        password = (self.credentials or {}).get("password")
        login_url = (self.credentials or {}).get("login_url") or DEFAULT_LOGIN
        if not user or not password:
            LOGGER.warning(
                "Würth: login požadovaný (%s) ale chýba USER/PASSWORD — ostávam public",
                "fallback" if force else "WURTH_B2B",
            )
            return False

        try:
            page = self._get_login_page(login_url)
            if page is None:
                LOGGER.error("Würth: login page nedostupná (Akamai/challenge) na %s", login_url)
                return False
            soup = BeautifulSoup(page.text, "html.parser")
            form = None
            # 1) username/password (bežný účet)
            for candidate in soup.select("form.loginForm"):
                if candidate.find("input", {"name": "LoginWithUserNameForm.UserName"}):
                    form = candidate
                    break
            # 2) customer number + login (firemný)
            if form is None:
                for candidate in soup.select("form.loginForm"):
                    if candidate.find("input", {"name": "LoginForm.Login"}):
                        form = candidate
                        break
            if form is None:
                if is_akamai_challenge(page.text):
                    LOGGER.error(
                        "Würth: web vrátil ochrannú stránku namiesto login formu na %s — "
                        "skončím, skúsi sa ďalší beh",
                        login_url,
                    )
                else:
                    LOGGER.error("Würth: login formulár nenájdený na %s", login_url)
                return False

            payload: Dict[str, str] = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    payload[name] = inp.get("value") or ""

            if form.find("input", {"name": "LoginWithUserNameForm.UserName"}):
                payload["LoginWithUserNameForm.UserName"] = user
                payload["LoginWithUserNameForm.Password"] = password
                # submit button (mobile/desktop)
                if not any("update" in k.lower() for k in payload):
                    payload["LoginWithUserNameForm.mobile_update"] = ""
            else:
                # CustomerNumber form — user môže byť "customer|login" alebo len login
                if "|" in user:
                    cust, login_name = user.split("|", 1)
                    payload["LoginForm.CustomerNumber"] = cust.strip()
                    payload["LoginForm.Login"] = login_name.strip()
                else:
                    payload["LoginForm.Login"] = user
                    if not payload.get("LoginForm.CustomerNumber"):
                        # niektoré účty majú customer = username
                        payload["LoginForm.CustomerNumber"] = user
                payload["LoginForm.Password"] = password
                if "LoginForm.update" not in payload:
                    payload["LoginForm.update"] = ""

            payload.pop("ForgottPassword", None)
            post_url = urljoin(login_url, form.get("action") or login_url)
            resp = self.http.post(
                post_url,
                data=payload,
                headers={
                    "Referer": login_url,
                    "Origin": f"https://{HOST}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            body = (resp.text or "").lower()
            final = (getattr(resp, "url", "") or "").lower()
            ok = (
                resp.status_code < 400
                and "logout" in body
                and "loginwithusernameform.password" not in body
                and "/login" not in final
            )
            # cookie WU_AUTHENTICATED je spoľahlivý signál (requests jar)
            cookies = session_cookie_names(self.http.session)
            if "WU_AUTHENTICATED" in cookies or any(
                "AuthenticationStateToken" in n for n in cookies
            ):
                ok = True
            self._logged_in = bool(ok)
            if not ok:
                LOGGER.error("Würth: login zlyhal (final=%s)", getattr(resp, "url", ""))
            else:
                LOGGER.info("Würth: login OK")
            return self._logged_in
        except Exception:
            LOGGER.exception("Würth: login exception")
            self._logged_in = False
            return False

    # -------------------------------------------------------------- discovery

    def _seeds(self) -> List[str]:
        raw = (self.credentials or {}).get("category_url") or ""
        if raw.strip():
            return [u.strip() for u in raw.split(",") if u.strip()]
        return list(DEFAULT_CATEGORY_SEEDS)

    def _resolve_sku_url(self, sku: str) -> Optional[str]:
        if not sku:
            return None
        if sku in self._sku_url_cache:
            return self._sku_url_cache[sku]
        try:
            resp = self.http.get(
                SMALL_LAYER,
                params={"SKU": sku},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            text = resp.text or ""
            start = text.find("{")
            if start < 0:
                return None
            data = json.loads(text[start:])
            url = data.get("productDetailUrl")
            if url and ".sku/" in url:
                url = _norm_sku_url(url)
                self._sku_url_cache[sku] = url
                return url
        except Exception:
            LOGGER.debug("Würth: small-layer resolve zlyhal pre %s", sku, exc_info=True)
        return None

    def _crawl_urls(self, limit: Optional[int] = None) -> List[str]:
        found: Set[str] = set()
        seen_pages: Set[str] = set()
        queue: List[str] = list(self._seeds())
        # Homepage len pri plnom katalógu. Pri --category-url by kradol featured
        # (batérie, malta…) namiesto prázdnej leaf kategórie.
        seeded = bool((self.credentials or {}).get("category_url"))
        if not seeded:
            queue.append(f"{BASE}/")

        max_pages = int(os.environ.get("SG_WURTH_MAX_PAGES") or 220) if not limit else max(30, int(limit) * 3)
        pages = 0
        model_queue: List[str] = []
        seed_prefixes = {_cyid(u) for u in queue if _cyid(u)}

        while (queue or model_queue) and pages < max_pages and (limit is None or len(found) < limit):
            page_url = model_queue.pop(0) if model_queue else queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            pages += 1
            try:
                resp = self.http.get(page_url)
            except Exception:
                continue
            html = resp.text or ""
            if resp.status_code in (403, 429) or is_akamai_challenge(html):
                blocks = getattr(self, "_blocks", 0) + 1
                self._blocks = blocks
                if os.getenv("SG_WURTH_FAST_FAIL") == "1" and not getattr(self, "_ok_pages", 0):
                    self._blocked = True
                    LOGGER.warning("Würth: ochrana webu hneď na začiatku — crawl končím bez čakania (SG_WURTH_FAST_FAIL)")
                    break
                if blocks > 3:
                    LOGGER.warning("Würth: opakovaná ochrana webu — crawl končím (pokračuje ďalší beh)")
                    break
                LOGGER.warning("Würth: %s / challenge — pauza %ss", resp.status_code, 120 * blocks)
                time.sleep(120 * blocks)
                seen_pages.discard(page_url)
                queue.insert(0, page_url)
                continue

            self._ok_pages = getattr(self, "_ok_pages", 0) + 1
            for url in extract_sku_urls(html, page_url):
                found.add(url)
                if limit and len(found) >= limit:
                    break
            if limit and len(found) >= limit:
                break

            for mid in extract_model_category_ids(html):
                # Pri seeded category drž sa vetvy (prefix cyid), inak model tiles
                # z navigácie vytiahnu cudzí sortiment.
                if seed_prefixes and not any(mid.startswith(p) for p in seed_prefixes):
                    continue
                murl = model_page_url(mid)
                if murl not in seen_pages:
                    model_queue.append(murl)

            for child in child_category_urls(page_url, html):
                cid = _cyid(child)
                if seed_prefixes and cid and not any(cid.startswith(p) for p in seed_prefixes):
                    continue
                if child not in seen_pages:
                    queue.append(child)

            if limit is None or len(found) < limit:
                have = {m.group(1) for u in found if (m := SKU_IN_URL_RE.search(u))}
                for sku in sorted(extract_sku_codes(html) - have):
                    if limit and len(found) >= limit:
                        break
                    resolved = self._resolve_sku_url(sku)
                    if resolved:
                        found.add(resolved)

        LOGGER.info(
            "Würth discover: %d URL (pages=%d, queue_left=%d, logged_in=%s)",
            len(found),
            pages,
            len(queue),
            bool(getattr(self, "_logged_in", False)),
        )
        urls = sorted(found)
        return urls[:limit] if limit else urls

    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        self.ensure_login()
        urls = self._crawl_urls(limit)
        if urls:
            return urls
        # Guest katalóg často 0 .sku — ak máme creds, automatický B2B fallback.
        user = (self.credentials or {}).get("user")
        password = (self.credentials or {}).get("password")
        if user and password and not getattr(self, "_logged_in", False):
            LOGGER.info("Würth: public=0 URL → B2B login fallback (creds nastavené)")
            self._login_fallback = True
            if self.ensure_login():
                urls = self._crawl_urls(limit)
        return urls

    # ------------------------------------------------------------------ parse

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        self.ensure_login()
        try:
            resp = self.http.get(url)
        except Exception:
            # truncated/featured SKU slug niekedy 404 → SmallLayer podľa čísla v URL
            m = SKU_IN_URL_RE.search(url or "")
            if m:
                fixed = self._resolve_sku_url(m.group(1))
                if fixed and fixed.rstrip("/") != (url or "").rstrip("/"):
                    return self.parse_product(fixed)
            LOGGER.debug("Würth: PDP fetch fail %s", url, exc_info=True)
            return None
        if "Fallback=true" in (resp.url or ""):
            m = SKU_IN_URL_RE.search(url or "")
            if m:
                fixed = self._resolve_sku_url(m.group(1))
                if fixed and fixed.rstrip("/") != (url or "").rstrip("/"):
                    return self.parse_product(fixed)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        final_url = _norm_sku_url(resp.url or url)

        name = ""
        jsonld_product: Dict[str, object] = {}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or "")
            except Exception:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    jsonld_product = item
                    break
            if jsonld_product:
                break

        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(" ", strip=True)
        if not name and jsonld_product.get("name"):
            name = str(jsonld_product.get("name") or "").strip()
        if not name:
            return None

        # Typ žiarovky (H4/H7…) často len v SEO slug-u — doplň do názvu
        # (ako viskozita pri olejoch), aby taxonómia + AI hub vedeli leaf.
        bulb_tok = None
        try:
            from taxonomy import extract_bulb_type_token  # noqa: WPS433

            bulb_tok = extract_bulb_type_token(final_url) or extract_bulb_type_token(name)
            if bulb_tok and not extract_bulb_type_token(name):
                name = f"{name} {bulb_tok}".strip()
        except Exception:
            bulb_tok = None

        sku = ""
        m = SKU_IN_URL_RE.search(final_url)
        if m:
            sku = m.group(1)
        if not sku:
            num_el = soup.select_one(".productdetail_info_right_productnumber, .product-number-row")
            if num_el:
                mm = re.search(r"(\d{6,14})", num_el.get_text(" ", strip=True))
                if mm:
                    sku = mm.group(1)

        ean = ""
        ean_el = soup.select_one(".ean-code-number")
        if ean_el:
            mm = re.search(r"(\d{8,14})", ean_el.get_text(" ", strip=True))
            if mm:
                ean = mm.group(1)
        if not ean:
            mm = re.search(r"EAN\s*[#: ]?\s*(\d{8,14})", soup.get_text(" ", strip=True), re.I)
            if mm:
                ean = mm.group(1)

        price = None
        price_el = soup.select_one(".priceValue")
        if price_el:
            price = parse_price(price_el.get_text())
        order_blob = soup.select_one(".productdetail_info_right_order_price_information")
        order_text = order_blob.get_text(" ", strip=True) if order_blob else ""
        if price is None and order_blob:
            price = parse_price(order_text)
        # Balenie: Würth cena je za BALENIE („Počet x Balenie 1 x 10 ks“), príp. „za 100 ks“.
        order_area = " ".join(
            el.get_text(" ", strip=True)
            for el in soup.select(
                ".productdetail_info_right_order, .productdetail_info_right_order_price_information, "
                ".productdetail_info_right, .packaging, .packing-unit, .product-packaging"
            )
        )
        page_text = soup.get_text("\n", strip=True)
        pack_qty = parse_pack_qty(order_area) or pack_from_supplier_block(resp.text or "", sku) or known_pack_qty(sku)
        price_basis = parse_price_basis(order_area or order_text)
        pack_price = None
        if price is not None:
            if price_basis and pack_qty:
                pack_price = round(float(price) / price_basis * pack_qty, 4)
            elif price_basis and not pack_qty:
                pack_price = None  # cena za N ks bez balenia — nevieme, čo zákazník dostane
            else:
                pack_price = float(price)
        origin = parse_origin(page_text)
        oe_numbers = parse_oe_numbers(page_text)

        stock_status = "instock"
        stock_el = soup.select_one(".productdetail_info_right_stock")
        stock_text = stock_el.get_text(" ", strip=True) if stock_el else ""
        icon = soup.select_one(".availability-icon")
        icon_cls = " ".join(icon.get("class") or []) if icon else ""
        lower = f"{stock_text} {icon_cls}".lower()
        if any(k in lower for k in ("nedostup", "vypred", "unavailable", "not-available", "notavailable")):
            stock_status = "outofstock"
        elif "available" in icon_cls or "dispoz" in lower:
            stock_status = "instock"

        img = ""
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img = og["content"]
        if not img and jsonld_product.get("image"):
            jimg = jsonld_product.get("image")
            if isinstance(jimg, list) and jimg:
                img = str(jimg[0])
            elif jimg:
                img = str(jimg)
        if not img:
            img_el = soup.select_one(".modeldetailproduct_info_left_img img, img[itemprop='image']")
            if img_el:
                img = img_el.get("data-src") or img_el.get("src") or ""
        # Prefer vyšší rozlíšenie z witglobal CDN, ak je v HTML
        if img and "media.witglobal.net" in img and "/200px/" in img:
            hi = img.replace("/200px/", "/576px/")
            if hi in (resp.text or ""):
                img = hi
        if not img:
            m_img = re.search(
                r"https://media\.witglobal\.net/source/eshop/stmedia/[^\"']+/576px/[^\"']+\.(?:jpg|png|webp)",
                resp.text or "",
                re.I,
            )
            if not m_img:
                m_img = re.search(
                    r"https://media\.witglobal\.net/source/eshop/stmedia/[^\"']+\.(?:jpg|png|webp)",
                    resp.text or "",
                    re.I,
                )
            if m_img:
                img = m_img.group(0)

        desc = ""
        desc_el = soup.select_one(".block_productinformation")
        if desc_el:
            for li in desc_el.find_all("li"):
                li.insert_before("\n")
                li.insert_after("\n")
            desc = desc_el.get_text("\n", strip=True)
        if not desc:
            ogd = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
            if ogd and ogd.get("content"):
                desc = ogd["content"]
        pack_verified = bool(pack_qty) and pack_price is not None
        # v3: čistý SK popis (čo to je, použitie, parametre, balenie a predaj); predaj po baleniach.
        v3 = build_description_v3(
            name, desc, sku=sku, ean=ean, supplier_url=final_url,
            pack_qty=pack_qty if pack_verified else None, oe_numbers=oe_numbers,
            translations=translations_sk(),
        )
        name = sale_title(v3["title"], pack_qty if pack_verified else None)
        clean = {"description": v3["description"], "short_description": v3["short_description"]}

        b2b = extract_b2b_signals(soup)
        # Po B2B logine je zobrazená cena nákupná (cost) = CENA ZA BALENIE.
        # Bez WURTH_B2B loginu cenu ako cost NEBERIEME (guest cenník ≠ náš IČO).
        # cost = cena balenia; WC produkt = celé balenie (nie kus).
        cost = None
        retail_hint = b2b.get("retail_hint")
        if self._logged_in:
            cost = pack_price if pack_price is not None else None
            if cost is None and price is not None and not price_basis:
                cost = price
        elif price is not None and not self._logged_in:
            # Verejná/guest cena môže ísť len ako retail hint, nie ako cost.
            retail_hint = retail_hint or price

        media_ok = wurth_media_allowed()
        images: List[str] = []
        if img and media_ok:
            images = [img]
        elif img and not media_ok:
            LOGGER.info(
                "Würth: fotka potlačená (WURTH_MEDIA_OK nie je 1) — Art.-Nr. %s",
                sku or "?",
            )

        attrs: Dict[str, str] = {}
        if sku:
            attrs["Art.-Nr."] = sku
        if bulb_tok:
            attrs["Typ žiarovky"] = bulb_tok
        if pack_verified:
            attrs["Počet v balení"] = f"{pack_qty} ks"

        return ProductRaw(
            name=name,
            source=self.name,
            supplier_url=final_url,
            sku=sku,
            ean=ean,
            brand="Würth",
            description=clean["description"],
            short_description=clean["short_description"],
            oe_numbers=oe_numbers,
            regular_price=str(retail_hint) if retail_hint else "",
            sale_price="",
            cost_price=str(cost) if cost is not None else "",
            stock_status=b2b.get("stock_status") or stock_status,
            stock_qty=b2b.get("stock_qty") if self._logged_in else None,
            delivery_days=(
                b2b.get("delivery_days") or (1 if stock_status == "instock" else None)
            )
            if self._logged_in
            else None,
            delivery_text=(
                b2b.get("delivery_text")
                or ("skladom — expedícia do 24 hodín" if stock_status == "instock" else "")
            )
            if self._logged_in
            else "",
            attributes=attrs,
            existing_images=images,
            raw_extra={
                "parser": "wurth-intershop",
                "b2b": b2b,
                "retail_hint": retail_hint,
                "bulb_type": bulb_tok,
                "_sg_supplier": "wurth_sk",
                "_sg_supplier_art_no": sku,
                "pack_qty": pack_qty,
                "pack_price": pack_price,
                "unit_cost": round(pack_price / pack_qty, 4) if pack_verified else None,
                "price_basis": price_basis,
                "pack_verified": pack_verified,
                "origin": origin.get("origin"),
                "origin_text": origin.get("origin_text"),
                "oe_numbers": oe_numbers,
                "clean_html": True,
                "media_source": "wurth_b2b_portal",
                "media_rights_note": (
                    "B2B customer media — require active account + WURTH_MEDIA_OK"
                ),
                "media_allowed": media_ok,
                "media_original_url": img or "",
            },
        )
