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
    """Full LKQ catalog: the whole park from the local taxonomy
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
    prio: List[tuple] = []
    seen: set = set()
    # pohon bez objemu (BEV/PHEV…) → LKQ motor „ľubovoľný“ (prvý v Garage)
    any_eng = {"bev", "ev", "phev", "hybrid", "reev", "benzín", "benzin"}
    for make, models in (tree.get("makes") or {}).items():
        mk = _fold_make(make)
        for model, info in (models or {}).items():
            gens = info.get("generations") or [{"label": "", "engines": info.get("engines") or []}]
            for g in gens:
                mo = f"{model} {g.get('label') or ''}".strip().lower()
                for eng in (g.get("engines") or info.get("engines") or [""]):
                    if str(eng).strip().lower() in any_eng:
                        eng = ""
                    c = (mk, mo, eng, "")
                    if c not in seen:
                        seen.add(c)
                        # nové priority-A modely (seed) idú na začiatok fronty
                        (prio if info.get("priority") == "A" else cores).append(c)
    cores = prio + cores
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


SERVIS_QUEUE_PATH = ROOT / "data" / "servis_balicky_queue.json"


def servis_balicky_queue() -> List[Dict[str, str]]:
    """Servisné balíčky: autá × motor (kW) × filtre/zapaľovanie z bot/data/servis_balicky_queue.json.

    Fronta sa generuje z auditu servisných balíčkov (balíček sa zobrazí až keď
    dodávateľská kompatibilita LKQ potvrdí filtre pre presný motor).
    """
    try:
        data = json.loads(SERVIS_QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"servis_balicky_queue.json: {exc}") from exc
    out: List[Dict[str, str]] = []
    for it in data.get("items") or []:
        if not (it.get("make") and it.get("model") and it.get("engine") and it.get("category")):
            continue
        out.append({k: str(it.get(k) or "") for k in ("make", "model", "engine", "kw", "category")})
    return _dedupe(out)


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
            if k in tombs:
                continue
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
                    # Bez tohto kľúča sa „výsek hotový pred X h“ pri každom behu nuloval (0.0 h)
                    # → nový cyklus (hľadanie nových dielov) nenastal nikdy.
                    "_slice_exhausted_at": state.get("_slice_exhausted_at"),
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


def done_record_is_empty(rec: Any) -> bool:
    """Done záznam bez potvrdeného nálezu produktov.

    skipped_covered ostáva hotový. Nový úspech má products>0.
    Záznam bez products (beh 2026-11-02 označil 0 nálezov ako done)
    sa berie ako prázdny a má sa spracovať znova.
    """
    if not isinstance(rec, dict):
        return True
    if rec.get("skipped_covered"):
        return False
    if "products" in rec:
        try:
            return int(rec.get("products") or 0) <= 0
        except (TypeError, ValueError):
            return True
    return True


def reset_empty_done(state: Dict[str, Any], keys: List[str]) -> List[str]:
    """Vyhoď z done úlohy bez nájdených produktov, aby šli znova do fronty.

    Tombstones zabránia, aby ich _save_state vrátil z disku (aj url cache).
    """
    done = state.setdefault("done", {})
    failed = state.setdefault("failed", {})
    cache = state.setdefault("url_cache", {})
    dropped = [k for k in keys if k in done and done_record_is_empty(done.get(k))]
    for k in dropped:
        done.pop(k, None)
        failed.pop(k, None)
        cache.pop(k, None)
    if dropped:
        tombs = set(state.get("_tombstones") or [])
        tombs.update(dropped)
        state["_tombstones"] = list(tombs)
        LOGGER.info("reset-empty: %d úloh s 0 produktmi vrátených do fronty", len(dropped))
    return dropped


_SECRET_ENV_KEYS = (
    "LKQ_PASSWORD",
    "LKQ_USER",
    "LKQ_USERNAME",
    "HQ_SYNC_TOKEN",
    "WC_CONSUMER_SECRET",
    "WC_CONSUMER_KEY",
)


def _scrub_secrets(text: str) -> str:
    """Vyhoď hodnoty secrets z textu, ktorý ide do logu alebo do stavu fronty."""
    out = text or ""
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key) or ""
        if len(val) >= 4 and val in out:
            out = out.replace(val, "•••")
    return out


def _parse_found_count(line: str) -> Optional[int]:
    marker = "Nájdených produktov:"
    idx = line.find(marker)
    if idx < 0:
        return None
    tail = line[idx + len(marker):].lstrip()
    digits: List[str] = []
    for ch in tail:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if not digits:
        return None
    return int("".join(digits))


def _is_logged_in_html(html: str) -> bool:
    body = (html or "").lower()
    return ("logoff" in body) or ("odhlás" in body) or ("odhlas" in body)


def _is_login_page(html: str) -> bool:
    """Mobilný login formulár LKQ. Prihlásená stránka (logoff) ním nie je."""
    if _is_logged_in_html(html):
        return False
    body = (html or "").lower()
    return ('name="usernamem"' in body) or ('name="passwordm"' in body)


def _is_lkq_catalog_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return lowered.startswith("https://www.lkq.sk/catalog/") or lowered.startswith(
        "https://lkq.sk/catalog/"
    )


def _lkq_cookie_path() -> str:
    return os.environ.get("SG_LKQ_COOKIE_FILE", "/tmp/sg_lkq_session.pkl")


def _lkq_fail_marker() -> str:
    return os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed")


def _drop_lkq_cookie() -> None:
    try:
        os.remove(_lkq_cookie_path())
    except FileNotFoundError:
        pass
    except OSError as exc:
        LOGGER.info("LKQ cookie súbor sa nepodarilo zmazať (%s)", type(exc).__name__)


def _raw_lkq_request(supplier: Any, method: str, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """HTTP bez raise. Do výsledku nejde telo odpovede — len status, dĺžka a príznaky."""
    session = supplier.http.session
    try:
        resp = session.request(
            method,
            url,
            headers=headers or {},
            timeout=60,
            allow_redirects=True,
        )
    except Exception as exc:
        from supplier_pace import SupplierStop
        if isinstance(exc, SupplierStop):
            raise
        LOGGER.info("LKQ %s zlyhal: %s", method, type(exc).__name__)
        return {
            "status": None,
            "bytes": 0,
            "login_page": False,
            "logged_in": False,
            "text": "",
        }
    raw = resp.content or b""
    try:
        text = resp.text or ""
    except Exception:
        text = ""
    return {
        "status": int(resp.status_code),
        "bytes": len(raw),
        "login_page": _is_login_page(text),
        "logged_in": _is_logged_in_html(text),
        "text": text,
    }


def _probe_lkq_home(supplier: Any) -> Dict[str, Any]:
    info = _raw_lkq_request(
        supplier,
        "GET",
        "https://www.lkq.sk/",
        headers={"Accept": "text/html"},
    )
    info.pop("text", None)
    LOGGER.info(
        "LKQ session: http=%s bytes=%s login_page=%s logged_in=%s",
        info.get("status"),
        info.get("bytes"),
        bool(info.get("login_page")),
        bool(info.get("logged_in")),
    )
    return info


def _session_hard_fail(home: Dict[str, Any]) -> bool:
    status = home.get("status")
    if status is None or int(status) >= 400:
        return True
    return bool(home.get("login_page"))


def _load_lkq_supplier() -> Any:
    """LKQ klient z existujúcich env/HQ údajov. Heslá sa nelogujú."""
    os.environ["HQ_CRED_QUIET"] = "1"
    try:
        from suppliers.hq_credentials import export_logins

        export_logins("lkq")
    except Exception as exc:
        LOGGER.info("LKQ prihlásenie: HQ export nedostupné (%s)", type(exc).__name__)
    from suppliers import get_supplier

    return get_supplier("lkq")


def _force_lkq_relogin(supplier: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    """Najviac jeden nový POST login na beh. Cookie zmaž, nech sa nenačíta stará session."""
    if (
        run.get("force_used")
        or os.path.exists(_lkq_fail_marker())
        or os.environ.get("SG_LKQ_DISABLED") == "1"
    ):
        LOGGER.error("LKQ re-login sa nespúšťa (pokus už bol alebo je login guard)")
        return _probe_lkq_home(supplier)
    run["force_used"] = True
    LOGGER.info("LKQ session neplatná → nový login")
    _drop_lkq_cookie()
    try:
        supplier.http.session.cookies.clear()
    except Exception:
        pass
    supplier._logged_in = False
    ok = False
    try:
        ok = bool(supplier.ensure_login())
    except Exception as exc:
        LOGGER.info("LKQ re-login výnimka: %s", type(exc).__name__)
        ok = False
    home = _probe_lkq_home(supplier)
    if ok and not _session_hard_fail(home):
        run["fresh"] = True
        run["trusted"] = True
    LOGGER.info("LKQ re-login: %s", "OK" if ok else "zlyhal")
    return home


def _ensure_lkq_session(supplier: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    """Jeden login. Platná je len session s odhlásením v HTML. Inak koniec behu."""
    from supplier_pace import SupplierStop

    creds = supplier.credentials or {}
    user = str(creds.get("user") or "").strip()
    password = str(creds.get("password") or "").strip()
    if not user or not password:
        raise RuntimeError("status=failed")
    try:
        ok = bool(supplier.ensure_login())
    except SupplierStop:
        raise
    except Exception as exc:
        LOGGER.info("LKQ login výnimka: %s", type(exc).__name__)
        ok = False
    home = _probe_lkq_home(supplier)
    run["fresh"] = bool(ok and home.get("logged_in"))
    run["trusted"] = bool(home.get("logged_in"))
    if not run["trusted"]:
        raise RuntimeError("status=failed")
    LOGGER.info("status=ok")
    return home


def _parse_product_list(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("Items")
    paging = data.get("Paging")
    if not isinstance(items, list) or not isinstance(paging, dict):
        return None
    total = paging.get("TotalCount")
    total_i: Optional[int]
    try:
        total_i = int(total) if total is not None and total != "" else None
    except (TypeError, ValueError):
        total_i = None
    return {"items": len(items), "total": total_i}


def _servis_failure_is_retryable(err: str) -> bool:
    """Session alebo výpadok siete: job nechať pending. 0 produktov pri platnej session nie."""
    text = err or ""
    return text.startswith("LKQ session") or "http=None" in text or "login_page=True" in text


def _probe_reason(prefix: str, probe: Dict[str, Any]) -> str:
    bits = [
        f"http={probe.get('status')}",
        f"bytes={probe.get('bytes')}",
        f"login_page={bool(probe.get('login_page'))}",
    ]
    if "category_status" in probe:
        bits.append(f"category_http={probe.get('category_status')}")
        bits.append(f"category_bytes={probe.get('category_bytes')}")
    if probe.get("items") is not None:
        bits.append(f"items={probe.get('items')}")
    if "total" in probe:
        bits.append(f"total={probe.get('total')}")
    return prefix + " (" + " ".join(bits) + ")"


def _probe_catalog(supplier: Any, url: str) -> Dict[str, Any]:
    """GET kategórie + POST ProductList. Loguje status, dĺžku a či ide o login."""
    out: Dict[str, Any] = {
        "is_list": False,
        "login_page": False,
        "status": None,
        "bytes": 0,
        "items": None,
        "total": None,
    }
    if not _is_lkq_catalog_url(url):
        LOGGER.info("LKQ category: URL nie je Catalog http=n/a bytes=0 login_page=False")
        out["reason"] = "URL nie je LKQ Catalog"
        return out
    cat = _raw_lkq_request(
        supplier,
        "GET",
        url,
        headers={"Referer": "https://www.lkq.sk/", "Accept": "text/html"},
    )
    cat.pop("text", None)
    LOGGER.info(
        "LKQ category: http=%s bytes=%s login_page=%s",
        cat.get("status"),
        cat.get("bytes"),
        bool(cat.get("login_page")),
    )
    out["category_status"] = cat.get("status")
    out["category_bytes"] = cat.get("bytes")
    # Katalógové HTML má login formulár v šablóne aj anonymne — nie je to dôkaz,
    # že URL nie je zoznam. Rozhoduje až JSON ProductList.
    if cat.get("status") is None or int(cat.get("status") or 0) >= 400:
        out["status"] = cat.get("status")
        out["bytes"] = cat.get("bytes")
        out["login_page"] = bool(cat.get("login_page"))
        return out
    api = _raw_lkq_request(
        supplier,
        "POST",
        "https://www.lkq.sk/ProductList/Items/AllInOne/1",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": url,
        },
    )
    parsed = None if api.get("login_page") else _parse_product_list(api.get("text") or "")
    api.pop("text", None)
    LOGGER.info(
        "LKQ product list: http=%s bytes=%s login_page=%s items=%s total=%s",
        api.get("status"),
        api.get("bytes"),
        bool(api.get("login_page")),
        None if parsed is None else parsed.get("items"),
        None if parsed is None else parsed.get("total"),
    )
    status = api.get("status")
    out.update(
        {
            "status": status,
            "bytes": api.get("bytes"),
            "login_page": bool(api.get("login_page")),
            "is_list": parsed is not None and not api.get("login_page") and status is not None and int(status) < 400,
            "items": None if parsed is None else parsed.get("items"),
            "total": None if parsed is None else parsed.get("total"),
        }
    )
    return out


def _probe_empty(probe: Dict[str, Any]) -> bool:
    if not probe.get("is_list"):
        return False
    try:
        items = int(probe.get("items") or 0)
    except (TypeError, ValueError):
        items = 0
    if items > 0:
        return False
    total = probe.get("total")
    return not (isinstance(total, int) and total > 0)


def _servis_resolve_url(
    job: Dict[str, str],
    url_cache: Dict[str, str],
    supplier: Any,
    run: Dict[str, Any],
) -> tuple:
    """Nájdi Catalog URL a over, že je to zoznam produktov. 0 ks → výnimka (failed, nie done)."""
    url = _find_url(job, url_cache=url_cache)
    LOGGER.info("URL %s", url)
    probe = _probe_catalog(supplier, url)
    if not probe.get("is_list"):
        if probe.get("login_page") or probe.get("status") is None or not run.get("trusted"):
            raise RuntimeError(_probe_reason("LKQ session neplatná: URL nie je zoznam produktov", probe))
        raise RuntimeError(_probe_reason("URL nie je zoznam produktov", probe))
    if _probe_empty(probe) and not run.get("trusted"):
        raise RuntimeError(
            _probe_reason(
                "LKQ session neplatná: prázdny zoznam bez potvrdeného prihlásenia",
                probe,
            )
        )
    if _probe_empty(probe):
        raise RuntimeError(_probe_reason("Nájdených produktov: 0", probe))
    return url, probe


def _run_import(url: str, limit: int, sleep: float, dry_run: bool, *, capture: bool = False) -> tuple:
    cmd = [
        sys.executable,
        "-u",
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
    if not capture:
        proc = subprocess.run(cmd, cwd=str(ROOT))
        return int(proc.returncode), None
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    found: Optional[int] = None
    assert proc.stdout is not None
    for line in proc.stdout:
        clean = _scrub_secrets(line.rstrip("\n"))
        LOGGER.info("%s", clean)
        parsed = _parse_found_count(clean)
        if parsed is not None:
            found = parsed
    rc = int(proc.wait())
    return rc, found


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
        "--reset-empty",
        action="store_true",
        help="z done vyhoď úlohy bez potvrdeného počtu produktov (znova do fronty)",
    )
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
        "--servis-balicky",
        action="store_true",
        help="len fronta servisných balíčkov (bot/data/servis_balicky_queue.json)",
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
    # Prihlásený LKQ beh je zámerne malý. Väčší počet len s SG_LKQ_ALLOW_BURST=1.
    if os.environ.get("SG_LKQ_ALLOW_BURST") != "1":
        cap_jobs = int(os.environ.get("SG_LKQ_MAX_JOBS", "3") or 3)
        if args.jobs > cap_jobs:
            LOGGER.info("status=ok jobs=%d", cap_jobs)
            args.jobs = cap_jobs
    if os.environ.get("SG_LKQ_KEEP_SESSION") != "1" and not args.check_only:
        for stale in (
            os.environ.get("SG_LKQ_ATTEMPT_FILE", "/tmp/sg_lkq_login_attempted"),
            os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed"),
        ):
            try:
                os.remove(stale)
            except OSError:
                pass

    only_flags = sum(
        bool(x)
        for x in (args.wave3_only, args.wave5_only, args.wave6_only, args.custom_categories, args.servis_balicky)
    )
    if only_flags > 1:
        raise SystemExit(
            "Zvoľ najviac jedno z --wave3-only / --wave5-only / --wave6-only / --custom-categories / --servis-balicky"
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

    if args.servis_balicky:
        queue = servis_balicky_queue()
    elif args.custom_categories:
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
    # 0 produktov sa nesmie tváriť ako hotové. Servisný beh to robí sám,
    # --reset-empty je ten istý krok pre vybranú frontu (aj z pending checku).
    if (args.servis_balicky or args.reset_empty) and not args.dry_run:
        if reset_empty_done(state, [_job_key(j) for j in queue]):
            _save_state(state)
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
        if age_h < recycle_h:
            LOGGER.info("fronta prejdená (výsek %d jobov hotový pred %.1f h; nový cyklus o %.1f h)",
                        len(slice_q), age_h, max(0.0, recycle_h - age_h))
            return 0
        if args.check_only:
            LOGGER.info("check-only: výsek prejdený pred %.1f h → nový cyklus je na rade (pending=%d)", age_h, len(slice_q))
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
    lkq_run: Dict[str, Any] = {
        "fresh": False,
        "force_used": False,
        "trusted": False,
        "supplier": None,
        "session_dead": False,
    }

    for job in batch:
        if os.environ.get("SG_LKQ_DISABLED") == "1" or os.path.exists(os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed")):
            LOGGER.error("LKQ login guard: login zlyhal — ruším zvyšné LKQ joby")
            break
        if lkq_run.get("session_dead"):
            LOGGER.error("LKQ session neplatná — ruším zvyšné joby")
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

            probe: Dict[str, Any] = {}
            if args.servis_balicky:
                if lkq_run.get("supplier") is None:
                    lkq_run["supplier"] = _load_lkq_supplier()
                    home = _ensure_lkq_session(lkq_run["supplier"], lkq_run)
                    lkq_run["trusted"] = bool(home.get("logged_in")) and not home.get("login_page")
                url, probe = _servis_resolve_url(job, url_cache, lkq_run["supplier"], lkq_run)
            else:
                url = _find_url(job, url_cache=url_cache)
                LOGGER.info("URL %s", url)
            row["url"] = url
            t0 = time.monotonic()
            rc, found = _run_import(
                url, args.limit, args.sleep, args.dry_run, capture=bool(args.servis_balicky)
            )
            elapsed = time.monotonic() - t0
            row["import_rc"] = rc
            row["products"] = found
            if rc != 0:
                raise RuntimeError(f"import_pipeline rc={rc}")
            if args.servis_balicky and (found is None or int(found) <= 0):
                raise RuntimeError(
                    _probe_reason(
                        f"Nájdených produktov: 0 (import {elapsed:.1f}s rc={rc})",
                        probe,
                    )
                )
            if not args.dry_run and not args.no_publish:
                row["published"] = _publish_fitment_drafts(limit=40)
            row["ok"] = True
            # dry-run nesmie zatvoriť frontu — inak „hotové“ bez evidencie v WC
            if not args.dry_run:
                rec: Dict[str, Any] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "url": url,
                    "limit": args.limit,
                    "published": row.get("published", 0),
                }
                if found is not None:
                    rec["products"] = int(found)
                done[key] = rec
                failed.pop(key, None)
        except Exception as exc:
            from supplier_pace import SupplierStop
            err = _scrub_secrets(str(exc))[:500]
            LOGGER.error("JOB FAIL %s: %s", key, err)
            if isinstance(exc, SupplierStop):
                lkq_run["session_dead"] = True
                row["error"] = err
                results.append(row)
                _save_state(state)
                break
            row["error"] = err
            # Session/sieť/login: job nechaj vo fronte. 0 produktov pri platnej session ostáva failed.
            login_dead = (
                "login zlyhal" in err
                or "session neplatná" in err
                or os.path.exists(os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed"))
            )
            if login_dead or (args.servis_balicky and _servis_failure_is_retryable(err)):
                lkq_run["session_dead"] = True
            elif not args.dry_run:
                fail_rec: Dict[str, Any] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "error": err,
                }
                if "Nájdených produktov: 0" in err:
                    fail_rec["products"] = 0
                failed[key] = fail_rec
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
        prod = r.get("products")
        prod_bit = f" products={prod}" if prod is not None else ""
        lines.append(
            f"- **{status}** `{r['key']}` pub={r.get('published', 0)}{prod_bit} {r.get('error', '')}"
        )
        if r.get("url"):
            lines.append(f"  - {r['url']}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("report %s", report)
    LOGGER.info("stav %s (done=%d failed=%d)", STATE_PATH, len(done), len(failed))
    # Heijunka: jeden padnutý leaf (napr. chýbajúca LKQ kategória) nemá zabíjať večer.
    ok_n = sum(1 for r in results if r.get("ok"))
    if lkq_run.get("session_dead") or os.path.exists(os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed")):
        return 1
    if not results or ok_n > 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
