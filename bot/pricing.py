#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
CENOTVORBA SmartGaráž — z dodávateľskej (B2B) ceny → predajná MOC
===============================================================================

DÔLEŽITÉ — SmartGaráž NIE JE platca DPH na Slovensku:
  - Nakupujeme od dodávateľov ZA CENU S DPH (tú si nevieme odpočítať/vrátiť),
    takže táto cena JE naša reálna nákupná cena, s ktorou sa počíta ďalej.
  - Zákazníkovi NEPRIDÁVAME žiadne ďalšie DPH navrch (nie sme platcovia) —
    predajná cena = nákupná (s DPH, ako bola zaplatená) + marža. Bodka.
  - Preto MUSÍ byť `COST_HAS_VAT=1` (predvolené nižšie), inak by sa DPH
    pripočítalo K nákupnej cene ešte raz (dvojité DPH → nafúknuté ceny).

Vzorec:
  predajná = (nákupná_s_DPH + doprava_na_ks) × (1 + MARGIN_PERCENT/100)
  zaokrúhlenie na psychologickú cenu (.90 / .99 / 0.05)

Env (.env):
  MARGIN_PERCENT=35          # základná marža %
  OIL_MARGIN_PERCENT=20      # MINIMUM marža pre oleje (motor/prevodovka/ATF…)
  SHIPPING_FLAT=0.60         # fixná doprava na kus (EUR)
  PRICE_ENDING=0.90          # psychologické koncovky (0.90 / 0.99 / 0)
  MIN_MARGIN_EUR=1.50        # minimálny zisk v €
  COST_HAS_VAT=1             # MUSÍ byť 1 — nakupujeme s DPH, nie sme platcovia
  VAT_RATE=0.00              # nepoužíva sa, keď COST_HAS_VAT=1 (necháme 0 pre istotu)

PRO-TEC (pro-tec-slovakia.sk):
  - Predaj = **verejný maloobchod** (retail_hint, napr. Engine Clean 8,49 €),
    nie B2B (5,94 €) a nie nafúknuté MOC cez min. objednávku.
  - `PROTEC_MIN_ORDER_EUR=15` je **prevádzkové** pravidlo (objednávaj viac
    kusov / manip. poplatok) — **NE** floor do každej kusovej MOC.
  - `PROTEC_APPLY_MIN_ORDER_TO_MOC=1` zapne starý floor (default OFF).

Maloobchod (Szekop / PRO-TEC / LKQ / …):
  PRO-TEC / Metabond / LKQ / JLM: pri retail_hint ≥ cost predávame **presne** ich MOC
  (LKQ: nákup = UserPrice/Vaša cena s DPH, predaj = RetailPrice/MOC s DPH).
  JLM: feed PRICE_VAT = MOC; cost ≈ MOC × JLM_COST_RATIO (default 0.80) kým nie je VO.
  Szekop/ostatní: max(marža, retail_hint); oleje ≥ OIL_MARGIN_PERCENT.

MOPAR / PTAK AUTO (ptak.auto.pl) — cenník je NETTO EUR:
  MOPAR_VAT_FACTOR=1.23      # PL DPH 23 % → reálny náklad (nie sme platcovia DPH)
  MOPAR_MARGIN_PERCENT=30    # marža podľa majiteľa (netto × 1.23 × 1.30)
  MOPAR_SHIPPING_FLAT=0      # doprava NIE v cene produktu — rieši cart fee pri 1 ks
  (balík do SK max ~18 € → WooCommerce fee `sg-mopar-single-fee`)
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Optional

from suppliers.base import OfferSnapshot


@dataclass
class PricingResult:
    cost_price: float
    shipping: float
    margin_percent: float
    retail_price: float
    profit_eur: float
    delivery_days: Optional[int] = None
    delivery_text: str = ""
    stock_qty: Optional[float] = None
    stock_status: str = "instock"


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def round_psychological(price: float, ending: float = 0.90) -> float:
    """Zaokrúhli nahor na X.90 / X.99 / najbližších 5 centov."""
    if ending <= 0:
        return round(price, 2)
    whole = math.floor(price)
    candidate = whole + ending
    if candidate < price:
        candidate = whole + 1 + ending
    return round(candidate, 2)


