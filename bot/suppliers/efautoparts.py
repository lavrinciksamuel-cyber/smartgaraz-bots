#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EF Autoparts (Emil Frey Group Slovakia) — https://efautoparts.sk/sk

B2B portál za loginom. Prihlásenie majiteľ dodá v priebehu týždňa
(EFAUTOPARTS_USER / EFAUTOPARTS_PASSWORD). Do vtedy je adapter pripravený:
- default login URL
- registrácia v get_supplier('efautoparts')
- jasná chyba bez credentials

Po credentials: doplniť ensure_login + discover/parse podľa reálneho HTML/API.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterator, List, Optional
from urllib.parse import urljoin

from .base import LoginMixin, ProductRaw
from .generic import GenericSupplier

LOGGER = logging.getLogger("sg.efautoparts")

DEFAULT_BASE = "https://efautoparts.sk"
DEFAULT_LOGIN = "https://efautoparts.sk/sk/prihlasenie"


class EfautopartsSupplier(LoginMixin, GenericSupplier):
    """Emil Frey / EF Autoparts B2B — čaká na credentials."""

    def __init__(self, http=None, credentials: Optional[Dict[str, str]] = None):
        creds = dict(credentials or {})
        base = (
            creds.get("base_url")
            or os.getenv("EFAUTOPARTS_BASE_URL", "").strip()
            or DEFAULT_BASE
        )
        super().__init__(
            base_url=base.rstrip("/"),
            http=http,
            credentials=creds,
            source_name="efautoparts",
        )
        self.login_url = (
            creds.get("login_url")
            or os.getenv("EFAUTOPARTS_LOGIN_URL", "").strip()
            or DEFAULT_LOGIN
        )
        self.user = creds.get("user") or os.getenv("EFAUTOPARTS_USER", "").strip()
        self.password = creds.get("password") or os.getenv("EFAUTOPARTS_PASSWORD", "").strip()

    def has_credentials(self) -> bool:
        return bool(self.user and self.password)

    def ensure_login(self) -> bool:
        if not self.has_credentials():
            LOGGER.error(
                "EF Autoparts: chýbajú EFAUTOPARTS_USER / EFAUTOPARTS_PASSWORD "
                "(majiteľ dodá prihlásenie v priebehu týždňa). Login: %s",
                self.login_url,
            )
            return False
        # Generic form POST — po reálnom logine doladiť polia (name/email/…).
        self.credentials.setdefault("login_url", self.login_url)
        self.credentials.setdefault("user", self.user)
        self.credentials.setdefault("password", self.password)
        ok = super().ensure_login()
        if not ok:
            LOGGER.error("EF Autoparts: login zlyhal na %s — treba doladiť form fields.", self.login_url)
        return ok

    def discover_product_urls(self, limit: int = 50) -> List[str]:
        if not self.ensure_login():
            return []
        # Po logine: skús generic crawl; ak portál je SPA, prepis cez API.
        urls = super().discover_product_urls(limit=limit)
        if not urls:
            LOGGER.warning(
                "EF Autoparts: po logine 0 URL — portál je pravdepodobne SPA; "
                "doplň API discover po dodaní credentials."
            )
        return urls

    def parse_product(self, url: str) -> Optional[ProductRaw]:
        if not self.ensure_login():
            return None
        product = super().parse_product(url)
        if product:
            product.source = "efautoparts"
            # Fitment z attributes (ak ich feed/HTML dá) — ai_engine už mapuje.
        return product

    def iter_catalog(self, limit: int = 50) -> Iterator[ProductRaw]:
        if not self.has_credentials():
            LOGGER.error(
                "EF Autoparts: credentials ešte nie sú (dodá majiteľ). "
                "Crawl skip. Login URL: %s",
                self.login_url,
            )
            return iter(())
        yield from super().iter_catalog(limit=limit)
