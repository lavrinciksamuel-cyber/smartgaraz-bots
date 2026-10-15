#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oprava / doplnenie fitmentu z cross-ref — len **overené páry** make+model.

Predtým OE-súrodenecký merge robil „hala-bála“ (BMW make + Octavia model).
Teraz:

  --repair   z `_sg_fit_sg_id` / `_sg_fit_vehicles` zlož čisté páry,
             zahoď orphan značky bez modelu, zapíš JSON `_sg_fit_vehicles`
  --discover (voliteľné) pre Oem.Manufacturer z LKQ Codes skús nájsť
             model cez Catalog (SKU musí byť v kategórii) — pomalé

  python bot/expand_fitment_from_crossref.py --repair --dry-run --limit 40 --category-slug stierace
  python bot/expand_fitment_from_crossref.py --repair --limit 40 --category-slug stierace
  python bot/expand_fitment_from_crossref.py --repair --sku 'BO 3397008998'
"""

from __future__ import annotations

import argparse
import logging
import os
try:  # loginy z HQ (hq.smartgaraz.sk) majú prednosť pred secrets
    from suppliers.hq_credentials import export_logins as _hq_export
    _hq_export("lkq")
except Exception:
    pass

import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_engine import Config, WooCommerceClient, setup_logging  # noqa: E402
from fitment import (  # noqa: E402
    extract_oe_from_lkq_codes,
    extract_oem_makes_from_lkq_codes,
    merge_oe_numbers,
    models_for_make,
    normalize_oe_code,
    vehicles_from_product_meta,
    vehicles_to_meta,
)

LOGGER = logging.getLogger("expand_fitment_crossref")

_LKQ_PID_RE = re.compile(r"/Product/[^/]+/(\d+)", re.I)

# Popular SK park — na discover (nie dump celej LKQ taxonómie)
_DISCOVER_MODELS: Dict[str, List[Tuple[str, str, str]]] = {
    # make -> [(model needle, engine, category)]
    # category = hint pre lkq_find (stierace/filtre/brzdy…)
    "Škoda": [
        ("octavia iii", "1.6 TDI", "stierace"),
        ("octavia iii", "2.0 TDI", "chladenie"),
        ("fabia iii", "1.0 TSI", "stierace"),
        ("superb iii", "2.0 TDI", "stierace"),
        ("superb iii", "2.0 TDI", "chladenie"),
        ("karoq", "1.6 TDI", "filtre"),
        ("kodiaq", "2.0 TDI", "filtre"),
        ("rapid", "1.6 TDI", "filtre"),
    ],
    "Volkswagen": [
        ("golf vii", "1.6 TDI", "stierace"),
        ("golf vii", "2.0 TDI", "chladenie"),
        ("polo vi", "1.0 TSI", "stierace"),
        ("passat", "2.0 TDI", "stierace"),
        ("passat", "2.0 TDI", "chladenie"),
        ("tiguan", "2.0 TDI", "filtre"),
        ("touran", "1.6 TDI", "filtre"),
        ("touareg", "3.0 TDI", "filtre"),
    ],
    "Audi": [
        ("a4 b8", "2.0 TDI", "stierace"),
        ("a3 8v", "2.0 TDI", "stierace"),
        ("a3 8v", "2.0 TDI", "chladenie"),
        ("a4 b9", "2.0 TDI", "filtre"),
    ],
    "Seat": [
        ("leon iii", "1.6 TDI", "stierace"),
        ("leon iii", "2.0 TDI", "chladenie"),
        ("ibiza", "1.2 TSI", "stierace"),
        ("ateca", "1.6 TDI", "filtre"),
    ],
    "Mercedes-Benz": [
        ("c-class (w205)", "C 220", "stierace"),
    ],
    "Renault": [
        ("megane", "1.5 dCi", "filtre"),
    ],
    "Ford": [
        ("focus", "1.6 TDCi", "filtre"),
        ("focus", "1.5", "kotúčová"),
        ("mondeo", "2.0 TDCi", "kotúčová"),
        ("kuga", "2.0 TDCi", "kotúčová"),
        ("fiesta", "1.0", "kotúčová"),
    ],
    "Volvo": [
        ("v40", "2.0", "kotúčová"),
        ("v60", "2.0", "kotúčová"),
        ("xc60", "2.0", "kotúčová"),
        ("s60", "2.0", "kotúčová"),
    ],
    "Opel": [
        ("astra", "1.6", "kotúčová"),
        ("insignia", "2.0", "kotúčová"),
        ("corsa", "1.4", "kotúčová"),
    ],
    "Mazda": [
        ("3", "2.0", "kotúčová"),
        ("6", "2.0", "kotúčová"),
        ("cx-5", "2.0", "kotúčová"),
    ],
    "Hyundai": [
        ("i30", "1.6 CRDi", "filtre"),
        ("coupe", "2.0", "filtre"),
    ],
}


def _has_paired_vehicles(product: dict) -> bool:
    """True ak produkt už má neprázdny `_sg_fit_vehicles` JSON."""
    raw = _meta(product, "_sg_fit_vehicles").strip()
    if not raw or raw in ("[]", "null", "None"):
        return False
    if raw.startswith("["):
        return True
    return bool(raw)


def _meta(product: dict, key: str) -> str:
    for m in product.get("meta_data") or []:
        if m.get("key") == key:
            return str(m.get("value") or "")
    return ""


def _load_products(
    wc: WooCommerceClient,
    *,
    limit: int,
    category_slug: str,
    sku: str,
    status: str,
    order: str = "desc",
    only_missing: bool = False,
    scan_cap: int = 200,
) -> List[dict]:
    if sku:
        resp = wc.api.get("products", params={"sku": sku, "per_page": 5})
        data = resp.json()
        return data if isinstance(data, list) else []

    cats = wc.api.get(
        "products/categories",
        params={"slug": category_slug, "per_page": 5},
    ).json()
    if not cats:
        raise SystemExit(f"Kategória slug={category_slug!r} nenájdená")
    cat_id = int(cats[0]["id"])

    out: List[dict] = []
    page = 1
    scanned = 0
    while len(out) < limit and scanned < scan_cap:
        batch = min(50, scan_cap - scanned)
        resp = wc.api.get(
            "products",
            params={
                "per_page": batch,
                "page": page,
                "status": status,
                "category": cat_id,
                "orderby": "id",
                "order": order if order in ("asc", "desc") else "desc",
            },
        )
        products = resp.json()
        if not isinstance(products, list) or not products:
            break
        scanned += len(products)
        for p in products:
            if only_missing and _has_paired_vehicles(p):
                continue
            # Chýbajúci JSON: ber aj produkty s OE / supplier_url (aj bez make),
            # inak BOSCH s plným `_sg_oe_numbers` a prázdnym fitmentom padne mimo.
            if only_missing and not (
                _meta(p, "_sg_fit_sg_id")
                or _meta(p, "_sg_fit_make")
                or _meta(p, "_sg_fit_model")
                or _meta(p, "_sg_oe_numbers")
                or _meta(p, "supplier_url")
                or _meta(p, "_sg_supplier_url")
            ):
                continue
            out.append(p)
            if len(out) >= limit:
                break
        if len(products) < batch:
            break
        page += 1
        time.sleep(0.05)
    return out[:limit]


def _fetch_lkq_codes(supplier_url: str) -> Tuple[List[str], List[str]]:
    m = _LKQ_PID_RE.search(supplier_url or "")
    if not m:
        return [], []
    user = os.environ.get("LKQ_USER") or os.environ.get("LKQ_USERNAME")
    password = os.environ.get("LKQ_PASSWORD")
    if not user or not password:
        return [], []
    try:
        from suppliers.lkq import LkqSupplier

        if not hasattr(_fetch_lkq_codes, "_sup"):
            sup = LkqSupplier(credentials={"user": user, "password": password})
            if not sup.ensure_login():
                return [], []
            _fetch_lkq_codes._sup = sup  # type: ignore[attr-defined]
        sup = _fetch_lkq_codes._sup  # type: ignore[attr-defined]
        resp = sup.http.post(
            f"{sup.base_url}/Product/Codes/{m.group(1)}",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
        )
        payload = resp.json()
        return extract_oe_from_lkq_codes(payload), extract_oem_makes_from_lkq_codes(
            payload
        )
    except Exception as exc:
        LOGGER.debug("Codes fail: %s", exc)
        return [], []


def _sku_in_catalog(category_url: str, sku: str) -> bool:
    """True ak LKQ category listing obsahuje SKU (overený výskyt).

    Prejde až ~12 strán ProductList (~240 ks) — prvých 40 často nestačí
    pri veľkých kategóriách (brzdy).
    """
    try:
        from suppliers.lkq import LkqSupplier
        from urllib.parse import urljoin

        user = os.environ.get("LKQ_USER") or os.environ.get("LKQ_USERNAME")
        password = os.environ.get("LKQ_PASSWORD")
        if not user or not password:
            return False
        if not hasattr(_sku_in_catalog, "_sup"):
            sup = LkqSupplier(credentials={"user": user, "password": password})
            if not sup.ensure_login():
                return False
            _sku_in_catalog._sup = sup  # type: ignore[attr-defined]
        sup = _sku_in_catalog._sup  # type: ignore[attr-defined]
        want = normalize_oe_code(sku).replace(" ", "")
        if not want:
            return False
        try:
            sup.http.get(category_url)
        except Exception:
            return False
        for page in range(1, 13):
            try:
                resp = sup.http.post(
                    f"{sup.base_url}/ProductList/Items/AllInOne/{page}",
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": category_url,
                    },
                )
                data = resp.json() or {}
            except Exception:
                break
            items = data.get("Items") or []
            if not items:
                break
            for item in items:
                code = normalize_oe_code(str(item.get("Code") or "")).replace(" ", "")
                link = str(item.get("Link") or "")
                if code == want or (want and want in normalize_oe_code(link).replace(" ", "")):
                    return True
            paging = data.get("Paging") or {}
            total_pages = paging.get("Total")
            if total_pages and page >= int(total_pages):
                break
            time.sleep(0.12)
        return False
    except Exception as exc:
        LOGGER.debug("catalog check fail: %s", exc)
        return False


def _discover_pairs_for_makes(
    oem_makes: List[str], sku: str, category_hint: str = "stierace"
) -> List[Dict[str, Any]]:
    """Pre každú OE značku over 1–N modelov cez Catalog URL + SKU listing."""
    from fitment import normalize_vehicle_pair, parse_fitment_from_catalog_slug
    import subprocess

    found: List[Dict[str, Any]] = []
    root = os.path.dirname(os.path.abspath(__file__))
    for make in oem_makes:
        jobs = _DISCOVER_MODELS.get(make) or []
        # fallback: prvý model z hierarchie
        if not jobs:
            mods = models_for_make(make)
            if mods:
                jobs = [(mods[0].lower(), "", category_hint)]
        # Až 5 modelov na značku; category_hint z CLI má prioritu pred job cat
        # ak CLI nie je generické (stierace default).
        for model_needle, engine, cat in jobs[:5]:
            use_cat = category_hint if category_hint not in ("stierace", "nahradne-diely", "") else (cat or category_hint)
            # Normalizuj SK diakritiku pre finder (kotúčová → kotucova)
            use_cat = (
                use_cat.replace("č", "c")
                .replace("ť", "t")
                .replace("ž", "z")
                .replace("ý", "y")
                .replace("á", "a")
                .replace("í", "i")
                .replace("é", "e")
                .replace("ú", "u")
                .replace("ô", "o")
            )
            cmd = [
                sys.executable,
                os.path.join(root, "lkq_find_vehicle_category.py"),
                "--make",
                make.split("-")[0] if make == "Mercedes-Benz" else make,
                "--model",
                model_needle,
                "--engine",
                engine or "1.0",
                "--category",
                use_cat or "filtre",
                "--exclude",
                "4x4,combi",
            ]
            if make == "Mercedes-Benz":
                cmd[cmd.index("--make") + 1] = "mercedes"
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=90, cwd=root
                )
            except Exception as exc:
                LOGGER.debug("find fail %s %s: %s", make, model_needle, exc)
                continue
            url = ""
            for line in (proc.stdout or "").splitlines():
                if line.startswith("http"):
                    url = line.strip()
                    break
            if not url:
                continue
            if not _sku_in_catalog(url, sku):
                LOGGER.info(
                    "  skip %s / %s — SKU nie je v Catalog", make, model_needle
                )
                continue
            slug_fit = parse_fitment_from_catalog_slug(url)
            pair = normalize_vehicle_pair(
                make=slug_fit.get("make") or make,
                model=slug_fit.get("model") or "",
                engine=slug_fit.get("engine") or engine,
                year_from=slug_fit.get("year_from"),
                year_to=slug_fit.get("year_to"),
            )
            if pair:
                LOGGER.info(
                    "  OK discover %s %s %s",
                    pair["make"],
                    pair["model"],
                    pair.get("engine"),
                )
                found.append(pair)
            time.sleep(0.3)
    return found


def _repair_product(
    product: dict,
    *,
    discover: bool,
    strict_oem: bool,
    category_hint: str,
) -> Tuple[Optional[List[Dict[str, Any]]], List[str], List[str], str]:
    """Vráť (vehicles, oe, oe_makes, reason)."""
    from fitment import merge_vehicle_lists

    meta = product.get("meta_data") or []
    vehicles = vehicles_from_product_meta(meta)
    oes = [x for x in _meta(product, "_sg_oe_numbers").split("|") if x.strip()]
    reason = "normalized"

    raw_makes = [m for m in _meta(product, "_sg_fit_make").split("|") if m.strip()]
    raw_models = [m for m in _meta(product, "_sg_fit_model").split("|") if m.strip()]
    messy = len(raw_makes) > len({v["make"] for v in vehicles}) + 1 or (
        len(raw_makes) >= 3 and len(raw_makes) > len(raw_models)
    )
    if messy:
        reason = "repaired"

    oem_makes: List[str] = [
        m for m in _meta(product, "_sg_oe_makes").split("|") if m.strip()
    ]
    sup = _meta(product, "supplier_url") or _meta(product, "_sg_supplier_url")
    need_codes = strict_oem or discover or not oes or not oem_makes
    if need_codes and "lkq" in (sup or "").lower():
        new_oe, fetched_makes = _fetch_lkq_codes(sup)
        if new_oe:
            oes = merge_oe_numbers(oes, new_oe)
        if fetched_makes:
            # union OE značiek (Ford|Volvo…) pre PDP
            seen = set(oem_makes)
            for m in fetched_makes:
                if m not in seen:
                    oem_makes.append(m)
                    seen.add(m)
        time.sleep(0.15)

    # Prísne: nechaj len páry, ktorých značka je v LKQ Oem.Manufacturer
    # (odstráni „požičané“ modely zo OE súrodencov inej značky).
    if strict_oem and oem_makes:
        allowed = set(oem_makes)
        filtered = [v for v in vehicles if v.get("make") in allowed]
        if filtered and len(filtered) < len(vehicles):
            vehicles = filtered
            reason = "strict_oem"
        elif not filtered and vehicles:
            # nič z Codes — nechaj 1 primárny pár (prvý sg_id)
            vehicles = vehicles[:1]
            reason = "strict_oem_primary"

    if discover and oem_makes:
        have_makes = {v["make"] for v in vehicles}
        missing = [m for m in oem_makes if m not in have_makes]
        if missing:
            sku = str(product.get("sku") or "")
            LOGGER.info("ID %s discover missing makes %s", product["id"], missing)
            extra = _discover_pairs_for_makes(missing, sku, category_hint)
            if extra:
                vehicles = merge_vehicle_lists(vehicles, extra)
                reason = "discover"

    # Jedna OE značka + prázdny fitment → skús default model SK park / Catalog slug
    if not vehicles and len(oem_makes) == 1:
        from fitment import normalize_vehicle_pair, parse_fitment_from_catalog_slug

        mk = oem_makes[0]
        slug_pair = {}
        if "lkq" in (sup or "").lower() and "Catalog" in (sup or ""):
            slug_pair = parse_fitment_from_catalog_slug(sup)
        model_guess = (slug_pair.get("model") or "").strip()
        if not model_guess:
            model_guess = {
                "Mazda": "3",
                "Nissan": "Qashqai",
                "Volvo": "V40",
                "Honda": "Civic",
                "Dacia": "Duster",
                "Fiat": "500",
                "Citroën": "C3",
                "Citroen": "C3",
            }.get(mk, "")
        if model_guess:
            pair = normalize_vehicle_pair(
                make=mk,
                model=model_guess,
                year_from=slug_pair.get("year_from"),
                year_to=slug_pair.get("year_to"),
            )
            if pair.get("sg_id"):
                vehicles = [pair]
                reason = "oe_make_default"

    if not vehicles:
        return None, oes, oem_makes, "no_pairs"

    return vehicles, oes, oem_makes, reason


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sku", default="")
    ap.add_argument("--category-slug", default="stierace")
    ap.add_argument("--status", default="publish", choices=("publish", "draft"))
    ap.add_argument(
        "--repair",
        action="store_true",
        default=True,
        help="vyčisti páry zo sg_id / JSON (default)",
    )
    ap.add_argument(
        "--discover",
        action="store_true",
        help="doplň chýbajúce OE značky cez overený LKQ Catalog výskyt SKU",
    )
    ap.add_argument(
        "--strict-oem",
        action="store_true",
        help="nechaj len páry značiek z LKQ Oem.Manufacturer (drop požičaných modelov)",
    )
    ap.add_argument(
        "--only-missing",
        action="store_true",
        help="len produkty bez _sg_fit_vehicles JSON",
    )
    ap.add_argument(
        "--order",
        default="asc",
        choices=("asc", "desc"),
        help="poradie WC id (asc = staršie najprv — typicky chýbajúci JSON)",
    )
    ap.add_argument(
        "--scan-cap",
        type=int,
        default=0,
        help="max produktov na scan (0 = auto: 200, pri --only-missing 500)",
    )
    args = ap.parse_args()
    if args.limit > 50:
        args.limit = 50
    scan_cap = args.scan_cap
    if scan_cap <= 0:
        scan_cap = 500 if args.only_missing else 200
    scan_cap = max(scan_cap, args.limit)

    config = Config()
    missing = [m for m in config.validate(require_anthropic=False) if m.startswith("WC_")]
    if missing:
        LOGGER.error("Chýbajú env: %s", ", ".join(missing))
        return 1

    wc = WooCommerceClient(config)
    products = _load_products(
        wc,
        limit=args.limit,
        category_slug=args.category_slug,
        sku=args.sku,
        status=args.status,
        order=args.order,
        only_missing=args.only_missing,
        scan_cap=scan_cap,
    )
    if not products:
        LOGGER.error("Žiadne produkty")
        return 1

    LOGGER.info(
        "Produktov %d repair=%s strict_oem=%s discover=%s only_missing=%s order=%s dry=%s",
        len(products),
        args.repair,
        args.strict_oem,
        args.discover,
        args.only_missing,
        args.order,
        args.dry_run,
    )

    fixed = skipped = cleared = 0
    for product in products:
        before_makes = _meta(product, "_sg_fit_make")
        vehicles, oes, oe_makes, reason = _repair_product(
            product,
            discover=args.discover,
            strict_oem=args.strict_oem,
            category_hint=args.category_slug if args.category_slug != "nahradne-diely" else "stierace",
        )
        if not vehicles:
            # Orphan make BEZ modelu — vymaž len ak make bol nastavený (žiadna hala-bála).
            # Produkty len s OE (bez make) nechaj — heijunka/import ich doplní neskôr.
            if before_makes.strip() and not args.dry_run:
                meta_clear = [
                    {"key": "_sg_fit_make", "value": ""},
                    {"key": "_sg_fit_model", "value": ""},
                    {"key": "_sg_fit_engine", "value": ""},
                    {"key": "_sg_fit_year_from", "value": ""},
                    {"key": "_sg_fit_year_to", "value": ""},
                    {"key": "_sg_fit_sg_id", "value": ""},
                    {"key": "_sg_fit_vehicles", "value": ""},
                ]
                if oes:
                    meta_clear.append({"key": "_sg_oe_numbers", "value": "|".join(oes)})
                if oe_makes:
                    meta_clear.append(
                        {"key": "_sg_oe_makes", "value": "|".join(oe_makes)}
                    )
                LOGGER.info(
                    "ID %s %s → CLEAR orphan make=%r OE=%d makes=%s [no_pairs]",
                    product["id"],
                    (product.get("sku") or "")[:28],
                    before_makes,
                    len(oes),
                    "|".join(oe_makes[:6]),
                )
                wc.api.put(f"products/{product['id']}", {"meta_data": meta_clear})
                cleared += 1
                time.sleep(0.2)
            else:
                skipped += 1
            continue
        new_meta = vehicles_to_meta(
            vehicles, oe_numbers=oes or None, oe_makes=oe_makes or None
        )
        after_makes = ""
        after_models = ""
        for row in new_meta:
            if row["key"] == "_sg_fit_make":
                after_makes = row["value"]
            if row["key"] == "_sg_fit_model":
                after_models = row["value"]

        if before_makes == after_makes and _has_paired_vehicles(product):
            # už čisté
            if not args.discover:
                skipped += 1
                continue

        LOGGER.info(
            "ID %s %s → %s || %s (%d párov) [%s]",
            product["id"],
            (product.get("sku") or "")[:28],
            after_makes,
            after_models,
            len(vehicles),
            reason,
        )
        if args.dry_run:
            fixed += 1
            continue
        wc.api.put(f"products/{product['id']}", {"meta_data": new_meta})
        fixed += 1
        time.sleep(0.2)

    LOGGER.info(
        "Hotovo. fixed=%s cleared=%s skipped=%s dry_run=%s",
        fixed,
        cleared,
        skipped,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