def is_oil_product(
    name: str = "",
    brand: str = "",
    sku: str = "",
    category: str = "",
    source: str = "",
) -> bool:
    """Motorové / prevodové oleje a príbuzné náplne — min. OIL_MARGIN_PERCENT.

    Filtre a náradie na olej NIE sú olej (vynechané).
    """
    blob = f"{name} {brand} {sku} {category} {source}".lower()
    if not blob.strip():
        return False
    if any(
        x in blob
        for x in (
            "olejový filter",
            "olejovy filter",
            "oil filter",
            "filter oleja",
            "olejový kľúč",
            "olejovy kluc",
            "filter wrench",
            "oil filter tool",
        )
    ):
        return False
    if re.search(
        r"\b(0w[-\s]?(?:8|12|16|20|30|40)|5w[-\s]?(?:20|30|40|50)|"
        r"10w[-\s]?(?:30|40|50|60)|15w[-\s]?(?:40|50)|20w[-\s]?(?:50|60))\b",
        blob,
    ):
        return True
    markers = (
        "motorový olej",
        "motorovy olej",
        "engine oil",
        "motor oil",
        "prevodov",
        "gear oil",
        "atf",
        "cvtf",
        "hydraulický olej",
        "hydraulicky olej",
        "hydraulic oil",
        "posilňovač riadenia",
        "posilnovac riadenia",
        "motorové oleje",
        "motorove oleje",
        "prevodové oleje",
        "prevodove oleje",
    )
    if any(m in blob for m in markers):
        return True
    # generický „olej“ / „oil“ — ale nie autochémia typu „oil cleaner“ bez SAE
    if re.search(r"(?<![a-z])(olej|oil)(?![a-z])", blob):
        if any(
            x in blob
            for x in (
                "cleaner",
                "čistič",
                "cistic",
                "flush",
                "additive",
                "aditív",
                "aditiv",
                "booster",
            )
        ):
            return False
        return True
    return False


def calculate_retail_mopar(net_cost: float) -> PricingResult:
    """
    PTAK MOPAR cenník = NETTO EUR.
    Majiteľ: predaj ≈ netto × 1.23 × 1.30 (kalkulačka 8.63 → 13.80).
    Dopravu na 1 ks rieši Woo fee, nie SHIPPING_FLAT v produkte.
    """
    vat_factor = _f("MOPAR_VAT_FACTOR", 1.23)
    margin = _f("MOPAR_MARGIN_PERCENT", 30.0)
    shipping = _f("MOPAR_SHIPPING_FLAT", 0.0)
    ending = _f("PRICE_ENDING", 0.90)
    min_profit = _f("MIN_MARGIN_EUR", 1.50)

    cost_base = float(net_cost) * vat_factor
    base = cost_base + shipping
    raw = base * (1.0 + margin / 100.0)
    if raw - cost_base < min_profit:
        raw = cost_base + min_profit
    retail = round_psychological(raw, ending)
    profit = round(retail - cost_base - shipping, 2)
    return PricingResult(
        cost_price=round(float(net_cost), 2),
        shipping=round(shipping, 2),
        margin_percent=margin,
        retail_price=retail,
        profit_eur=profit,
    )


def is_mopar_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower()
    return any(k in blob for k in ("mopar", "maxpro", "ptak"))


def is_protec_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower().replace("_", "-")
    return any(k in blob for k in ("pro-tec", "protec", "pro tec"))


def is_metabond_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower()
    return "metabond" in blob


def is_lkq_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower()
    return "lkq" in blob or "auto kelly" in blob or "autokelly" in blob


def is_jlm_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower().replace("_", "-")
    return "jlm" in blob or "jlmaditiva" in blob


