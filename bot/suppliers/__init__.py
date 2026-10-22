#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Registry dodávateľov — jeden vstupný bod pre import/sync.

Použitie:
    from suppliers import get_supplier
    supplier = get_supplier("pro-tec")
    for product in supplier.iter_catalog(limit=50):
        ...
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from .base import BaseSupplier, HttpClient
from .generic import GenericSupplier
from .protec import ProtecSupplier
from .wurth import WurthSupplier
from .lkq import LkqSupplier
from .szekop import SzekopSupplier
from .metabond import MetabondSupplier
from .efautoparts import EfautopartsSupplier
from .linkentry import LinkentrySupplier
from .jlm import JlmSupplier
from .ideo import IdeoSupplier


def _creds(prefix: str) -> Dict[str, str]:
    mapping = {
        "api_url": f"{prefix}_API_URL",
        "api_token": f"{prefix}_API_TOKEN",
        "user": f"{prefix}_USER",
        "password": f"{prefix}_PASSWORD",
        "login_url": f"{prefix}_LOGIN_URL",
        "detail_url_template": f"{prefix}_DETAIL_URL_TEMPLATE",
        "category_url": f"{prefix}_CATEGORY_URL",
    }
    out = {}
    for key, env_key in mapping.items():
        val = os.getenv(env_key, "").strip()
        if val:
            out[key] = val
    # Login z HQ (hq.smartgaraz.sk → Dodávatelia) má prednosť; env/secrets = fallback.
    slug = _HQ_SLUG.get(prefix)
    if slug:
        try:
            from .hq_credentials import supplier_profile
            prof = supplier_profile(slug)
        except Exception:
            prof = {}
        if prof.get("username") and prof.get("password"):
            out["user"], out["password"] = prof["username"], prof["password"]
        if prof.get("api_key"):
            out["api_token"] = prof["api_key"]
    return out


_HQ_SLUG = {"LKQ": "lkq", "SZEKOP": "szekop", "WURTH": "wurth", "PROTEC": "pro-tec", "METABOND": "metabond", "IDEO": "ideo"}


def get_supplier(name: str, base_url: Optional[str] = None, category_url: Optional[str] = None) -> BaseSupplier:
    # Lazy import — suppliers balík sa načítava aj mimo bot/ cwd.
    import sys
    from pathlib import Path

    _bot = Path(__file__).resolve().parent.parent
    if str(_bot) not in sys.path:
        sys.path.insert(0, str(_bot))
    from catalog_licensing import assert_allowed_catalog_url

    key = (name or "").strip().lower()
    http = HttpClient(delay=float(os.getenv("CRAWL_DELAY", "0.25")))

    if category_url:
        assert_allowed_catalog_url(category_url, context=f"get_supplier({key}).category_url")
    if base_url:
        assert_allowed_catalog_url(base_url, context=f"get_supplier({key}).base_url")

    if key in ("pro-tec", "protec", "pro_tec"):
        return ProtecSupplier(http=http, credentials=_creds("PROTEC"))
    if key in ("metabond", "meta-bond"):
        return MetabondSupplier(http=http, credentials=_creds("METABOND"))
    if key in ("jlm", "jlmaditiva", "jlm-lubricants", "jlm_lubricants"):
        creds = _creds("JLM")
        feed = os.getenv("JLM_FEED_URL", "").strip() or os.getenv("JLM_XML_URL", "").strip()
        if feed:
            creds["feed_url"] = feed
        return JlmSupplier(http=http, credentials=creds)
    if key in ("wurth", "würth"):
        creds = _creds("WURTH")
        if category_url:
            creds["category_url"] = category_url
        return WurthSupplier(http=http, credentials=creds)
    if key == "lkq":
        creds = _creds("LKQ")
        if category_url:
            creds["category_url"] = category_url
        return LkqSupplier(http=http, credentials=creds)
    if key == "szekop":
        creds = _creds("SZEKOP")
        if category_url:
            creds["category_url"] = category_url
        return SzekopSupplier(http=http, credentials=creds)
    if key in ("efautoparts", "ef-autoparts", "ef_autoparts", "emilfrey", "emil-frey"):
        # Credentials prídu neskôr (EFAUTOPARTS_USER/PASSWORD) — stub je pripravený.
        return EfautopartsSupplier(http=http, credentials=_creds("EFAUTOPARTS"))
    if key in ("linkentry", "link-entry", "linktry", "mypeople", "stellantis-parts", "fiat-catalog", "eper"):
        # Default crawl OFF — citlivé IT; secrets = LINKTRY_* (majiteľ).
        delay = max(float(os.getenv("CRAWL_DELAY", "3")), 3.0)
        http_slow = HttpClient(delay=delay)
        creds = _creds("LINKTRY")
        # fallback alias prefix
        if not creds.get("user"):
            creds = {**_creds("LINKENTRY"), **creds}
        return LinkentrySupplier(http=http_slow, credentials=creds)
    if key in ("ideo", "ideo.sk"):
        return IdeoSupplier(http=http, credentials=_creds("IDEO"))
    if key in ("generic", "any", "custom") or base_url:
        return GenericSupplier(
            base_url=base_url or os.getenv("GENERIC_BASE_URL", ""),
            http=http,
            credentials=_creds("GENERIC"),
            source_name=key if key not in ("generic", "any", "custom") else "generic",
        )
    raise ValueError(
        f"Neznámy dodávateľ '{name}'. Podporované: pro-tec, metabond, jlm, wurth, lkq, "
        f"szekop, efautoparts, linkentry, generic"
    )


AVAILABLE_SUPPLIERS = (
    "pro-tec",
    "metabond",
    "jlm",
    "wurth",
    "lkq",
    "szekop",
    "efautoparts",
    "linkentry",
    "ideo",
    "generic",
)
