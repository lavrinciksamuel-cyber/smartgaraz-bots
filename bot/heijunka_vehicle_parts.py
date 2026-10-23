#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heijunka import autodielov podľa vozidla (LKQ Catalog URL → WC).

Cieľ: postupne dostať drobné diely do evidencie — max --limit ks / job,
žiadne tisícky naraz. Stav fronty v bot/data/heijunka_vehicle_queue_state.json
(gitignored lokálne môže byť; report ide do bot/reports/).

Príklad:
  python bot/heijunka_vehicle_parts.py --jobs 3 --limit 12
  python bot/heijunka_vehicle_parts.py --dry-run --jobs 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("heijunka_vehicle")
STATE_PATH = ROOT / "data" / "heijunka_vehicle_queue_state.json"
REPORTS = ROOT / "reports"

# Trvalý skip (prežije --reset-failed). Prefix match na _job_key.
PERMANENT_SKIP_PREFIXES = (
    "audi|q7|3.0 TDI|",
)

def _expand_cats(
    cores: List[tuple],
    cats: tuple = ("filtre", "kotúčová"),
) -> List[Dict[str, str]]:
    """(make, model, engine, kw) × kategórie → job dicty."""
    out: List[Dict[str, str]] = []
    for make, model, engine, kw in cores:
        for cat in cats:
            out.append(
                {
                    "make": make,
                    "model": model,
                    "engine": engine,
                    "kw": kw,
                    "category": cat,
                }
            )
    return out


# Wave 3 — zvyšok hierarchie SK park (filtre + kotúče). Ide NA ZAČIATOK fronty.
# LKQ Garage needles (nie TecDoc ID); engine = typický SK park.
_WAVE3_ALL_CARS_CORE: List[tuple] = [
    # Audi (Q7 3.0 TDI trvalo skip — LKQ garage match stuck)
    ("audi", "q3", "2.0 TDI", ""),
    # BMW
    ("bmw", "1 (f40)", "118d", ""),
    ("bmw", "5 (g30)", "520d", ""),
    ("bmw", "x1", "xDrive20d", ""),
    ("bmw", "x3", "xDrive20d", ""),
    ("bmw", "x5", "xDrive30d", ""),
    # Mercedes
    ("mercedes-benz", "a-class", "180", ""),
    ("mercedes-benz", "e-class", "220", ""),
    ("mercedes-benz", "gla", "200", ""),
    ("mercedes-benz", "glc", "220", ""),
    ("mercedes-benz", "vito", "114", ""),
    # Ford
    ("ford", "fiesta", "1.0", ""),
    ("ford", "mondeo", "2.0", ""),
    ("ford", "kuga", "1.5", ""),
    ("ford", "puma", "1.0", ""),
    ("ford", "transit", "2.0", ""),
    # Opel
    ("opel", "corsa", "1.2", ""),
    ("opel", "insignia", "1.6", ""),
    ("opel", "mokka", "1.2", ""),
    ("opel", "zafira", "1.6", ""),
    ("opel", "vivaro", "1.6", ""),
    # Peugeot
    ("peugeot", "208", "1.2", ""),
    ("peugeot", "2008", "1.5", ""),
    ("peugeot", "3008", "1.5", ""),
    ("peugeot", "508", "1.5", ""),
    ("peugeot", "partner", "1.5", ""),
    # Renault
    ("renault", "clio", "1.5", ""),
    ("renault", "captur", "1.5", ""),
    ("renault", "kadjar", "1.5", ""),
    ("renault", "scenic", "1.5", ""),
    ("renault", "trafic", "1.6", ""),
    # Hyundai
    ("hyundai", "i20", "1.2", ""),
    ("hyundai", "tucson", "1.6", ""),
    ("hyundai", "santa fe", "2.2", ""),
    ("hyundai", "ix35", "2.0", ""),
    ("hyundai", "kona", "1.0", ""),
    # Kia
    ("kia", "sportage", "1.6", ""),
    ("kia", "rio", "1.2", ""),
    ("kia", "sorento", "2.2", ""),
    ("kia", "stonic", "1.0", ""),
    ("kia", "niro", "hybrid", ""),
    # Toyota
    ("toyota", "yaris", "1.5", ""),
    ("toyota", "rav4", "2.0", ""),
    ("toyota", "auris", "1.6", ""),
    ("toyota", "avensis", "2.0", ""),
    ("toyota", "c-hr", "1.8", ""),
    # Seat
    ("seat", "arona", "1.0 TSI", ""),
    ("seat", "toledo", "1.2 TSI", ""),
    ("seat", "alhambra", "2.0 TDI", ""),
    # Fiat
    ("fiat", "500", "1.2", ""),
    ("fiat", "punto", "1.2", ""),
    ("fiat", "tipo", "1.4", ""),
    ("fiat", "ducato", "2.3", ""),
    # Honda
    ("honda", "jazz", "1.3", ""),
    ("honda", "cr-v", "1.6", ""),
    ("honda", "hr-v", "1.5", ""),
    # Dacia
    ("dacia", "sandero", "1.0", ""),
    ("dacia", "logan", "1.0", ""),
    ("dacia", "jogger", "1.0", ""),
    # Citroën
    ("citroen", "c4", "1.2", ""),
    ("citroen", "berlingo", "1.5", ""),
    ("citroen", "c5 aircross", "1.5", ""),
    # Mazda / Nissan / Volvo (v hierarchii)
    ("mazda", "3", "2.0", ""),
    ("mazda", "cx-5", "2.2", ""),
    ("mazda", "cx-30", "2.0", ""),
    ("nissan", "qashqai", "1.5", ""),
    ("nissan", "juke", "1.0", ""),
    ("nissan", "x-trail", "1.6", ""),
    ("volvo", "v40", "D2", ""),
    ("volvo", "xc40", "T3", ""),
    ("volvo", "xc60", "D4", ""),
    # Wave3+ SK volume 2026-08 — Cupra / Suzuki / MB C / VW T-Roc…
    ("cupra", "formentor", "1.5 TSI", ""),
    ("cupra", "leon", "2.0 TSI", ""),
    ("suzuki", "swift", "1.2", ""),
    ("suzuki", "vitara", "1.4", ""),
    ("suzuki", "s-cross", "1.4", ""),
    ("subaru", "xv", "2.0", ""),
    ("subaru", "forester", "2.0", ""),
    ("mitsubishi", "asx", "1.6", ""),
    ("mitsubishi", "outlander", "2.0", ""),
    ("mini", "countryman", "1.5", ""),
    ("mini", "cooper", "1.5", ""),
    ("mercedes-benz", "c-class", "220", ""),
    ("mercedes-benz", "glb", "200", ""),
    ("volkswagen", "t-roc", "1.5 TSI", ""),
    ("volkswagen", "t-cross", "1.0 TSI", ""),
    ("volkswagen", "arteon", "2.0 TDI", ""),
    ("volkswagen", "caddy", "2.0 TDI", ""),
    ("skoda", "enyaq", "electric", ""),
    ("skoda", "kamiq", "1.0 TSI", ""),
    ("skoda", "octavia iv", "2.0 TDI", "110kw"),
    ("audi", "a1", "1.0 TFSI", ""),
    ("audi", "q2", "1.5 TFSI", ""),
    ("audi", "a6", "2.0 TDI", ""),
    ("bmw", "2", "218d", ""),
    ("toyota", "prius", "1.8", ""),
    ("toyota", "yaris cross", "1.5", ""),
    ("toyota", "camry", "2.5", ""),
    ("hyundai", "i30", "1.6", ""),
    ("kia", "ceed", "1.6", ""),
    ("kia", "xceed", "1.0", ""),
    ("peugeot", "308", "1.5", ""),
    ("renault", "megane", "1.5", ""),
    ("dacia", "duster", "1.5", ""),
    ("honda", "civic", "1.5", ""),
    ("jeep", "renegade", "1.6", ""),
    ("alfa romeo", "giulia", "2.0", ""),
    ("lexus", "nx", "2.5", ""),
    ("land rover", "discovery sport", "2.0", ""),
    ("tesla", "model 3", "electric", ""),
    ("porsche", "macan", "2.0", ""),
    # Wave3++ SK volume 2026-08-04 — VAG + top park (filtre/kotúče; chýbali v core)
    ("volkswagen", "golf vii", "1.6 TDI", ""),
    ("volkswagen", "golf viii", "2.0 TDI", "150hp"),
    ("volkswagen", "passat b8", "2.0 TDI", ""),
    ("volkswagen", "polo vi", "1.0 TSI", "70kw"),
    ("volkswagen", "tiguan", "2.0 TDI", "110kw"),
    ("volkswagen", "touran", "2.0 TDI", ""),
    ("volkswagen", "transporter", "2.0 TDI", ""),
    ("volkswagen", "taigo", "1.0 TSI", ""),
    ("skoda", "fabia iii", "1.0 TSI", "70kw"),
    ("skoda", "fabia iv", "1.0 TSI", ""),
    ("skoda", "octavia iii", "1.6 TDI", ""),
    ("skoda", "superb iii", "2.0 TDI", "150hp"),
    ("skoda", "karoq", "2.0 TDI", ""),
    ("skoda", "kodiaq", "2.0 TDI", "150hp"),
    ("skoda", "scala", "1.0 TSI", ""),
    ("skoda", "rapid", "1.2 TSI", ""),
    ("seat", "leon iii", "1.6 TDI", ""),
    ("seat", "ibiza", "1.0 TSI", ""),
    ("seat", "ateca", "2.0 TDI", ""),
    ("audi", "a3 8v", "2.0 TDI", ""),
    ("audi", "a4", "2.0 TDI", ""),
    ("audi", "q5", "2.0 TDI", ""),
    ("bmw", "3 (g20)", "320d", ""),
    ("bmw", "x2", "xDrive20d", ""),
    ("mercedes-benz", "b-class", "200", ""),
    ("mercedes-benz", "sprinter", "314", ""),
    ("ford", "focus", "1.5", ""),
    ("ford", "ranger", "2.0", ""),
    ("opel", "astra", "1.6", ""),
    ("toyota", "corolla", "1.8", ""),
    ("hyundai", "bayon", "1.2", ""),
    ("volvo", "xc90", "D5", ""),
    ("dacia", "spring", "electric", ""),
    ("nissan", "leaf", "electric", ""),
]