def is_wurth_source(source: str = "", brand: str = "", name: str = "") -> bool:
    blob = f"{source} {brand} {name}".lower()
    return "wurth" in blob or "würth" in blob


def wurth_price_floor(net_cost: float) -> float:
    """Würth: cena v B2B e-shope je NETTO (bez DPH). Lavro s.r.o. nie je platca DPH.

    Pri objednávke do 100 € netto Würth účtuje poplatok 6,80 € (VOP čl. 2.6/5.12)
    → minimálna predajná = (netto + 6,80) × 1,23 + MIN_MARGIN_EUR, zaokrúhlené na ,90.
    (Košík Samuela: 11,90 + 6,80 + DPH 4,30 = 23,00 → floor 24,90.)
    """
    try:
        net = float(net_cost)
    except (TypeError, ValueError):
        return 0.0
    if net <= 0:
        return 0.0
    fee = _f("WURTH_ORDER_FEE", 6.80) if net < _f("WURTH_FREE_ORDER_NET", 100.0) else 0.0
    vat = _f("WURTH_VAT_FACTOR", 1.23)
    raw = (net + fee) * vat + _f("MIN_MARGIN_EUR", 1.50)
    return round_psychological(raw, _f("PRICE_ENDING", 0.90))


def protec_effective_unit_cost(unit_cost: float) -> float:
    """Ops helper: min. objednávka PRO-TEC (default 15 €).

    Defaultne sa **nepoužíva** na katalógovú MOC (Engine Clean by skočil
    na 21,90 € namiesto maloobchodu 8,49 €). Zapni len
    `PROTEC_APPLY_MIN_ORDER_TO_MOC=1`.
    """
    min_order = _f("PROTEC_MIN_ORDER_EUR", 15.0)
    unit = float(unit_cost or 0)
    if min_order <= 0 or unit <= 0:
        return unit
    return max(unit, min_order)


