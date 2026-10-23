#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Doplnenie reálnych fotiek Würth podľa artiklového čísla.

Zdroje, ktoré odpovedajú obyčajným HTTP 200 (žiadne impersonovanie, proxy,
stealth). Cena, sklad a status sa nečítajú a do Woo sa neposielajú.

- Bosna (eshop.wurth.ba): produktová URL zo sitemapu má v prvej zložke
  artiklové číslo. Stránka musí mať ``data-product-code`` rovné tomu číslu.
  Berieme len obrázky, ktorých názov súboru obsahuje to isté číslo
  (``08931066.jpg``, ``07201711x.jpg``). Súbor ``/{sku}.jpg`` s JPEG telom
  nad 6 kB je druhá možnosť, keď sitemap produkt nemá. PNG 4511 B je
  Magento „žiadna fotka“ a zahadzuje sa.
- Kosovo (eshop.wurth-kosova.com): sitemap URL končí artiklovým číslom.
  Berieme ``/media/`` súbory s tým číslom v názve, nie ``/thumbnail/`` a nie
  fotky iných artiklov na tej istej stránke.

robots.txt: na BA je ``Disallow: /*?q=``, na Kosove ``Disallow: /*?``.
Search URL sa nevolajú. Priamy media súbor a pretty URL zo sitemapu áno.

Zápis (``--apply``) vyžaduje ``WURTH_MEDIA_OK=1``. Bez neho je beh vždy
len dry-run. Importný prepínač fotiek sa tým nemení.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_licensing import assert_allowed_catalog_url  # noqa: E402

LOG = logging.getLogger("wurth_photo_backfill")

USER_AGENT = "SmartGarazBot/1.0 (wurth-photo-backfill; plain HTTP; +https://smartgaraz.sk)"
BA_SITEMAP = "https://eshop.wurth.ba/sitemap.xml"
KOS_SITEMAP = "https://eshop.wurth-kosova.com/sitemap.xml"
BA_HOST = "eshop.wurth.ba"
KOS_HOST = "eshop.wurth-kosova.com"
SOURCE_BA = "wurth_public_ba"
SOURCE_KOS = "wurth_public_kosovo"

# Magento na BA vracia pri chýbajúcom súbore PNG placeholder, 4511 B, HTTP 200.
PLACEHOLDER_PNG_BYTES = 4511
MIN_PHOTO_BYTES = 6000
MAX_GALLERY = 8
PAUSE_S = float(os.environ.get("WURTH_PHOTO_DELAY", "0.4") or 0.4)

PLACEHOLDER_MARKERS = (
    "uploads/smartgaraz-placeholders/",
    "uploads/smartgaraz-repair/",
    "smartgaraz-placeholders",
    "smartgaraz-repair",
)
DECORATIVE_HINTS = (
    "/logo",
    "favicon",
    "/wysiwyg/",
    "/shops/",
    "placeholder",
    "/thumbnail/",
    "/resized/",
    "no-image",
    "noimage",
    "emptyimage",
)
# Woo update smie obsahovať len tieto kľúče. Cena, sklad a status nikdy.
UPDATE_KEYS = frozenset({"id", "images", "meta_data"})
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_IMG_URL = re.compile(r"https?://[^\"'\s>]+\.(?:jpe?g|png|webp)(?:\?[^\"'\s>]*)?", re.I)
_BA_FILE = re.compile(
    r"/media/catalog/product/(?:cache/[^/]+/)?([^/]+)/([^/]+)/([^/?#]+\.(?:jpe?g|png|webp))$",
    re.I,
)


def normalize_article(value: str) -> str:
    """Artiklové číslo bez medzier. ``0890 100043`` a HTML entita sú to isté."""
    return "".join(html.unescape(value or "").split())


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower().rstrip(".")


def _stem(url: str) -> str:
    name = unquote(urlparse(url).path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def filename_matches_article(url: str, sku: str) -> bool:
    """Názov súboru obsahuje artikl ako celé číslo, nie ako podreťazec iného."""
    sku = normalize_article(sku)
    if not sku:
        return False
    stem = _stem(url)
    return re.search(rf"(?<![0-9]){re.escape(sku)}(?![0-9])", stem) is not None


def is_decorative(url: str) -> bool:
    low = (url or "").lower()
    return any(hint in low for hint in DECORATIVE_HINTS)


def is_generated_placeholder(src: str) -> bool:
    low = (src or "").lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def image_srcs(images: Optional[Sequence]) -> List[str]:
    out: List[str] = []
    for img in images or []:
        if isinstance(img, str):
            src = img
        elif isinstance(img, dict):
            src = str(img.get("src") or "")
        else:
            src = ""
        src = src.strip()
        if src:
            out.append(src)
    return out


def needs_photo(images: Optional[Sequence]) -> bool:
    """True ak produkt nemá fotku, alebo má len generovaný smartgaraz placeholder."""
    srcs = image_srcs(images)
    if not srcs:
        return True
    return all(is_generated_placeholder(src) for src in srcs)


def article_from_ba_product_url(url: str) -> Optional[str]:
    """Prvá zložka pretty URL je artikl (``/0893%201066/slug`` → ``08931066``)."""
    if not url or "?" in url:
        return None
    parts = [p for p in unquote(urlparse(url).path).strip("/").split("/") if p]
    if len(parts) != 2:
        return None
    art = normalize_article(parts[0])
    if art.isdigit() and len(art) >= 4:
        return art
    return None


def article_from_kosovo_url(url: str) -> Optional[str]:
    """Posledná zložka produktovej URL je artikl. Query URL (search) sa neberú."""
    if not url or "?" in url:
        return None
    if host_of(url) not in ("", KOS_HOST):
        return None
    parts = [p for p in unquote(urlparse(url).path).strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    art = parts[-1]
    if art.isdigit() and len(art) >= 4:
        return art
    return None


def ba_direct_url(sku: str) -> Optional[str]:
    sku = normalize_article(sku)
    if not sku.isdigit() or len(sku) < 2:
        return None
    return f"https://{BA_HOST}/media/catalog/product/{sku[0]}/{sku[1]}/{sku}.jpg"


def ba_article_confirmed(html_text: str, sku: str) -> bool:
    """Prvé ``data-product-code`` na stránke je tento artikl, nie susedný výrobok."""
    sku = normalize_article(sku)
    codes = [
        normalize_article(c)
        for c in re.findall(r'data-product-code="([^"]*)"', html_text or "")
    ]
    codes = [c for c in codes if c]
    return bool(codes) and codes[0] == sku


def kosovo_article_confirmed(page_url: str, sku: str) -> bool:
    sku = normalize_article(sku)
    parts = [p for p in unquote(urlparse(page_url).path).strip("/").split("/") if p]
    return bool(parts) and parts[-1] == sku


def canonical_ba_url(url: str) -> str:
    """Cache hash v ceste nie je stabilný. Originál je ``/media/catalog/product/a/b/file``."""
    path = unquote(urlparse(url).path)
    match = _BA_FILE.search(path)
    if not match:
        return url.split("#")[0]
    folder_a, folder_b, filename = match.group(1), match.group(2), match.group(3)
    return f"https://{BA_HOST}/media/catalog/product/{folder_a}/{folder_b}/{filename}"


def _rank(url: str, sku: str) -> Tuple[int, str]:
    stem = _stem(url)
    exact = re.fullmatch(rf"{re.escape(sku)}x*", stem, re.I) is not None
    return (0 if exact else 1, stem.lower())


def _grouped_candidates(urls: Iterable[str], sku: str, *, canonical) -> List[List[str]]:
    """Jedna skupina na názov súboru. Prvá URL v skupine sa skúša ako prvá."""
    groups: Dict[str, List[str]] = {}
    order: List[str] = []
    for raw in urls:
        raw = (raw or "").strip()
        if not raw or is_decorative(raw) or not filename_matches_article(raw, sku):
            continue
        primary = canonical(raw) if canonical else raw.split("#")[0]
        options = [primary]
        bare = raw.split("#")[0]
        if bare not in options:
            options.append(bare)
        key = _stem(primary).lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        for opt in options:
            if opt not in groups[key]:
                groups[key].append(opt)
    order.sort(key=lambda key: _rank(groups[key][0], sku))
    return [groups[key] for key in order]


def ordered_ba_candidates(html_text: str, sku: str) -> List[List[str]]:
    found = _IMG_URL.findall(html_text or "")
    same_host = [u for u in found if host_of(u) == BA_HOST]
    return _grouped_candidates(same_host, sku, canonical=canonical_ba_url)


def ordered_kosovo_candidates(html_text: str, sku: str) -> List[List[str]]:
    found = _IMG_URL.findall(html_text or "")
    same_host = []
    for url in found:
        if host_of(url) != KOS_HOST:
            continue
        if "/media/" not in urlparse(url).path:
            continue
        same_host.append(url.split("#")[0])
    return _grouped_candidates(same_host, sku, canonical=None)


def image_is_real_photo(
    *,
    status: int,
    content_type: str,
    size: int,
    magic: bytes,
    final_url: str,
    sku: str,
) -> bool:
    """Skutočná JPEG fotka tohto artiklu. PNG placeholder (4511 B) nie je fotka."""
    if status != 200:
        return False
    if not filename_matches_article(final_url, sku):
        return False
    if is_decorative(final_url):
        return False
    blob = magic or b""
    if blob.startswith(PNG_MAGIC):
        return False
    if not blob.startswith(JPEG_MAGIC):
        return False
    ctype = (content_type or "").lower()
    if any(bad in ctype for bad in ("png", "gif", "html", "text/", "json")):
        return False
    if size < MIN_PHOTO_BYTES or size == PLACEHOLDER_PNG_BYTES:
        return False
    return True


@dataclass
class ImageProbe:
    status: int
    content_type: str
    size: int
    magic: bytes
    final_url: str


@dataclass
class PhotoMatch:
    sku: str
    source: str
    urls: List[str]
    verified_by: str


class PublicHttp:
    """Obyčajný requests. Žiadne TLS impersonovanie, proxy ani falošné Chrome hlavičky."""

    def __init__(self, session: Optional[requests.Session] = None, pause_s: float = PAUSE_S):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.pause_s = pause_s

    def _pause(self) -> None:
        if self.pause_s > 0:
            time.sleep(self.pause_s)

    def get_bytes(self, url: str, timeout: float) -> Tuple[int, bytes, str]:
        assert_allowed_catalog_url(url, context="wurth_photo_backfill")
        response = self.session.get(url, timeout=timeout, allow_redirects=True)
        self._pause()
        data = response.content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return response.status_code, data, response.url

    def get_text(self, url: str, timeout: float = 90) -> str:
        status, data, final = self.get_bytes(url, timeout=timeout)
        if status != 200:
            LOG.warning("HTTP %s %s", status, url[:140])
            return ""
        if host_of(final) not in (BA_HOST, KOS_HOST):
            LOG.warning("presmerovanie mimo Würth foto hosta: %s", final[:140])
            return ""
        return data.decode("utf-8", "replace")

    def inspect_image(self, url: str) -> ImageProbe:
        assert_allowed_catalog_url(url, context="wurth_photo_backfill")
        response = self.session.get(url, timeout=25, allow_redirects=True)
        self._pause()
        data = response.content or b""
        return ImageProbe(
            response.status_code,
            response.headers.get("content-type") or "",
            len(data),
            data[:16],
            response.url,
        )


def _verified_urls(
    groups: Sequence[Sequence[str]],
    sku: str,
    inspect: Callable[[str], ImageProbe],
) -> List[str]:
    kept: List[str] = []
    for options in groups:
        for url in options:
            if host_of(url) not in (BA_HOST, KOS_HOST):
                continue
            try:
                probe = inspect(url)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("fotka %s: %s", url[:120], exc)
                continue
            if image_is_real_photo(
                status=probe.status,
                content_type=probe.content_type,
                size=probe.size,
                magic=probe.magic,
                final_url=probe.final_url,
                sku=sku,
            ):
                kept.append(probe.final_url.split("#")[0])
                break
        if len(kept) >= MAX_GALLERY:
            break
    return kept


def match_article(
    sku: str,
    *,
    ba_pages: Dict[str, str],
    kos_pages: Dict[str, str],
    http: PublicHttp,
) -> Optional[PhotoMatch]:
    """Nájde fotky jedného artiklu. Zhoda je vždy podľa čísla, nie podľa názvu."""
    sku = normalize_article(sku)
    if not sku:
        return None
    page = ba_pages.get(sku)
    if page:
        html_text = http.get_text(page, timeout=30)
        final_ok = ba_article_confirmed(html_text, sku)
        if final_ok:
            urls = _verified_urls(ordered_ba_candidates(html_text, sku), sku, http.inspect_image)
            if urls:
                return PhotoMatch(sku, SOURCE_BA, urls, "ba_page")
        elif html_text:
            LOG.info("BA stránka nespárovala artikl %s", sku)
    direct = ba_direct_url(sku)
    if direct:
        urls = _verified_urls([[direct]], sku, http.inspect_image)
        if urls:
            return PhotoMatch(sku, SOURCE_BA, urls, "ba_media_filename")
    kos_page = kos_pages.get(sku)
    if kos_page and kosovo_article_confirmed(kos_page, sku):
        html_text = http.get_text(kos_page, timeout=30)
        # Číslo musí byť na stránke ako celé číslo. Názov produktu sa neporovnáva.
        if re.search(rf"(?<![0-9]){re.escape(sku)}(?![0-9])", html_text or ""):
            urls = _verified_urls(
                ordered_kosovo_candidates(html_text, sku), sku, http.inspect_image
            )
            if urls:
                return PhotoMatch(sku, SOURCE_KOS, urls, "kosovo_page")
    return None


def sitemap_locs(xml_text: str) -> List[str]:
    locs = []
    for chunk in (xml_text or "").split("<loc>")[1:]:
        loc = chunk.split("</loc>", 1)[0].strip()
        if loc:
            locs.append(loc)
    return locs


def index_from_sitemap(xml_text: str, article_of) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for loc in sitemap_locs(xml_text):
        art = article_of(loc)
        if art:
            out.setdefault(art, loc)
    return out


def load_article_index(http: PublicHttp, index_url: str, article_of) -> Dict[str, str]:
    xml_text = http.get_text(index_url)
    if not xml_text:
        return {}
    head = xml_text.lstrip()[:800]
    if "<sitemapindex" not in head:
        return index_from_sitemap(xml_text, article_of)
    out: Dict[str, str] = {}
    for child in sitemap_locs(xml_text):
        if host_of(child) not in (BA_HOST, KOS_HOST):
            continue
        try:
            child_xml = http.get_text(child)
        except requests.RequestException as exc:
            LOG.warning("sitemap %s: %s", child[:140], exc)
            continue
        for art, loc in index_from_sitemap(child_xml, article_of).items():
            out.setdefault(art, loc)
    return out


def is_wurth_product(product: dict) -> bool:
    meta = {
        m.get("key"): m.get("value")
        for m in (product.get("meta_data") or [])
        if isinstance(m, dict)
    }
    source = str(meta.get("_supplier_source") or "").strip().lower()
    if source == "wurth":
        return True
    blob = str(meta.get("supplier_url") or "")
    return "eshop.wurth" in blob or "wurth-kosova" in blob


def plan_image_update(
    product_id: int,
    urls: Sequence[str],
    *,
    source: str,
    article: str,
    fetched_at: str,
) -> dict:
    """Woo payload: hlavná fotka + galéria a proveniencia. Nič iné."""
    clean = [u for u in urls if u][:MAX_GALLERY]
    if not clean:
        raise ValueError("žiadna fotka")
    payload = {
        "id": int(product_id),
        "images": [{"src": u} for u in clean],
        "meta_data": [
            {"key": "_sg_image_source", "value": source},
            {"key": "_sg_image_original_url", "value": clean[0][:500]},
            {"key": "_sg_image_fetched_at", "value": fetched_at},
            {"key": "_sg_image_article", "value": article},
            {
                "key": "_sg_image_rights_note",
                "value": (
                    "Public Würth product photo matched by article number. "
                    "Content only; price and stock were not read."
                ),
            },
        ],
    }
    extra = set(payload) - UPDATE_KEYS
    if extra:
        raise RuntimeError(f"photo update obsahuje zakázané polia: {sorted(extra)}")
    return payload


def summarize(rows: Sequence[Optional[PhotoMatch]], candidates: int) -> dict:
    matched = [r for r in rows if r and r.urls]
    by_source: Dict[str, int] = {}
    by_verification: Dict[str, int] = {}
    for row in matched:
        by_source[row.source] = by_source.get(row.source, 0) + 1
        by_verification[row.verified_by] = by_verification.get(row.verified_by, 0) + 1
    rate = round(len(matched) / candidates, 4) if candidates else None
    return {
        "candidates": candidates,
        "matched": len(matched),
        "unmatched": candidates - len(matched),
        "match_rate": rate,
        "by_source": by_source,
        "by_verification": by_verification,
    }


def _wc_request(method: str, path: str, **kwargs):
    from shop_guard import wait_if_slow

    base = os.environ["WC_URL"].rstrip("/") + "/wp-json/wc/v3"
    auth = (os.environ["WC_CONSUMER_KEY"], os.environ["WC_CONSUMER_SECRET"])
    last = None
    for wait in (0, 15, 45, 120):
        if wait:
            time.sleep(wait)
        wait_if_slow()
        try:
            response = requests.request(method, base + path, auth=auth, timeout=120, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                return response
            last = response
        except Exception as exc:  # noqa: BLE001
            LOG.warning("WC %s %s: %s", method, path, exc)
    if last is not None:
        return last
    raise RuntimeError(f"WC {method} {path} zlyhalo")


def fetch_wurth_needing_photo(limit: int) -> List[dict]:
    from account_split import mine

    out: List[dict] = []
    page = 1
    fields = "id,sku,images,meta_data"
    while True:
        response = _wc_request(
            "GET",
            "/products",
            params={
                "search": "Würth",
                "status": "any",
                "per_page": 50,
                "page": page,
                "_fields": fields,
            },
        )
        if not response.ok:
            LOG.error("WC list HTTP %s", response.status_code)
            break
        rows = response.json()
        for product in rows:
            if not is_wurth_product(product):
                continue
            if not needs_photo(product.get("images")):
                continue
            sku = normalize_article(str(product.get("sku") or ""))
            if not sku or not mine(sku):
                continue
            product = dict(product)
            product["sku"] = sku
            out.append(product)
            if limit and len(out) >= limit:
                return out
        if len(rows) < 50:
            break
        page += 1
        time.sleep(0.5)
    return out


def skus_from_args(args: argparse.Namespace) -> List[str]:
    from account_split import mine

    found: List[str] = []
    if args.skus:
        found.extend(part.strip() for part in args.skus.split(","))
    if args.skus_file:
        text = Path(args.skus_file).read_text(encoding="utf-8")
        found.extend(line.strip() for line in text.splitlines())
    out = []
    seen = set()
    for raw in found:
        sku = normalize_article(raw)
        if not sku or sku in seen or not mine(sku):
            continue
        seen.add(sku)
        out.append(sku)
        if args.limit and len(out) >= args.limit:
            break
    return out


def _post_batches(updates: List[dict], batch: int, apply: bool) -> int:
    written = 0
    for start in range(0, len(updates), batch):
        chunk = updates[start : start + batch]
        for item in chunk:
            extra = set(item) - UPDATE_KEYS
            if extra:
                raise RuntimeError(f"zápis fotky má zakázané polia: {sorted(extra)}")
        if not apply:
            continue
        response = _wc_request("POST", "/products/batch", json={"update": chunk})
        if not response.ok:
            LOG.error("batch HTTP %s: %s", response.status_code, response.text[:300])
            continue
        result = response.json().get("update") or []
        bad = [row for row in result if isinstance(row, dict) and row.get("error")]
        if bad:
            LOG.warning("batch item error: %s", bad[0].get("error"))
        written += len(chunk) - len(bad)
        time.sleep(2)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    parser = argparse.ArgumentParser(description="Würth photo backfill (dry-run unless --apply)")
    parser.add_argument("--apply", action="store_true", help="zapíš fotky do Woo (inak len report)")
    parser.add_argument("--skus", default="", help="čiarkou oddelené artikle, bez Woo listu")
    parser.add_argument("--skus-file", default="", help="súbor s artiklom na riadok, bez Woo listu")
    parser.add_argument("--limit", type=int, default=0, help="max artiklov pre tento účet")
    parser.add_argument("--batch", type=int, default=10)
    args = parser.parse_args(argv)

    from media_licensing import wurth_media_allowed

    apply = bool(args.apply)
    if apply and not wurth_media_allowed():
        LOG.error("Zápis fotiek je vypnutý. --apply vyžaduje WURTH_MEDIA_OK=1.")
        print("SG_RESULT " + json.dumps({"ok": False, "error": "wurth_media_not_allowed"}, ensure_ascii=False), flush=True)
        return 2

    explicit = bool(args.skus or args.skus_file)
    if apply and explicit:
        LOG.error("--apply zapisuje Woo produkty podľa id. --skus je len na meranie zhody.")
        print("SG_RESULT " + json.dumps({"ok": False, "error": "apply_needs_shop_ids"}), flush=True)
        return 2
    products: List[dict] = []
    if explicit:
        for sku in skus_from_args(args):
            products.append({"id": None, "sku": sku, "images": []})
    else:
        if not os.environ.get("WC_URL"):
            LOG.error("Bez WC_URL použi --skus alebo --skus-file (dry-run proti verejným shopom).")
            print("SG_RESULT " + json.dumps({"ok": False, "error": "no_wc_and_no_skus"}), flush=True)
            return 2
        products = fetch_wurth_needing_photo(args.limit)

    http = PublicHttp()
    LOG.info("načítavam verejné sitemap Würth BA a Kosovo")
    try:
        ba_pages = load_article_index(http, BA_SITEMAP, article_from_ba_product_url)
    except requests.RequestException as exc:
        LOG.error("BA sitemap: %s", exc)
        ba_pages = {}
    try:
        kos_pages = load_article_index(http, KOS_SITEMAP, article_from_kosovo_url)
    except requests.RequestException as exc:
        LOG.error("Kosovo sitemap: %s", exc)
        kos_pages = {}
    LOG.info("sitemap artikle BA %d, Kosovo %d, kandidáti %d", len(ba_pages), len(kos_pages), len(products))

    matches: List[Optional[PhotoMatch]] = []
    updates: List[dict] = []
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for product in products:
        sku = normalize_article(str(product.get("sku") or ""))
        try:
            found = match_article(sku, ba_pages=ba_pages, kos_pages=kos_pages, http=http)
        except requests.RequestException as exc:
            LOG.warning("artikl %s: %s", sku, exc)
            found = None
        matches.append(found)
        if not found:
            continue
        LOG.info("zhoda %s %s %s (%d)", sku, found.verified_by, found.urls[0][:100], len(found.urls))
        if product.get("id") and apply:
            updates.append(
                plan_image_update(
                    int(product["id"]),
                    found.urls,
                    source=found.source,
                    article=sku,
                    fetched_at=fetched_at,
                )
            )
        elif product.get("id") and not apply:
            # Dry-run drží payload v pamäti len kvôli kontrole tvaru, nezapisuje.
            plan_image_update(
                int(product["id"]),
                found.urls,
                source=found.source,
                article=sku,
                fetched_at=fetched_at,
            )

    report = summarize(matches, len(products))
    written = 0
    if apply:
        written = _post_batches(updates, max(1, args.batch), apply=True)
    report.update({
        "ok": True,
        "dry_run": not apply,
        "updated": written,
        "price_stock_status": "unchanged",
    })
    print("SG_RESULT " + json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