WAVE3_ALL_CARS: List[Dict[str, str]] = _expand_cats(_WAVE3_ALL_CARS_CORE)

# Wave 5 — imobilizéry / kľúče / štart bez kľúča (LKQ nested Categories).
# Needles: „Centrálne zamykanie a alarm“, „Zámky a kliky“, „Spínače skrinky zapalovania“.
_WAVE5_IMMOB_CORE: List[tuple] = [
    ("skoda", "octavia iii", "1.6 TDI", ""),
    ("skoda", "octavia iii", "1.4 TSI", "110kw"),
    ("skoda", "fabia iii", "1.0 TSI", "70kw"),
    ("skoda", "superb iii", "2.0 TDI", "150hp"),
    ("skoda", "karoq", "2.0 TDI", ""),
    ("skoda", "kodiaq", "2.0 TDI", "150hp"),
    ("volkswagen", "golf vii", "1.6 TDI", ""),
    ("volkswagen", "golf viii", "2.0 TDI", "150hp"),
    ("volkswagen", "polo vi", "1.0 TSI", "70kw"),
    ("volkswagen", "passat b8", "2.0 TDI", ""),
    ("volkswagen", "tiguan", "2.0 TDI", "110kw"),
    ("audi", "a3 8v", "2.0 TDI", ""),
    ("audi", "a4", "2.0 TDI", ""),
    ("seat", "leon iii", "1.6 TDI", ""),
    ("bmw", "3 (g20)", "320d", ""),
    ("mercedes-benz", "c-class (w205)", "220", ""),
    ("ford", "focus", "1.5", ""),
    ("opel", "astra", "1.6", ""),
    ("toyota", "corolla", "1.8", ""),
    ("hyundai", "i30", "1.6", ""),
]
_WAVE5_CATS = (
    "centralne zamykanie",
    "zamky a kliky",
    "spinace skrinky zapalovania",
)
WAVE5_IMMOB_KEYS: List[Dict[str, str]] = _expand_cats(
    _WAVE5_IMMOB_CORE, cats=_WAVE5_CATS
)