def _protec_apply_min_order_to_moc() -> bool:
    return os.getenv("PROTEC_APPLY_MIN_ORDER_TO_MOC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def calculate_retail(
    cost: float,
    margin_percent: Optional[float] = None,
    shipping_flat: Optional[float] = None,
    price_ending: Optional[float] = None,
    min_margin_eur: Optional[float] = None,
    vat_rate: Optional[float] = None,
    cost_has_vat: Optional[bool] = None,
    supplier: Optional[str] = None,
    name: str = "",
    brand: str = "",
    sku: str = "",
    category: str = "",
    retail_hint: Optional[float] = None,
) -> PricingResult:
    if supplier and is_mopar_source(source=supplier, brand=brand, name=name):
        return calculate_retail_mopar(cost)
    margin = margin_percent if margin_percent is not None else _f("MARGIN_PERCENT", 35.0)
    shipping = shipping_flat if shipping_flat is not None else _f("SHIPPING_FLAT", 0.60)
    ending = price_ending if price_ending is not None else _f("PRICE_ENDING", 0.90)
    min_profit = min_margin_eur if min_margin_eur is not None else _f("MIN_MARGIN_EUR", 1.50)
    vat = vat_rate if vat_rate is not None else _f("VAT_RATE", 0.0)
    # Predvolené 1: SmartGaráž nie je platca DPH, nakupuje s DPH a tú si nevie
    # odpočítať — nákupná cena "s DPH, ako bola zaplatená" JE reálny náklad.
    has_vat = cost_has_vat if cost_has_vat is not None else os.getenv("COST_HAS_VAT", "1") == "1"

    protec = bool(supplier and is_protec_source(source=supplier, brand=brand, name=name))
    metabond = bool(supplier and is_metabond_source(source=supplier, brand=brand, name=name))
    lkq = bool(supplier and is_lkq_source(source=supplier, brand=brand, name=name))
    jlm = bool(supplier and is_jlm_source(source=supplier, brand=brand, name=name))
    hint = 0.0
    try:
        if retail_hint not in (None, "", 0, 0.0):
            hint = float(retail_hint)
    except (TypeError, ValueError):
        hint = 0.0

    # PRO-TEC / Metabond / LKQ / JLM zarovno oficiálneho maloobchodu (nie marža × MOC).
    # LKQ: cost = Vaša cena s DPH (UserPrice), hint = Maloobchod s DPH (RetailPrice).
    # JLM: cost ≈ MOC × JLM_COST_RATIO (odhad), hint = PRICE_VAT z feedu.
    if (protec or metabond or lkq or jlm) and hint > 0 and hint + 1e-9 >= float(cost):
        profit = round(hint - float(cost) - shipping, 2)
        m_pct = round((hint / float(cost) - 1.0) * 100.0, 1) if float(cost) > 0 else 0.0
        return PricingResult(
            cost_price=round(float(cost), 2),
            shipping=round(shipping, 2),
            margin_percent=m_pct,
            retail_price=round(hint, 2),
            profit_eur=profit,
        )

    # Oleje: minimum OIL_MARGIN_PERCENT (default 20 %), aj keď globálna marža klesne
    # alebo retail-pass-through by dal ~0 %.
    if is_oil_product(name=name, brand=brand, sku=sku, category=category, source=supplier or ""):
        oil_floor = _f("OIL_MARGIN_PERCENT", 20.0)
        margin = max(margin, oil_floor)

    unit_cost = float(cost)
    # Voliteľný starý floor (default OFF) — min. objednávka ≠ kusová MOC.
    if protec and _protec_apply_min_order_to_moc():
        unit_cost = protec_effective_unit_cost(unit_cost)

    # Iba ak by nákupná bola preukázateľne BEZ DPH (COST_HAS_VAT=0), pridaj DPH
    # do nákladov pred maržou. Pre bežný prípad (COST_HAS_VAT=1) sa nič
    # nepripočítava — predávame bez DPH, nie sme platcovia.
    cost_base = unit_cost if has_vat else unit_cost * (1.0 + vat)
    base = cost_base + shipping
    raw = base * (1.0 + margin / 100.0)

    # Garantuj minimálny zisk
    if raw - cost_base < min_profit:
        raw = cost_base + min_profit

    retail = round_psychological(raw, ending)

    # Ostatní dodávatelia: ak MOC hint > naša marža — nepodliezaj.
    if not protec and not metabond and not lkq and not jlm and hint > retail and hint > cost_base:
        retail = round(hint, 2)

    # Würth: netto cena + poplatok 6,80 € (objednávka < 100 €) + DPH 23 % — nikdy pod to.
    if supplier and is_wurth_source(source=supplier, brand=brand, name=name):
        retail = max(retail, wurth_price_floor(float(cost)))

    profit = round(retail - cost_base - shipping, 2)

    return PricingResult(
        cost_price=round(float(cost), 2),  # meta = skutočná jednotková B2B
        shipping=round(shipping, 2),
        margin_percent=margin,
        retail_price=retail,
        profit_eur=profit,
    )


def price_from_offer(offer: OfferSnapshot) -> Optional[PricingResult]:
    cost = offer.cost_price
    if cost is None and offer.regular_price is not None:
        # Fallback: po logine regular = B2B
        cost = offer.regular_price
    if cost is None or cost <= 0:
        return None

    retail_hint = offer.retail_hint
    # Ak retail_hint ≈ cost (verejný Szekop), stále počítaj maržu — oleje majú
    # floor 20 %. Hint použije calculate_retail na dorovnanie NAHOR na MOC.
    result = calculate_retail(
        cost,
        supplier=offer.source or "",
        name=offer.name or "",
        sku=offer.sku or "",
        retail_hint=retail_hint,
    )

    result.delivery_days = offer.delivery_days
    result.delivery_text = offer.delivery_text or ""
    result.stock_qty = offer.stock_qty
    result.stock_status = offer.stock_status or ("instock" if (offer.stock_qty or 0) > 0 else "outofstock")

    return result
