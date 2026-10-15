#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LinkEntry / ePER (Fiat · Alfa · Jeep / Stellantis).

Primárne: ``https://eper.parts.fiat.com`` (nie ADFS linkentry redirect).
**Opatrný režim** — talianske servery sú citlivé na crawl.
Default: **NEscrapuj** katalóg (žiadny dump, žiadne TecDoc ID).

Cieľ: mikro OE / VIN / **service checklist** pozorovanie → ``sgv-*``.
Pozri ``bot/data/STELLANTIS_SERVICE_CHECKLIST.md``.

Zapnutie: ``LINKTRY_ALLOW_LOOKUP=1``
Secrets (naschvál): ``LINKTRY_USER``, ``LINKTRY_PASSWORD``,
``LINKTRY_BASE_URL`` (default ePER), ``LINKTRY_LOGIN_TYPE`` (UPRS|CSPS).
Alias: ``LINKENTRY_*``.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterator, List, Optional

from .base import BaseSupplier, ProductRaw

LOGGER = logging.getLogger("sg.linkentry")

DEFAULT_BASE = "https://eper.parts.fiat.com"
DEFAULT_LOGIN = "https://eper.parts.fiat.com/login"


class LinkentrySupplier(BaseSupplier):
    """Stellantis ePER / LinkEntry — stub + kill-switch proti scrapu."""

    source_name = "linkentry"

    def __init__(self, http=None, credentials: Optional[Dict[str, str]] = None):
        creds = dict(credentials or {})
        super().__init__(http=http, credentials=creds)
        # Primárne LINKTRY_* (majiteľ naschvál), alias LINKENTRY_* / EPER_*.
        self.user = (
            creds.get("user")
            or os.getenv("LINKTRY_USER", "").strip()
            or os.getenv("LINKENTRY_USER", "").strip()
            or os.getenv("EPER_USER", "").strip()
        )
        self.password = (
            creds.get("password")
            or os.getenv("LINKTRY_PASSWORD", "").strip()
            or os.getenv("LINKENTRY_PASSWORD", "").strip()
            or os.getenv("EPER_PASSWORD", "").strip()
        )
        self.base_url = (
            creds.get("base_url")
            or os.getenv("LINKTRY_BASE_URL", "").strip()
            or os.getenv("LINKENTRY_BASE_URL", "").strip()
            or os.getenv("EPER_BASE_URL", "").strip()
            or DEFAULT_BASE
        ).rstrip("/")
        self.login_url = (
            creds.get("login_url")
            or os.getenv("LINKTRY_LOGIN_URL", "").strip()
            or os.getenv("LINKENTRY_LOGIN_URL", "").strip()
            or os.getenv("EPER_LOGIN_URL", "").strip()
            or f"{self.base_url}/login"
        )
        self.login_type = (
            creds.get("login_type")
            or os.getenv("LINKTRY_LOGIN_TYPE", "").strip()
            or os.getenv("LINKENTRY_LOGIN_TYPE", "").strip()
            or os.getenv("EPER_LOGIN_TYPE", "").strip()
            or "UPRS"
        ).upper()
        self.allow_lookup = (
            (
                os.getenv("LINKTRY_ALLOW_LOOKUP", "")
                or os.getenv("LINKENTRY_ALLOW_LOOKUP", "")
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

    def has_credentials(self) -> bool:
        return bool(self.user and self.password and self.base_url)

    def status(self) -> Dict[str, object]:
        return {
            "supplier": "linkentry",
            "portal": "eper",
            "has_credentials": self.has_credentials(),
            "allow_lookup": self.allow_lookup,
            "base_url": self.base_url,
            "login_url": self.login_url,
            "login_type": self.login_type,
            "scrape_catalog": False,
            "mode": "checklist_oe_facts_only",
            "note": (
                "Default crawl OFF. Cieľ = service checklist + OE fakty → sgv-*; "
                "nie Service Package mirror. Heslo nikdy do chatu."
            ),
        }

    def ensure_login(self) -> bool:
        if not self.has_credentials():
            LOGGER.error(
                "ePER: chýbajú LINKTRY_USER / LINKTRY_PASSWORD "
                "(Runtime Secret — nie chat). Login: %s type=%s",
                self.login_url,
                self.login_type,
            )
            return False
        if not self.allow_lookup:
            LOGGER.warning(
                "ePER: credentials OK, LINKTRY_ALLOW_LOOKUP/LINKENTRY_ALLOW_LOOKUP!=1 — "
                "login zámerme vypnutý (citlivé IT). Pre jemný login nastav "
                "LINKTRY_ALLOW_LOOKUP=1."
            )
            return False
        LOGGER.info(
            "ePER: allow_lookup=1 — live POST login ešte po dohode "
            "(form: username/password/loginType=%s). Zatiaľ bez requestu.",
            self.login_type,
        )
        return False

    def discover_product_urls(self, limit: int = 20) -> List[str]:
        LOGGER.error(
            "ePER: catalog crawl ZAKÁZANÝ. Použi service checklist pozorovanie "
            "(STELLANTIS_SERVICE_CHECKLIST.md), nie dump."
        )
        return []

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        LOGGER.error("ePER: parse_product zakázané. url=%s", url[:80])
        return None

    def iter_catalog(self, limit: int = 20) -> Iterator[ProductRaw]:
        _ = limit
        return iter(())


def linkentry_ready_message() -> str:
    s = LinkentrySupplier()
    st = s.status()
    if not st["has_credentials"]:
        return (
            "ePER: Cursor Secrets → LINKTRY_USER + LINKTRY_PASSWORD "
            "(naschvál LINKTRY_*) + voliteľne LINKTRY_BASE_URL="
            "https://eper.parts.fiat.com (heslo nie do chatu)."
        )
    if not st["allow_lookup"]:
        return (
            "ePER: LINKTRY secrets OK. Lookup OFF — po dohode "
            "LINKTRY_ALLOW_LOOKUP=1 len checklist/OE mikro, nie scrape."
        )
    return (
        "ePER: secrets + allow_lookup — pripravené na jemný login "
        f"(loginType={st['login_type']}), bez bulk crawl."
    )