# Wave 6 — mop-up kategórie na tom istom SK parku (po Wave3 filtre/kotúče).
# Needles musia sedieť na LKQ Garage Text (supplier_category_map.json).
_WAVE6_CATS = (
    "stierače",
    "zapalovanie",
    "ložiská",
    "tlmiče",
    "okruh chladenia",
    "spojka",
    "remene",
    "snímače",
)
WAVE6_MOPUP: List[Dict[str, str]] = _expand_cats(
    _WAVE3_ALL_CARS_CORE, cats=_WAVE6_CATS
)

# Bežné SK vozidlá × kategórie (filtre / brzdy / stierače). Engine/kw = typický SK park.
# Heijunka: nové riadky = ďalšie drobné diely do evidencie; hotové sa skipnú cez state.
# Wave 3 (všetky chýbajúce autá) ide PRVÁ — priorita coverage celého parku.
_QUEUE_CORE: List[Dict[str, str]] = [
    # Škoda
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "filtre"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "kotúčová"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "stierače"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "stierače"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.4 TSI", "kw": "110kw", "category": "filtre"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "filtre"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "kotúčová"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "stierače"},
    {"make": "skoda", "model": "superb iii", "engine": "1.5 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "rapid", "engine": "1.2 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "rapid", "engine": "1.2 TSI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "stierače"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "stierače"},
    {"make": "skoda", "model": "karoq", "engine": "1.5 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "karoq", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "karoq", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "kodiaq", "engine": "2.0 TDI", "kw": "150hp", "category": "filtre"},
    {"make": "skoda", "model": "kodiaq", "engine": "2.0 TDI", "kw": "150hp", "category": "kotúčová"},
    {"make": "skoda", "model": "kodiaq", "engine": "2.0 TDI", "kw": "150hp", "category": "stierače"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "filtre"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "kotúčová"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "stierače"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "filtre"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "kotúčová"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "stierače"},
    # VW
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "kotúčová"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "stierače"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.4 TSI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "golf viii", "engine": "2.0 TDI", "kw": "150hp", "category": "filtre"},
    {"make": "volkswagen", "model": "golf viii", "engine": "2.0 TDI", "kw": "150hp", "category": "kotúčová"},
    {"make": "volkswagen", "model": "golf viii", "engine": "1.5 TSI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "golf viii", "engine": "1.5 TSI", "kw": "", "category": "stierače"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "filtre"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "kotúčová"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "stierače"},
    {"make": "volkswagen", "model": "passat", "engine": "2.0 TDI", "kw": "110kw", "category": "filtre"},
    {"make": "volkswagen", "model": "passat", "engine": "2.0 TDI", "kw": "110kw", "category": "kotúčová"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "stierače"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "filtre"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "kotúčová"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "stierače"},
    {"make": "volkswagen", "model": "caddy", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "caddy", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "volkswagen", "model": "touran", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "touareg", "engine": "3.0 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "touareg", "engine": "3.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "volkswagen", "model": "transporter t6", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "volkswagen", "model": "transporter t6", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    # Audi / Seat
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "stierače"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "stierače"},
    {"make": "audi", "model": "a6", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "audi", "model": "a6", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "audi", "model": "q5", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "audi", "model": "q5", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "filtre"},
    {"make": "seat", "model": "leon iii", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "kotúčová"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "stierače"},
    {"make": "seat", "model": "ibiza", "engine": "1.2 TSI", "kw": "", "category": "filtre"},
    {"make": "seat", "model": "ibiza", "engine": "1.2 TSI", "kw": "", "category": "kotúčová"},
    {"make": "seat", "model": "ateca", "engine": "2.0 TDI", "kw": "", "category": "filtre"},
    {"make": "seat", "model": "ateca", "engine": "2.0 TDI", "kw": "", "category": "kotúčová"},
    # Ďalšie značky SK park (konkrétny model/motor = LKQ Garage match)
    # model needle pre LKQ Garage; fitment mapuje G20/F30 → „3 Series“
    {"make": "bmw", "model": "3 (g20)", "engine": "320d", "kw": "", "category": "filtre"},
    {"make": "bmw", "model": "3 (g20)", "engine": "320d", "kw": "", "category": "kotúčová"},
    {"make": "bmw", "model": "3 (f30)", "engine": "320d", "kw": "", "category": "filtre"},
    {"make": "mercedes-benz", "model": "c-class (w205)", "engine": "220", "kw": "", "category": "filtre"},
    {"make": "mercedes-benz", "model": "c-class (w205)", "engine": "220", "kw": "", "category": "kotúčová"},
    {"make": "mercedes-benz", "model": "c-class (w205)", "engine": "220", "kw": "", "category": "stierače"},
    {"make": "opel", "model": "astra", "engine": "1.6", "kw": "", "category": "filtre"},
    {"make": "opel", "model": "astra", "engine": "1.6", "kw": "", "category": "kotúčová"},
    {"make": "renault", "model": "megane iv", "engine": "1.5", "kw": "", "category": "filtre"},
    {"make": "renault", "model": "megane iv", "engine": "1.5", "kw": "", "category": "kotúčová"},
    {"make": "kia", "model": "ceed", "engine": "1.6", "kw": "", "category": "filtre"},
    {"make": "kia", "model": "ceed", "engine": "1.6", "kw": "", "category": "kotúčová"},
    {"make": "kia", "model": "ceed", "engine": "1.6", "kw": "", "category": "stierače"},
    {"make": "hyundai", "model": "i30", "engine": "1.6", "kw": "", "category": "filtre"},
    {"make": "hyundai", "model": "i30", "engine": "1.6", "kw": "", "category": "kotúčová"},
    {"make": "ford", "model": "focus", "engine": "1.5", "kw": "", "category": "filtre"},
    {"make": "ford", "model": "focus", "engine": "1.5", "kw": "", "category": "kotúčová"},
    {"make": "toyota", "model": "corolla", "engine": "1.8", "kw": "", "category": "filtre"},
    {"make": "toyota", "model": "corolla", "engine": "1.8", "kw": "", "category": "kotúčová"},
    {"make": "peugeot", "model": "308", "engine": "1.6", "kw": "", "category": "filtre"},
    {"make": "peugeot", "model": "308", "engine": "1.6", "kw": "", "category": "kotúčová"},
    {"make": "citroen", "model": "c3", "engine": "1.2", "kw": "", "category": "filtre"},
    {"make": "dacia", "model": "duster", "engine": "1.5", "kw": "", "category": "filtre"},
    {"make": "dacia", "model": "duster", "engine": "1.5", "kw": "", "category": "kotúčová"},
    {"make": "honda", "model": "civic", "engine": "1.5", "kw": "", "category": "filtre"},
    # Wave 2 — zapaľovanie / tlmiče (ďalšie drobné diely SK park)
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "zapalovanie"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "tlmiče"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "tlmiče"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "zapalovanie"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "tlmiče"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "zapalovanie"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "zapalovanie"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "zapalovanie"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "zapalovanie"},
    {"make": "skoda", "model": "karoq", "engine": "2.0 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "skoda", "model": "kodiaq", "engine": "2.0 TDI", "kw": "150hp", "category": "zapalovanie"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "tlmiče"},
    {"make": "volkswagen", "model": "golf viii", "engine": "2.0 TDI", "kw": "150hp", "category": "zapalovanie"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "zapalovanie"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "tlmiče"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "zapalovanie"},
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "zapalovanie"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "tlmiče"},
    {"make": "bmw", "model": "3 (g20)", "engine": "320d", "kw": "", "category": "zapalovanie"},
    {"make": "mercedes-benz", "model": "c-class (w205)", "engine": "220", "kw": "", "category": "zapalovanie"},
    {"make": "kia", "model": "ceed", "engine": "1.6", "kw": "", "category": "zapalovanie"},
    {"make": "hyundai", "model": "i30", "engine": "1.6", "kw": "", "category": "zapalovanie"},
    # Focus IV benzín nemá leaf „Zapalovanie“; EcoBlue má žhavenie
    {"make": "ford", "model": "focus", "engine": "1.5 EcoBlue", "kw": "", "category": "zapalovanie"},
    {"make": "opel", "model": "astra", "engine": "1.6", "kw": "", "category": "zapalovanie"},
    {"make": "renault", "model": "megane iv", "engine": "1.5", "kw": "", "category": "zapalovanie"},
    {"make": "toyota", "model": "corolla", "engine": "1.8", "kw": "", "category": "zapalovanie"},
    {"make": "dacia", "model": "duster", "engine": "1.5", "kw": "", "category": "zapalovanie"},
    # Wave 3 — ložiská / náprava / snímače / remene / tesnenie
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "ložiská"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "diely nápravy"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "snímače"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "remene"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "ložiská"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "diely nápravy"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "snímače"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "remene"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "ložiská"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "diely nápravy"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "ložiská"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "diely nápravy"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "ložiská"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "ložiská"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "ložiská"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "ložiská"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "diely nápravy"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "snímače"},
    {"make": "volkswagen", "model": "golf viii", "engine": "2.0 TDI", "kw": "150hp", "category": "ložiská"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "ložiská"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "diely nápravy"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "ložiská"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "ložiská"},
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "ložiská"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "ložiská"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "ložiská"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "diely nápravy"},
    {"make": "bmw", "model": "3 (g20)", "engine": "320d", "kw": "", "category": "ložiská"},
    {"make": "mercedes-benz", "model": "c-class (w205)", "engine": "220", "kw": "", "category": "ložiská"},
    {"make": "kia", "model": "ceed", "engine": "1.6", "kw": "", "category": "ložiská"},
    {"make": "hyundai", "model": "i30", "engine": "1.6", "kw": "", "category": "ložiská"},
    {"make": "ford", "model": "focus", "engine": "1.5", "kw": "", "category": "ložiská"},
    {"make": "opel", "model": "astra", "engine": "1.6", "kw": "", "category": "ložiská"},
    {"make": "toyota", "model": "corolla", "engine": "1.8", "kw": "", "category": "ložiská"},
    # Wave 4 — tesnenie / chladenie / brzdová hydraulika (top SK park only)
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "tesnenie"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "okruh chladenia"},
    {"make": "skoda", "model": "fabia iii", "engine": "1.0 TSI", "kw": "70kw", "category": "brzdová hydraulika"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "tesnenie"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "skoda", "model": "octavia iii", "engine": "1.6 TDI", "kw": "", "category": "brzdová hydraulika"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "tesnenie"},
    {"make": "skoda", "model": "superb iii", "engine": "2.0 TDI", "kw": "150hp", "category": "okruh chladenia"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "tesnenie"},
    {"make": "skoda", "model": "octavia iv", "engine": "2.0 TDI", "kw": "110kw", "category": "okruh chladenia"},
    {"make": "skoda", "model": "fabia iv", "engine": "1.0 TSI", "kw": "", "category": "tesnenie"},
    {"make": "skoda", "model": "scala", "engine": "1.0 TSI", "kw": "", "category": "okruh chladenia"},
    {"make": "skoda", "model": "kamiq", "engine": "1.0 TSI", "kw": "", "category": "okruh chladenia"},
    {"make": "skoda", "model": "karoq", "engine": "2.0 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "skoda", "model": "kodiaq", "engine": "2.0 TDI", "kw": "150hp", "category": "okruh chladenia"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "tesnenie"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "volkswagen", "model": "golf vii", "engine": "1.6 TDI", "kw": "", "category": "brzdová hydraulika"},
    {"make": "volkswagen", "model": "golf viii", "engine": "2.0 TDI", "kw": "150hp", "category": "okruh chladenia"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "okruh chladenia"},
    {"make": "volkswagen", "model": "polo vi", "engine": "1.0 TSI", "kw": "70kw", "category": "brzdová hydraulika"},
    {"make": "volkswagen", "model": "passat b8", "engine": "2.0 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "volkswagen", "model": "tiguan", "engine": "2.0 TDI", "kw": "110kw", "category": "okruh chladenia"},
    {"make": "audi", "model": "a3 8v", "engine": "2.0 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "audi", "model": "a4", "engine": "2.0 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "okruh chladenia"},
    {"make": "seat", "model": "leon iii", "engine": "1.6 TDI", "kw": "", "category": "brzdová hydraulika"},
]

# Wave 5 (imobil/kľúče) + Wave 3 (park) + VAG/core.
_BASE_QUEUE: List[Dict[str, str]] = (
    WAVE5_IMMOB_KEYS + WAVE3_ALL_CARS + WAVE6_MOPUP + _QUEUE_CORE
)


def _fold_make(name: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "")
    return "".join(c for c in t if not unicodedata.combining(c)).strip().lower()


def full_fleet_queue() -> List[Dict[str, str]]:
    """Plný LKQ katalóg (2026-10-23, Samuel): celý SK park z vlastnej taxonómie
    (data/vehicle_hierarchy_sk.json: značka × model+generácia × motor) × všetky
    LKQ kategórie, ktoré majú mapovanie v data/supplier_category_map.json
    (lkq_needles → žiadne fallback kategórie). Poradie: značky podľa SK parku."""
    try:
        tree = json.loads((ROOT / "data" / "vehicle_hierarchy_sk.json").read_text(encoding="utf-8"))
        needles = json.loads((ROOT / "data" / "supplier_category_map.json").read_text(encoding="utf-8"))["lkq_needles"]
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("full fleet queue: %s", exc)
        return []
    cats = tuple(needles.keys())
    cores: List[tuple] = []
    seen: set = set()
    for make, models in (tree.get("makes") or {}).items():
        mk = _fold_make(make)
        for model, info in (models or {}).items():
            gens = info.get("generations") or [{"label": "", "engines": info.get("engines") or []}]
            for g in gens:
                mo = f"{model} {g.get('label') or ''}".strip().lower()
                for eng in (g.get("engines") or info.get("engines") or [""]):
                    c = (mk, mo, eng, "")
                    if c not in seen:
                        seen.add(c)
                        cores.append(c)
    return _expand_cats(cores, cats=cats)


def _dedupe(jobs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out, seen = [], set()
    for j in jobs:
        k = "|".join(j.get(f, "") for f in ("make", "model", "engine", "kw", "category"))
        if k not in seen:
            seen.add(k)
            out.append(j)
    return out


# Wave 5 (imobil/kľúče) + Wave 3 (park) + VAG/core, potom celý SK park × všetky
# LKQ kategórie (SG_LKQ_FULL_FLEET=0 vypne). Hotové joby sa preskočia cez state.
DEFAULT_QUEUE: List[Dict[str, str]] = _dedupe(
    _BASE_QUEUE + (full_fleet_queue() if os.environ.get("SG_LKQ_FULL_FLEET", "1") != "0" else [])
)

# Cloud overnight grind (2026-08-03) — pomenované presety kategórií pre
# `--custom-categories`, mapované na celý SK park (_WAVE3_ALL_CARS_CORE).
# Needles sú fuzzy-matchované cez lkq_find_vehicle_category._tokens_match,
# takže nemusia presne sedieť s LKQ Text — len obsahovať kľúčové slovo.
#
# POZOR needle výber: LKQ Categories/{last} vracia top-level uzly ako
# „Poistky, patice, konektory“, „Relé“, „Filtre“, „Zapalovanie / žhavenie“,
# „Tesnenie“, „Okruh chladenia motora“, „Brzdová hydraulika“, „Snímače a
# čidlá“ — LKQ NEMÁ samostatné top-level uzly pre kabínový/vzduchový filter,
# lambda sondu, termostat, vodné čerpadlo ani AdBlue (sú vnorené hlbšie).
# _find_category_deep na nematch robí BFS + 4× retry s exp. backoff na
# KAŽDÝ uzol → jeden nematch needle = až rádovo minúty na job (overené).
# Preto presety mieria len na overené rýchle top-level needles; jemnejšie
# diely (lambda/termostat/vodné čerpadlo/kabínový filter) sú v praxi súčasťou
# týchto širších uzlov (snímače / okruh chladenia / filtre) a AI kategorizuje
# produkt správne aj tak (taxonomy.match_keyword_category podľa názvu).
CATEGORY_PRESETS: Dict[str, tuple] = {
    "poistky_rele": ("poistky", "relé"),
    "sviecky": ("zapalovanie",),
    "stierace": ("stierače",),
    "filtre_kabina_vzduch": ("filtre",),
    "dot_adblue_chladenie": ("brzdová hydraulika", "okruh chladenia"),
    "lambda_termostat_pumpy_tesnenia": ("snímače", "okruh chladenia", "tesnenie"),
}


def custom_categories_queue(names_csv: str) -> List[Dict[str, str]]:
    """Rozlož `--custom-categories` (presety alebo voľné needle texty, čiarkou)."""
    cats: List[str] = []
    for tok in (names_csv or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        preset = CATEGORY_PRESETS.get(tok.lower())
        if preset:
            cats.extend(preset)
        else:
            cats.append(tok)
    if not cats:
        return []
    return _expand_cats(_WAVE3_ALL_CARS_CORE, cats=tuple(cats))


def _job_key(job: Dict[str, str]) -> str:
    return "|".join(
        [
            job.get("make", ""),
            job.get("model", ""),
            job.get("engine", ""),
            job.get("kw", ""),
            job.get("category", ""),
        ]
    )


def _is_permanent_skip(key: str) -> bool:
    return any(key.startswith(p) for p in PERMANENT_SKIP_PREFIXES)


def _load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "failed": {}, "url_cache": {}, "updated_at": None}


def _save_state(state: Dict[str, Any]) -> None:
    """Atomický zápis + flock (2 shard workery zdieľajú ten istý state)."""
    import fcntl

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    lock_path = STATE_PATH.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        disk: Dict[str, Any] = {}
        if STATE_PATH.exists():
            try:
                disk = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                disk = {}
        # merge in-place; tombstones = zámerné zmazania (reopen skip / reset)
        done = state.setdefault("done", {})
        failed = state.setdefault("failed", {})
        cache = state.setdefault("url_cache", {})
        tombs = set(state.setdefault("_tombstones", []))
        for k, v in (disk.get("done") or {}).items():
            if k in tombs:
                continue
            done.setdefault(k, v)
        for k, v in (disk.get("failed") or {}).items():
            if k not in done and k not in tombs:
                failed.setdefault(k, v)
        for k, v in (disk.get("url_cache") or {}).items():
            cache.setdefault(k, v)
        for k in list(failed):
            if k in done:
                failed.pop(k, None)
        # tombstones len pre tento zápis — nepersistuj navždy
        state["_tombstones"] = []
        STATE_PATH.write_text(
            json.dumps(
                {
                    "done": done,
                    "failed": failed,
                    "url_cache": cache,
                    "updated_at": state["updated_at"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _canon_vehicle_key(job: Dict[str, str]) -> str:
    """make|model po mapovaní na SG hierarchiu (pre coverage skip)."""
    try:
        from fitment import normalize_vehicle_pair
    except Exception:
        return f"{job.get('make', '')}|{job.get('model', '')}".lower()
    pair = normalize_vehicle_pair(
        make=job.get("make") or "",
        model=job.get("model") or "",
        engine=job.get("engine") or "",
    )
    if pair.get("make") and pair.get("model"):
        return f"{pair['make']}|{pair['model']}".lower()
    return f"{job.get('make', '')}|{job.get('model', '')}".lower()


def _wc_covered_vehicles(min_products: int = 6) -> set:
    """Vozidlá (make|model), ktoré už majú dosť publish produktov s JSON pármi."""
    try:
        import requests
        from requests.auth import HTTPBasicAuth
    except ImportError:
        return set()
    wc = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY")
    secret = os.environ.get("WC_CONSUMER_SECRET")
    if not (wc and key and secret):
        return set()
    auth = HTTPBasicAuth(key, secret)
    counts: Dict[str, int] = {}
    for page in range(1, 9):
        resp = requests.get(
            f"{wc}/wp-json/wc/v3/products",
            auth=auth,
            params={
                "per_page": 50,
                "page": page,
                "status": "publish",
                "orderby": "id",
                "order": "desc",
            },
            timeout=60,
        )
        if not resp.ok:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        for product in batch:
            meta = {
                m["key"]: m.get("value")
                for m in (product.get("meta_data") or [])
                if isinstance(m, dict)
            }
            raw = meta.get("_sg_fit_vehicles") or ""
            if isinstance(raw, str) and raw.strip().startswith("["):
                try:
                    vehicles = json.loads(raw)
                except Exception:
                    vehicles = []
            elif isinstance(raw, list):
                vehicles = raw
            else:
                continue
            for v in vehicles:
                if not isinstance(v, dict):
                    continue
                mk = str(v.get("make") or "").strip()
                md = str(v.get("model") or "").strip()
                if mk and md:
                    k = f"{mk}|{md}".lower()
                    counts[k] = counts.get(k, 0) + 1
        if len(batch) < 50:
            break
    return {k for k, n in counts.items() if n >= min_products}


def _find_url(job: Dict[str, str], url_cache: Optional[Dict[str, str]] = None) -> str:
    key = _job_key(job)
    if url_cache and url_cache.get(key):
        LOGGER.info("URL cache hit %s", key)
        return url_cache[key]

    # engine fallbacky — typický SK park (muda: nepadni na 1. pokus)
    engines = [job.get("engine") or ""]
    eng0 = (job.get("engine") or "").strip()
    if eng0:
        # skráť „2.0 TDI“ → „2.0“; „xDrive20d“ nechaj
        short = eng0.split()[0] if " " in eng0 else ""
        if short and short not in engines:
            engines.append(short)
    if not eng0:
        engines.append("")

    attempts_kw = [job.get("kw") or ""]
    if job.get("kw"):
        attempts_kw.append("")

    last_err = ""
    for engine in engines:
        for kw in attempts_kw:
            cmd = [
                sys.executable,
                str(ROOT / "lkq_find_vehicle_category.py"),
                "--make",
                job["make"],
                "--model",
                job["model"],
                "--engine",
                engine or job.get("engine") or "",
                "--category",
                job["category"],
            ]
            if kw:
                cmd.extend(["--kw", kw])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("https://"):
                        if url_cache is not None:
                            url_cache[key] = line
                        return line
            last_err = proc.stderr.strip() or proc.stdout.strip() or str(proc.returncode)
    raise RuntimeError(f"finder failed: {last_err}")


def _run_import(url: str, limit: int, sleep: float, dry_run: bool) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "import_pipeline.py"),
        "--supplier",
        "lkq",
        "--limit",
        str(limit),
        "--cheap",
        "--fast",
        "--in-stock",
        "--category-url",
        url,
        "--sleep",
        str(sleep),
    ]
    if dry_run:
        cmd.append("--dry-run")
    LOGGER.info("import: %s", " ".join(cmd[-8:]))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return int(proc.returncode)


def _publish_fitment_drafts(limit: int = 40) -> int:
    """Publish drafts that already have _sg_fit_make + QA fields."""
    try:
        import requests
        from requests.auth import HTTPBasicAuth
    except ImportError:
        return 0
    wc = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY")
    secret = os.environ.get("WC_CONSUMER_SECRET")
    if not (wc and key and secret):
        return 0
    auth = HTTPBasicAuth(key, secret)
    published = 0
    # draft + private (re-import niekedy nechá private na existujúcom SKU)
    for status in ("draft", "private"):
        resp = requests.get(
            f"{wc}/wp-json/wc/v3/products",
            auth=auth,
            params={
                "per_page": min(limit, 50),
                "orderby": "id",
                "order": "desc",
                "status": status,
            },
            timeout=60,
        )
        if not resp.ok:
            continue
        for product in resp.json():
            meta = {
                m["key"]: m["value"]
                for m in (product.get("meta_data") or [])
                if isinstance(m, dict)
            }
            if not meta.get("_sg_fit_make"):
                continue
            if not (product.get("price") or product.get("regular_price")):
                continue
            if not product.get("images") or not product.get("sku") or not product.get("categories"):
                continue
            put = requests.put(
                f"{wc}/wp-json/wc/v3/products/{product['id']}",
                auth=auth,
                json={"status": "publish"},
                timeout=60,
            )
            if put.ok and put.json().get("status") == "publish":
                published += 1
                LOGGER.info(
                    "PUB #%s %s %s %s",
                    product["id"],
                    meta.get("_sg_fit_model"),
                    meta.get("_sg_fit_engine"),
                    (product.get("name") or "")[:48],
                )
    return published


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs", type=int, default=5, help="koľko frontových jobov spracovať (Heijunka)")
    ap.add_argument("--limit", type=int, default=20, help="max produktov na job (≤50)")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-only", action="store_true",
                    help="len zisti, či je výsek fronty prejdený (bez loginu/importu)")
    ap.add_argument(
        "--force-daytime",
        action="store_true",
        help="Ignoruj denný quiet (inak SK 07–20 heijunka pauza)",
    )
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--reset-failed", action="store_true", help="znova skús failed joby")
    ap.add_argument(
        "--reopen-zero-pub",
        action="store_true",
        help="znova zarad joby s published=0 (napr. po dry-run)",
    )
    ap.add_argument(
        "--shard",
        default="",
        help="paralelizmus: 0/2 alebo 1/2 (hash make) — zdieľaný state + flock",
    )
    ap.add_argument(
        "--skip-covered",
        action="store_true",
        help="preskoč filtre/kotúče/stierače ak auto už má ≥6 publish JSON párov",
    )
    ap.add_argument(
        "--wave3-only",
        action="store_true",
        help="len Wave 3 (nové autá) — efektívne coverage všetkých značiek",
    )
    ap.add_argument(
        "--wave5-only",
        action="store_true",
        help="len Wave 5 — imobilizéry / zámky / spínacie skrinky (keyless)",
    )
    ap.add_argument(
        "--wave6-only",
        action="store_true",
        help="len Wave 6 — stierače/zapaľovanie/ložiská/tlmiče/chladenie/spojka/remene/snímače",
    )
    ap.add_argument(
        "--custom-categories",
        default="",
        help=(
            "Cloud overnight: čiarkou oddelené presety "
            f"({', '.join(sorted(CATEGORY_PRESETS))}) alebo voľné needle texty "
            "— queue = celý SK park × tieto kategórie"
        ),
    )
    args = ap.parse_args()
    if args.limit > 50:
        raise SystemExit("Heijunka: --limit max 50")
    if args.jobs > 12:
        args.jobs = 12

    only_flags = sum(
        bool(x)
        for x in (args.wave3_only, args.wave5_only, args.wave6_only, args.custom_categories)
    )
    if only_flags > 1:
        raise SystemExit(
            "Zvoľ najviac jedno z --wave3-only / --wave5-only / --wave6-only / --custom-categories"
        )

    if not args.force_daytime and not args.dry_run:
        try:
            from daytime_quiet import is_daytime_quiet, quiet_message

            if is_daytime_quiet():
                LOGGER.warning("%s", quiet_message())
                return 0
        except Exception:
            pass

    state = _load_state()
    if args.reset_failed:
        keep = {
            k: v
            for k, v in (state.get("failed") or {}).items()
            if _is_permanent_skip(k) or (isinstance(v, dict) and v.get("permanent"))
        }
        state["failed"] = keep
    done = state.setdefault("done", {})
    failed = state.setdefault("failed", {})
    # Doplň permanent skip do failed, aby nikdy nešli do pending
    for j in list(WAVE3_ALL_CARS) + list(WAVE6_MOPUP) + list(DEFAULT_QUEUE):
        k = _job_key(j)
        if _is_permanent_skip(k) and k not in failed:
            failed[k] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "error": "permanent skip",
                "permanent": True,
            }
    url_cache = state.setdefault("url_cache", {})
    if args.reopen_zero_pub:
        drop = [k for k, v in done.items() if int(v.get("published") or 0) == 0]
        for k in drop:
            done.pop(k, None)
        LOGGER.info("reopen-zero-pub: uvoľnených %d jobov", len(drop))

    if args.custom_categories:
        queue = custom_categories_queue(args.custom_categories)
        if not queue:
            raise SystemExit(f"--custom-categories {args.custom_categories!r} nevytvorilo žiadny queue")
    elif args.wave6_only:
        queue = list(WAVE6_MOPUP)
    elif args.wave5_only:
        queue = list(WAVE5_IMMOB_KEYS)
    elif args.wave3_only:
        queue = list(WAVE3_ALL_CARS)
    else:
        queue = list(DEFAULT_QUEUE)
    # Najprv vlastný výsek fronty (shard + účet), až potom test „prejdená“.
    # (Predtým sa test robil na CELEJ fronte → shard s hotovým výsekom ostal
    # navždy na pending=0 a ~4,5 h točil prázdne kolá bez nových LKQ produktov.)
    slice_q = list(queue)
    if args.shard:
        try:
            shard_i, shard_n = (int(x) for x in args.shard.split("/", 1))
        except ValueError as exc:
            raise SystemExit(f"Zly shard {args.shard!r}: {exc}") from exc
        if shard_n < 1 or shard_i < 0 or shard_i >= shard_n:
            raise SystemExit(f"Zly shard {args.shard!r}")
        slice_q = [
            j for j in slice_q
            if (__import__("zlib").crc32(_job_key(j).encode("utf-8")) % shard_n) == shard_i
        ]
    from account_split import mine as _acct_mine, count as _acct_n
    if _acct_n() > 1 and not args.shard:
        # So shardom delí účty už parita shardu (A párne, B nepárne) — druhý filter
        # by polovicu jobov výseku nedal nikomu.
        slice_q = [j for j in slice_q if _acct_mine("hv:" + _job_key(j))]
    pending = [j for j in slice_q if _job_key(j) not in done and _job_key(j) not in failed]
    LOGGER.info("shard %s → výsek=%d pending=%d", args.shard or "-", len(slice_q), len(pending))
    if not pending and slice_q and not args.dry_run:
        # Výsek prejdený. Nový cyklus (SG_SKIP_EXISTING=1 → len nové SKU) najskôr
        # po SG_LKQ_RECYCLE_H hodinách; dovtedy hneď koniec → uvoľní runner slot.
        recycle_h = float(os.environ.get("SG_LKQ_RECYCLE_H", "24") or 24)
        now = datetime.now(timezone.utc)
        exhausted_at = state.get("_slice_exhausted_at")
        if not exhausted_at:
            state["_slice_exhausted_at"] = now.isoformat()
            _save_state(state)
            exhausted_at = state["_slice_exhausted_at"]
        try:
            age_h = (now - datetime.fromisoformat(exhausted_at)).total_seconds() / 3600
        except Exception:
            age_h = recycle_h
        if age_h < recycle_h or args.check_only:
            LOGGER.info("fronta prejdená (výsek %d jobov hotový pred %.1f h; nový cyklus o %.1f h)",
                        len(slice_q), age_h, max(0.0, recycle_h - age_h))
            return 0
        qkeys = {_job_key(j) for j in slice_q}
        reset = [k for k in list(done.keys()) if k in qkeys]
        for k in reset:
            done.pop(k, None)
        state.pop("_slice_exhausted_at", None)
        state["_tombstones"] = list(set(state.get("_tombstones") or []) | set(reset))
        _save_state(state)
        pending = [j for j in slice_q if _job_key(j) not in failed]
        LOGGER.info("nový cyklus fronty (len nové SKU), pending=%d", len(pending))
    elif pending and state.pop("_slice_exhausted_at", None):
        _save_state(state)
    if args.check_only:
        LOGGER.info("check-only: pending=%d", len(pending))
        return 0

    covered: set = set()
    if args.skip_covered:
        covered = _wc_covered_vehicles(min_products=6)
        LOGGER.info("WC covered vehicles (≥6 JSON): %d", len(covered))

    LOGGER.info(
        "fronta: pending=%d done=%d failed=%d wave3=%s wave5=%s wave6=%s skip_covered=%s",
        len(pending),
        len(done),
        len(failed),
        args.wave3_only,
        args.wave5_only,
        args.wave6_only,
        args.skip_covered,
    )
    batch = pending[: max(0, args.jobs)]
    results: List[Dict[str, Any]] = []
    _skip_cats = {"filtre", "kotúčová", "stierače"}

    for job in batch:
        if os.environ.get("SG_LKQ_DISABLED") == "1" or os.path.exists(os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed")):
            LOGGER.error("LKQ login guard: login zlyhal — ruším zvyšné LKQ joby")
            break
        key = _job_key(job)
        LOGGER.info("=== JOB %s ===", key)
        row: Dict[str, Any] = {"job": job, "key": key, "ok": False}
        try:
            # Muda: auto už v evidencii → neimportuj znova filtre/kotúče
            if (
                args.skip_covered
                and (job.get("category") or "") in _skip_cats
                and _canon_vehicle_key(job) in covered
            ):
                LOGGER.info("SKIP covered %s (%s)", key, _canon_vehicle_key(job))
                row["ok"] = True
                row["skipped_covered"] = True
                if not args.dry_run:
                    done[key] = {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "skipped_covered": True,
                        "vehicle": _canon_vehicle_key(job),
                    }
                    failed.pop(key, None)
                results.append(row)
                _save_state(state)
                continue

            url = _find_url(job, url_cache=url_cache)
            row["url"] = url
            LOGGER.info("URL %s", url)
            rc = _run_import(url, args.limit, args.sleep, args.dry_run)
            row["import_rc"] = rc
            if rc != 0:
                raise RuntimeError(f"import_pipeline rc={rc}")
            if not args.dry_run and not args.no_publish:
                row["published"] = _publish_fitment_drafts(limit=40)
            row["ok"] = True
            # dry-run nesmie zatvoriť frontu — inak „hotové“ bez evidencie v WC
            if not args.dry_run:
                done[key] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "url": url,
                    "limit": args.limit,
                    "published": row.get("published", 0),
                }
                failed.pop(key, None)
        except Exception as exc:
            LOGGER.error("JOB FAIL %s: %s", key, exc)
            row["error"] = str(exc)
            if not args.dry_run:
                failed[key] = {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        results.append(row)
        _save_state(state)
        time.sleep(0.3)

    REPORTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = REPORTS / f"ops-heijunka-vehicle-{ts}.md"
    lines = [
        f"# Heijunka vehicle parts — {ts}",
        "",
        f"- jobs this run: {len(results)}",
        f"- ok: {sum(1 for r in results if r.get('ok'))}",
        f"- fail: {sum(1 for r in results if not r.get('ok'))}",
        f"- queue done total: {len(done)} / {len(queue)}",
        f"- queue failed: {len(failed)}",
        "",
        "## Runs",
    ]
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        lines.append(f"- **{status}** `{r['key']}` pub={r.get('published', 0)} {r.get('error', '')}")
        if r.get("url"):
            lines.append(f"  - {r['url']}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("report %s", report)
    LOGGER.info("stav %s (done=%d failed=%d)", STATE_PATH, len(done), len(failed))
    # Heijunka: jeden padnutý leaf (napr. chýbajúca LKQ kategória) nemá zabíjať večer.
    ok_n = sum(1 for r in results if r.get("ok"))
    if not results or ok_n > 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
