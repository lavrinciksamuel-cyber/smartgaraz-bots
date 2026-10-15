#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jediný zdroj pravdy pre rozpoznávanie SKUTOČNEJ výrobnej značky produktu
(Mopar, Pennzoil, Mobil 1, ...) z názvu/popisu — nezávisle od toho, akého
DISTRIBÚTORA/dodávateľa (napr. PRO-TEC, LKQ, Würth) sme produkt kúpili.

Prečo je to dôležité:
    Niektorí dodávatelia (napr. PRO-TEC) len ĎALEJ PREDÁVAJÚ originálne
    Mopar/Pennzoil/Mobil oleje. Ak by sme im automaticky priradili značku
    distribútora ("PRO-TEC"), produkt "Mopar MaxPro SAE 0W20 208L" by sa
    tváril ako produkt značky PRO-TEC — nesprávne zobrazená značka AJ zlé
    vyhľadávacie dopyty pri hľadaní HD fotky (hľadá sa "PRO-TEC <kód>" miesto
    "Mopar <kód>", čo nikdy nenájde skutočnú fotku Mopar fľaše/suda).

Zoznam nižšie zoraď od NAJŠPECIFICKEJŠÍCH (dlhšie/presnejšie tokeny) po
najvšeobecnejšie, aby `detect_true_brand` vrátil napr. "Mobil 1", nie len
"Mobil", ak sa v texte nájde "Mobil 1 0W40 Supercar".
"""

from __future__ import annotations

import re
from typing import Optional

# Skutoční VÝROBCOVIA (nie distribútori) — v poradí špecifickosť najprv.
KNOWN_MANUFACTURER_BRANDS = [
    "Mobil 1",
    "Mopar",
    "Pennzoil",
    "Mobil",
    "Shell Rotella",
    "Shell",
    "Amalie",
    "Castrol",
    "Motul",
    "Liqui Moly",
    "Valvoline",
    "Fuchs",
    "Total",
    "Elf",
    "Bluechem",
    "Autoprofi",
    "Marvelous",
    "X-Chemie",
    "Koch-Chemie",
    "Koch Chemie",
    "JLM",
    "Proelite",
    "Metabond",
    # DFU = vlastná private-label značka majiteľa (Don't F*ck Up).
    # Zatiaľ sa NEVYRÁBA / NEZVEREJŇUJE na storefronte — len rozpoznanie
    # pri budúcom importe. Live exkluzíva na webe ostáva KENT.
    "DFU",
    "Kent",
    "Würth",
    "Wurth",
    "Starline",
    "Motul",
    "Zic",
    "Gulf",
    "Repsol",
    "Petronas",
    "MPM",
    "Volvo",
    "XT",
    "MANN",
    "Bosch",
    "Brembo",
    # Žiarovky / osvetlenie (LKQ sortiment)
    "Osram",
    "OSRAM",
    "Philips",
    "Hella",
    "HELLA",
    "ERA",
    "Tesla",
    # Sviečky / brzdy / chladenie
    "NGK",
    "Beru",
    "BERU",
    "ATE",
    "Glysantin",
    "Fridex",
    "Febi",
    "FEBI",
]

# Exkluzívne značky SmartGaráž.
# DFU = budúca private label (zatiaľ nie na webe). KENT = live exkluzívna distribúcia.
EXCLUSIVE_BRANDS = {"DFU", "KENT", "Kent"}

# Storefront / homepage chip / search suggestions: DFU ešte NEpromovať.
DFU_STOREFRONT_LIVE = False

# LavroDoc IP cloak: hlboké IP (pin/OE dump/strom) von až po majiteľovom unlock.
# Runtime override: env LAVRODOC_PUBLIC_UNLOCK=1 — pozri bot/lavrodoc_stealth.py.
LAVRODOC_PUBLIC_UNLOCK = False

# Používa sa na odmietnutie kandidátskej fotky z obrázkového vyhľadávania,
# ktorá vo svojom titulku/URL spomína INÚ (konkurenčnú) značku, než akú
# reálne hľadáme — bez tejto kontroly vie napr. "STARLINE 5W-30" dopyt
# vrátiť fotku "Castrol EDGE 5W-30" (populárnejší, vizuálne podobný
# produkt, ktorý sa v obrázkovom vyhľadávaní umiestni vyššie).
def conflicts_with_other_brand(text: str, expected_brand: str) -> bool:
    if not expected_brand or not text:
        return False
    low = (text or "").lower()
    expected_low = expected_brand.lower()
    for brand in KNOWN_MANUFACTURER_BRANDS:
        brand_low = brand.lower()
        if brand_low == expected_low or brand_low in expected_low or expected_low in brand_low:
            continue
        if re.search(r"\b" + re.escape(brand_low) + r"\b", low):
            return True
    return False

# Naši DISTRIBÚTORI/dodávatelia — nikdy ich nepoužívať ako "brand" tag, ak sa
# v názve/popise nájde konkrétnejšia výrobná značka vyššie.
DISTRIBUTOR_BRANDS = {"PRO-TEC", "LKQ", "Szekop"}

# Ad ADITIV (AdBlue anti-crystallization) — na fľaši je PRO-TEC logo / P0xxx SKU.
# Shoptet manufacturer pole na pro-tec-slovakia.sk chybne uvádza "X-Chemie".
AD_ADITIV_RE = re.compile(
    r"\bad\s*aditiv\b|\baditívum do adblue\b|\baditivum do adblue\b|"
    r"\badblue\b.*\banti[-\s]?crystall|\banti[-\s]?zhluk",
    re.I,
)
AD_ADITIV_SKU_RE = re.compile(r"^P02\d{2}$", re.I)


def detect_true_brand(name: str, description: str = "", fallback: str = "", sku: str = "") -> str:
    """Vráti najšpecifickejšiu skutočnú výrobnú značku nájdenú v `name`
    (prioritne) alebo `description`. Ak sa nič nenájde, vráti `fallback`
    (typicky pôvodné `raw_data.get("brand")` z dodávateľského crawlera)."""
    blob = f"{name or ''} {description or ''} {fallback or ''}"
    if AD_ADITIV_RE.search(blob) or AD_ADITIV_SKU_RE.match((sku or "").strip()):
        return "PRO-TEC"
    haystacks = [name or "", description or ""]
    for haystack in haystacks:
        low = haystack.lower()
        for brand in KNOWN_MANUFACTURER_BRANDS:
            pattern = r"\b" + re.escape(brand.lower()) + r"\b"
            if re.search(pattern, low):
                return brand
    return fallback or ""


def is_distributor_brand(brand: str) -> bool:
    return (brand or "").strip() in DISTRIBUTOR_BRANDS


def is_exclusive_brand(brand: str) -> bool:
    """True pre DFU (private label) a KENT (exkluzívna distribúcia)."""
    b = (brand or "").strip()
    if not b:
        return False
    if b in EXCLUSIVE_BRANDS:
        return True
    return b.upper() in {x.upper() for x in EXCLUSIVE_BRANDS}


def resolve_display_brand(raw_data: dict) -> str:
    """Vysoko-úrovňová pomôcka pre ai_engine/suppliers: ak dodávateľ priradil
    len svoju distribútorskú značku, ale v názve/popise je rozpoznateľná
    skutočná výrobná značka, uprednostní ju."""
    current = str(raw_data.get("brand") or "").strip()
    name = str(raw_data.get("name") or "")
    description = str(raw_data.get("description") or "")
    sku = str(raw_data.get("sku") or "")
    detected = detect_true_brand(name, description, fallback="", sku=sku)
    if detected:
        return detected
    return current


def extract_pack_size(text: str) -> Optional[str]:
    """Extrahuje objem balenia (napr. '0,946L', '5L', '208L') z textu — užitočné
    na doplnenie presného vyhľadávacieho dopytu pre HD fotku (fľaša vs sud)."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,4}(?:[.,]\d+)?)\s*(l|liter|litrov|ml)\b", text.lower())
    if not m:
        return None
    value, unit = m.groups()
    return f"{value}{unit.upper() if unit == 'l' else unit}"
