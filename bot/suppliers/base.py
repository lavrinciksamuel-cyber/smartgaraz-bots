#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Základný kontrakt pre všetkých dodávateľov (Pro-Tec, Würth, LKQ, Szekop, Generic…).

Každý adapter vracia rovnaký `ProductRaw` dict, ktorý ide priamo do
`ai_engine.process_product_with_ai()` — bez ohľadu na to, či dáta prišli
z verejného HTML crawlera, B2B API alebo tajného partner endpointu.
"""

from __future__ import annotations

import abc
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# bot/ na sys.path (HttpClient beží aj z package importu suppliers.*)
_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))
from catalog_licensing import assert_allowed_catalog_url  # noqa: E402

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
}


@dataclass
class ProductRaw:
    """Jednotný formát produktu zo všetkých zdrojov."""

    name: str
    source: str
    supplier_url: str = ""
    sku: str = ""
    ean: str = ""
    brand: str = ""
    description: str = ""
    short_description: str = ""
    regular_price: str = ""          # predajná (MOC) — ak ju poznáme
    sale_price: str = ""
    cost_price: str = ""             # dodávateľská / B2B nákupná cena
    stock_status: str = "instock"    # instock | outofstock | onbackorder
    stock_qty: Optional[float] = None
    delivery_days: Optional[int] = None
    delivery_text: str = ""          # napr. "do 24 hodín", "2–3 pracovné dni"
    attributes: Dict[str, Any] = field(default_factory=dict)
    existing_images: List[str] = field(default_factory=list)
    status: str = "draft"
    currency: str = "EUR"
    # Fitment (voliteľné — z B2B feedu / EF Autoparts / LKQ…)
    make: str = ""
    model: str = ""
    engine: str = ""
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    oe_numbers: List[str] = field(default_factory=list)
    raw_extra: Dict[str, Any] = field(default_factory=dict)

    def to_ai_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("raw_extra", None) or {}
        # Maloobchodný hint pre cenotvorbu (PRO-TEC / Szekop) — nechaj v AI dict.
        hint = extra.get("retail_hint") if isinstance(extra, dict) else None
        if hint and not data.get("retail_hint"):
            data["retail_hint"] = hint
        list_p = extra.get("list_price") if isinstance(extra, dict) else None
        if list_p and not data.get("list_price"):
            data["list_price"] = list_p
        # Proveniencia dodávateľa (Würth Art.-Nr., media rights) — pre WC meta.
        if isinstance(extra, dict):
            data["raw_extra"] = {
                k: extra[k]
                for k in (
                    "_sg_supplier",
                    "_sg_supplier_art_no",
                    "media_source",
                    "media_rights_note",
                    "media_allowed",
                    "media_original_url",
                    "catalog_url",
                    "lkq_codes",
                    "list_price",
                    "retail_hint",
                    "pack_qty",
                    "pack_price",
                    "unit_cost",
                    "price_basis",
                    "pack_verified",
                    "origin",
                    "origin_text",
                    "clean_html",
                    "stock_ba",
                    "stock_dist",
                    "stock_text",
                    "sg_category_path",
                )
                if k in extra and extra[k] not in (None, "")
            }
            # LKQ dostupnosť (availability.py) — len potrebné polia, nie celý item
            _li = extra.get("lkq_item")
            if isinstance(_li, dict) and _li:
                _d = _li.get("Disponibility") or {}
                data["raw_extra"]["lkq_item"] = {
                    "StorageStockAmount": _li.get("StorageStockAmount"),
                    "IsStorageStock": _li.get("IsStorageStock"),
                    "IsOrderable": _li.get("IsOrderable", True),
                    "IsUserOrderForbidden": _li.get("IsUserOrderForbidden", False),
                    "Disponibility": {"Text": _d.get("Text") if isinstance(_d, dict) else str(_d or "")},
                }
            if not data["raw_extra"]:
                data.pop("raw_extra", None)
        # AI copywriting nepotrebuje sklad/dodanie, ale cenotvorba áno cez meta sync
        # Fitment polia ostávajú — parse_fitment_from_supplier ich číta.
        return data


@dataclass
class OfferSnapshot:
    """Snapshot B2B ponuky: nákupná cena + sklad + rýchlosť dodania."""

    supplier_url: str
    source: str
    cost_price: Optional[float] = None       # dodávateľská (B2B) cena
    retail_hint: Optional[float] = None      # odporúčaná MOC od dodávateľa (ak je)
    sale_price: Optional[float] = None
    stock_status: Optional[str] = None
    stock_qty: Optional[float] = None
    delivery_days: Optional[int] = None
    delivery_text: str = ""
    currency: str = "EUR"
    sku: str = ""
    name: str = ""
    # spätná kompatibilita so starým sync_prices
    regular_price: Optional[float] = None

    def __post_init__(self):
        if self.regular_price is None and self.retail_hint is not None:
            self.regular_price = self.retail_hint
        if self.cost_price is None and self.regular_price is not None:
            # Ak B2B crawler nedostal oddelenú nákupnú, regular môže byť B2B po logine
            pass


def session_cookie_names(session) -> set:
    """Cookie names from requests or curl_cffi jar (curl_cffi yields name strings)."""
    names: set = set()
    jar = getattr(session, "cookies", None)
    if jar is None:
        return names
    if hasattr(jar, "keys"):
        try:
            names.update(jar.keys())
        except Exception:
            pass
    for c in jar:
        if hasattr(c, "name"):
            names.add(c.name)
        elif isinstance(c, str):
            names.add(c)
    return names


class HttpClient:
    """HTTP session. ``impersonate`` (e.g. safari17_0) uses curl_cffi TLS fingerprint —
    needed for Akamai Bot Manager on eshop.wurth.sk (plain requests → Challenge Validation).
    """

    def __init__(
        self,
        timeout: int = 45,
        delay: float = 0.4,
        verify: bool = False,
        impersonate: Optional[str] = None,
    ):
        # 45s — LKQ Catalog pri záťaži často prekročí 20s read timeout.
        self.timeout = timeout
        self.delay = delay
        self.verify = verify
        self.impersonate = (impersonate or "").strip() or None
        self._cffi = False
        if self.impersonate:
            try:
                from curl_cffi import requests as cr  # type: ignore

                self.session = cr.Session(impersonate=self.impersonate)
                self._cffi = True
                # curl_cffi sets a matching UA; only add language.
                self.session.headers.update({"Accept-Language": DEFAULT_HEADERS["Accept-Language"]})
            except Exception:
                self.session = requests.Session()
                self.session.headers.update(DEFAULT_HEADERS)
                self.impersonate = None
        else:
            self.session = requests.Session()
            self.session.headers.update(DEFAULT_HEADERS)

    def get(self, url: str, **kwargs) -> requests.Response:
        # Sleep AŽ po requeste (min. spacing), nie pred — menej idle pri rýchlom hoste.
        # Jidoka: žiadny scrape zakázaných konkurenčných / TecAlliance katalógov.
        assert_allowed_catalog_url(url, context="HttpClient.get")
        kwargs.setdefault("timeout", self.timeout)
        if not self._cffi:
            kwargs.setdefault("verify", self.verify)
        elif "verify" in kwargs:
            kwargs.pop("verify", None)
        t0 = time.monotonic()
        resp = self.session.get(url, **kwargs)
        resp.raise_for_status()
        elapsed = time.monotonic() - t0
        if self.delay > 0 and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        return resp

    def post(self, url: str, **kwargs) -> requests.Response:
        assert_allowed_catalog_url(url, context="HttpClient.post")
        kwargs.setdefault("timeout", self.timeout)
        if not self._cffi:
            kwargs.setdefault("verify", self.verify)
        elif "verify" in kwargs:
            kwargs.pop("verify", None)
        t0 = time.monotonic()
        resp = self.session.post(url, **kwargs)
        resp.raise_for_status()
        elapsed = time.monotonic() - t0
        if self.delay > 0 and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        return resp


class LoginMixin:
    """Prihlásenie e-mail/heslo stačí — API token nie je povinný.

    Skript sa prihlási cez bežný webový formulár, session cookie ostane
    v HttpClient.session a ďalší crawl ide ako prihlásený používateľ
    (B2B ceny, dostupnosť, skryté položky).
    """

    _logged_in: bool = False

    def ensure_login(self) -> bool:
        if getattr(self, "_logged_in", False):
            return True
        creds = getattr(self, "credentials", {}) or {}
        user = creds.get("user")
        password = creds.get("password")
        if not user or not password:
            return False

        login_url = creds.get("login_url") or ""
        if not login_url:
            return False

        http: HttpClient = self.http  # type: ignore[attr-defined]
        try:
            # 1) GET login page (CSRF / cookies)
            page = http.get(login_url)
            soup = BeautifulSoup(page.text, "html.parser")
            form = soup.find("form")
            payload: Dict[str, str] = {}
            post_url = login_url
            if form:
                action = form.get("action")
                if action:
                    post_url = urljoin(login_url, action)
                for inp in form.find_all("input"):
                    name = inp.get("name")
                    if not name:
                        continue
                    payload[name] = inp.get("value") or ""

            # Bežné polia login formulárov (SK/EN e-shopy)
            user_keys = ("email", "Email", "username", "UserName", "user", "login", "Login", "j_username")
            pass_keys = ("password", "Password", "passwd", "j_password")
            for key in user_keys:
                if key in payload or not payload:
                    payload[key] = user
            for key in pass_keys:
                if key in payload or not any(k in payload for k in pass_keys):
                    payload[key] = password
            # Ak formulár nemal známe polia, pošli univerzálny set
            if not any(k in payload for k in user_keys):
                payload.update({"email": user, "username": user, "UserName": user, "login": user})
            if not any(k in payload for k in pass_keys):
                payload.update({"password": password, "Password": password})

            resp = http.post(post_url, data=payload)
            # HTTP 200 na login URL ešte neznamená úspech — skontroluj redirect / formulár.
            final_url = (getattr(resp, "url", "") or "").lower()
            body = (resp.text or "").lower()
            still_login = (
                "login" in final_url
                or "prihlas" in final_url
                or ('name="password"' in body and "logout" not in body)
            )
            self._logged_in = resp.status_code < 400 and not still_login
            return self._logged_in
        except Exception:
            self._logged_in = False
            return False


def parse_price(text: Any) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^\d,\.]", "", str(text).replace("\xa0", " ").strip())
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def absolutize(base: str, url: str) -> str:
    return urljoin(base, url)


def same_host(url: str, host: str) -> bool:
    try:
        return host in (urlparse(url).netloc or "")
    except Exception:
        return False


class BaseSupplier(abc.ABC):
    """Každý dodávateľ implementuje discovery + parse + (voliteľne) API auth."""

    name: str = "generic"
    base_url: str = ""

    def __init__(self, http: Optional[HttpClient] = None, credentials: Optional[Dict[str, str]] = None):
        self.http = http or HttpClient()
        self.credentials = credentials or {}

    @abc.abstractmethod
    def discover_product_urls(self, limit: Optional[int] = None) -> List[str]:
        """Nájde URL produktov v katalógu dodávateľa."""

    @abc.abstractmethod
    def parse_product(self, url: str) -> Optional[ProductRaw]:
        """Stiahne a sparsuje jeden produkt do ProductRaw."""

    def fetch_offer(self, url: str) -> Optional[OfferSnapshot]:
        """Rýchly sync B2B: nákupná cena + sklad + dodanie + maloobchod (akcia)."""
        product = self.parse_product(url)
        if not product:
            return None
        cost = parse_price(product.cost_price)
        sale = parse_price(product.sale_price)
        listed = parse_price(product.regular_price)
        extra = product.raw_extra if isinstance(product.raw_extra, dict) else {}
        hint = parse_price(extra.get("retail_hint")) if extra else None
        list_extra = parse_price(extra.get("list_price")) if extra else None
        if list_extra and list_extra > 0:
            listed = list_extra

        # Aktuálna predajná MOC: hint (verejný final) > sale > regular.
        # NIKDY neber list/„bola“ cenu ako aktuálny predaj.
        current = hint or sale or listed
        if cost is None and listed is not None and getattr(self, "_logged_in", False):
            # Po B2B logine regular môže byť nákup — len ak nemáme oddelený cost/hint.
            if hint is None and sale is None:
                cost = listed
                current = None

        # Pôvodná MOC (prečiarknuté) len ak je vyššia než aktuálna (reálna zľava).
        list_moc = None
        if listed and current and listed > current + 0.009:
            # Absurdné „zľavy“ (10×+) = scrap šum, nie maloobchodná akcia.
            if listed <= current * 5:
                list_moc = listed

        return OfferSnapshot(
            supplier_url=url,
            source=self.name,
            cost_price=cost,
            retail_hint=current,
            regular_price=list_moc or current,
            sale_price=current if list_moc else None,
            stock_status=product.stock_status,
            stock_qty=product.stock_qty,
            delivery_days=product.delivery_days,
            delivery_text=product.delivery_text or "",
            currency=product.currency,
            sku=product.sku,
            name=product.name,
        )

    def iter_catalog(self, limit: Optional[int] = None) -> Iterator[ProductRaw]:
        for url in self.discover_product_urls(limit=limit):
            try:
                product = self.parse_product(url)
                if product and product.name:
                    yield product
            except Exception:
                continue

    def soup(self, url: str) -> BeautifulSoup:
        resp = self.http.get(url)
        return BeautifulSoup(resp.text, "html.parser")


def extract_b2b_signals(soup: BeautifulSoup, page_text: str = "") -> Dict[str, Any]:
    """Univerzálna extrakcia dodávateľskej ceny, skladu a rýchlosti dodania z HTML."""
    text = page_text or soup.get_text("\n", strip=True)
    lower = text.lower()
    out: Dict[str, Any] = {}

    # --- Dodávateľská / B2B / nákupná cena ---
    cost_patterns = [
        r"(?:veľkoobchodn[áa]\s*cena|nákupn[áa]\s*cena|cena\s*pre\s*partnera|tvoja\s*cena|your\s*price|net\s*price|b2b\s*cena|moc\s*bez\s*dph|cena\s*bez\s*dph)\s*[:\s]*([0-9]+[.,\s]?[0-9]*)",
        r"(?:wholesale|dealer\s*price|nettopreis)\s*[:\s]*([0-9]+[.,\s]?[0-9]*)",
    ]
    for pat in cost_patterns:
        m = re.search(pat, text, re.I)
        if m:
            out["cost_price"] = parse_price(m.group(1))
            break

    # CSS selektory typické pre B2B
    if out.get("cost_price") is None:
        for sel in (
            ".price-wholesale", ".wholesale-price", ".b2b-price", ".partner-price",
            ".price-netto", ".your-price", "[data-price-type=wholesale]",
            ".price--wholesale", ".c-price__wholesale",
        ):
            el = soup.select_one(sel)
            if el:
                out["cost_price"] = parse_price(el.get_text())
                if out["cost_price"] is not None:
                    break

    # --- Odporúčaná maloobchodná (MOC) ---
    retail_patterns = [
        r"(?:odporúčan[áa]\s*cena|maloobchodn[áa]\s*cena|moc|rrp|uvp)\s*[:\s]*([0-9]+[.,\s]?[0-9]*)",
    ]
    for pat in retail_patterns:
        m = re.search(pat, text, re.I)
        if m:
            out["retail_hint"] = parse_price(m.group(1))
            break

    # --- Skladové zásoby (ks) ---
    qty_patterns = [
        r"(?:skladom|na\s*sklade|available|lager|qty|množstvo)\s*[:\s]*(\d+[.,]?\d*)\s*(?:ks|ks\.|pcs|ks\b)?",
        r"(\d+[.,]?\d*)\s*(?:ks|pcs)\s*(?:skladom|na\s*sklade|available)",
        r"sklad(?:ová)?\s*zásoba\s*[:\s]*(\d+)",
    ]
    for pat in qty_patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                out["stock_qty"] = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
            break

    if out.get("stock_qty") is None:
        for sel in ("[data-stock]", ".stock-qty", ".product-stock", ".availability-qty", ".c-availability__qty"):
            el = soup.select_one(sel)
            if el:
                val = el.get("data-stock") or el.get_text()
                qty = parse_price(val)
                if qty is not None:
                    out["stock_qty"] = qty
                    break

    # --- Rýchlosť dodania ---
    delivery_patterns = [
        (r"do\s*24\s*hod", 1, "do 24 hodín"),
        (r"24\s*hod", 1, "do 24 hodín"),
        (r"nasledujúc[ei]\s*deň|na\s*druhý\s*deň|next\s*day", 1, "na druhý deň"),
        (r"1\s*[-–]\s*2\s*(?:pracovn[ée]\s*)?dni", 2, "1–2 pracovné dni"),
        (r"2\s*[-–]\s*3\s*(?:pracovn[ée]\s*)?dni", 3, "2–3 pracovné dni"),
        (r"3\s*[-–]\s*5\s*(?:pracovn[ée]\s*)?dni", 5, "3–5 pracovných dní"),
        (r"dodanie\s*[:\s]*(\d+)\s*(?:pracovn[ée]\s*)?dni", None, None),
        (r"delivery\s*[:\s]*(\d+)\s*days?", None, None),
        (r"expedícia\s*(?:do|v\s*)?\s*(\d+)\s*d", None, None),
    ]
    for pat, days, label in delivery_patterns:
        m = re.search(pat, lower, re.I)
        if not m:
            continue
        if days is None:
            try:
                days = int(m.group(1))
            except Exception:
                continue
            label = f"do {days} dní"
        out["delivery_days"] = days
        out["delivery_text"] = label
        break

    if "skladom" in lower or "na sklade" in lower:
        out.setdefault("stock_status", "instock")
        if out.get("delivery_days") is None:
            out["delivery_days"] = 1
            out["delivery_text"] = out.get("delivery_text") or "skladom — expedícia do 24 hodín"
    if any(k in lower for k in ("vypredané", "nedostupné", "out of stock")):
        out["stock_status"] = "outofstock"

    return out
