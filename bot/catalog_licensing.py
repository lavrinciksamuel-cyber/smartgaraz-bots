#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Katalógová licencia — deny-list konkurenčných / TecDoc dump hostiteľov.

Politika: ``bot/data/CATALOG_LICENSING.md`` + ``VEHICLE_TAXONOMY_POLICY.md``.

Cieľ: zabrániť scrape maloobchodných konkurenčných katalógov a oficiálnych
TecAlliance download portálov bez licencie. B2B dodávatelia (Szekop, LKQ,
Würth, …) ostávajú povolené — tam ideme s vlastným účtom, nie kópia ich
vehicle DB.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# Maloobchodní konkurenti / agregátori + oficiálne TecAlliance portály.
# Porovnávame suffix hosta (www.autodoc.sk → autodoc.sk).
FORBIDDEN_CATALOG_HOST_SUFFIXES: tuple[str, ...] = (
    # TecAlliance / TecDoc official
    "tecalliance.net",
    "tecalliance.com",
    "tecdoc.net",
    "tecdoc.com",
    "tecdoc.de",
    "tecdoc.info",
    # Retail / agregátori (nie naši B2B dodávatelia)
    "autodoc.sk",
    "autodoc.cz",
    "autodoc.de",
    "autodoc.com",
    "autodoc.co.uk",
    "filtreoleje.sk",
    "f-o-p.sk",
    "mister-auto.com",
    "mister-auto.sk",
    "mister-auto.cz",
    "motordoctor.sk",
    "motordoctor.cz",
    "autodily24.cz",
    "auto-doc.sk",
)

# Explicitne povolené B2B / brand portály (whitelist na dokumentáciu + testy).
ALLOWED_SUPPLIER_HOST_SUFFIXES: tuple[str, ...] = (
    "szekopsk.nextis.cz",
    "nextis.cz",
    "lkq.sk",
    "lkq.cz",
    "eshop.wurth.sk",
    "wurth.sk",
    "wurth.cz",
    "media.witglobal.net",  # Würth CDN (len s WURTH_MEDIA_OK + B2B účet)
    "witglobal.net",
    "protec.sk",
    "pro-tec.sk",
    "metabond.sk",
    "metabond.eu",
    "jlmaditiva.sk",
    "myshoptet.com",  # Shoptet CDN (JLM / iné Shoptet B2B feedy + fotky)
    "efautoparts.sk",
    "emilfrey.sk",
    "smartgaraz.sk",
)


class CatalogLicensingError(RuntimeError):
    """Pokus o crawl zakázaného katalógu / TecDoc dump hostiteľa."""


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower().rstrip(".")


def _matches_suffix(host: str, suffix: str) -> bool:
    h = (host or "").lower().rstrip(".")
    s = (suffix or "").lower().rstrip(".")
    if not h or not s:
        return False
    return h == s or h.endswith("." + s)


def is_forbidden_catalog_host(url_or_host: str) -> bool:
    host = host_of(url_or_host) if "://" in (url_or_host or "") or "/" in (url_or_host or "") else (
        (url_or_host or "").lower().rstrip(".")
    )
    if not host:
        return False
    # Whitelist má prednosť (napr. subdomain dodávateľa)
    if any(_matches_suffix(host, s) for s in ALLOWED_SUPPLIER_HOST_SUFFIXES):
        return False
    return any(_matches_suffix(host, s) for s in FORBIDDEN_CATALOG_HOST_SUFFIXES)


def assert_allowed_catalog_url(url: str, *, context: Optional[str] = None) -> str:
    """Raise ``CatalogLicensingError`` ak URL smeruje na zakázaný katalóg.

    Vracia pôvodné ``url`` pri OK (aj prázdne — volajúci rieši empty sám).
    """
    if not (url or "").strip():
        return url
    if is_forbidden_catalog_host(url):
        where = f" ({context})" if context else ""
        raise CatalogLicensingError(
            f"Zakázaný katalógový hostiteľ{where}: {host_of(url)!r}. "
            f"Bez licencie TecDoc / súhlasu nesmie SmartGaráž skrapovať "
            f"konkurenčné ani TecAlliance katalógy. "
            f"Pozri bot/data/CATALOG_LICENSING.md"
        )
    return url
