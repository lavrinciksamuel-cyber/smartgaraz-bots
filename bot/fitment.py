#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FITMENT vrstva — na čo diel SEDÍ.

Kategórie = typ dielu (rameno nápravy).
Fitment = VW / Golf / 1.6 TDI / 2013–2020 — vždy cez **vlastnú**
SmartGaráž hierarchiu (`sgv-*` ID), nie cez TecDoc/LKQ/Szekop/Inter Cars.

Hierarchia (zdroj pravdy):
  bot/data/vehicle_hierarchy_sk.json
  (+ kópia v child theme pre UI)
  Politika: bot/data/VEHICLE_TAXONOMY_POLICY.md

WooCommerce meta na produkte:
  _sg_fit_vehicles — JSON pole overených párov
    [{"make":"Škoda","model":"Octavia","engine":"1.6 TDI",
      "year_from":2012,"year_to":2021,"sg_id":"sgv-model-skoda-octavia"}, …]
  _sg_fit_make/model/engine/sg_id — odvodené pipe (spätná kompatibilita)
  _sg_fit_year_from / _sg_fit_year_to — min/max cez páry
  _sg_oe_numbers — OE/cross dielu (nie vehicle ID)
  _sg_oe_makes — OE výrobca áut z LKQ Codes (Ford|Volvo|…) pre PDP hint

Ingest pravidlo:
  - z dodávateľa ber len voľný text make/model/engine/years ALEBO OE dielu
  - cudzie vehicle ID (kType, linkage, …) → ZAHODIŤ
  - nikdy neukladaj supplier vehicle taxonomy 1:1
  - multi-vehicle = zoznam párov (nie „hala-bála“ make×model cartesian)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
HIERARCHY_PATH = ROOT / "data" / "vehicle_hierarchy_sk.json"

FITMENT_ATTRIBUTES = (
    {"slug": "pa_vyrobca_auta", "name": "Výrobca auta", "type": "select"},
    {"slug": "pa_model", "name": "Model", "type": "select"},
    {"slug": "pa_motor", "name": "Motorizácia", "type": "select"},
    {"slug": "pa_rok_od", "name": "Rok od", "type": "select"},
    {"slug": "pa_rok_do", "name": "Rok do", "type": "select"},
)

# Alias → kanonický make z hierarchie
MAKE_ALIASES = {
    "vw": "Volkswagen",
    "volkswagen": "Volkswagen",
    "skoda": "Škoda",
    "škoda": "Škoda",
    "mercedes": "Mercedes-Benz",
    "mercedes benz": "Mercedes-Benz",
    "mercedes-benz": "Mercedes-Benz",
    "mb": "Mercedes-Benz",
    "citroen": "Citroën",
    "citroën": "Citroën",
    "seat": "Seat",
    "bmw": "BMW",
    "audi": "Audi",
    "ford": "Ford",
    "opel": "Opel",
    "peugeot": "Peugeot",
    "renault": "Renault",
    "hyundai": "Hyundai",
    "kia": "Kia",
    "toyota": "Toyota",
    "fiat": "Fiat",
    "alfa": "Alfa Romeo",
    "alfa romeo": "Alfa Romeo",
    "alfaromeo": "Alfa Romeo",
    "jeep": "Jeep",
    "honda": "Honda",
    "dacia": "Dacia",
    "mazda": "Mazda",
    "nissan": "Nissan",
    "volvo": "Volvo",
}

# Voľný text / LKQ chassis → kanonický model (fold kľúč bez medzier).
# Verejné faktické generácie (G20 = 3 Series), nie cudzie vehicle ID.
_MODEL_TEXT_ALIASES: Dict[str, Dict[str, str]] = {
    "bmw": {
        "3er": "3 Series",
        "3erreiche": "3 Series",
        "3reihe": "3 Series",
        "3series": "3 Series",
        "3g20": "3 Series",
        "3f30": "3 Series",
        "g20": "3 Series",
        "g80": "3 Series",
        "g28": "3 Series",
        "f30": "3 Series",
        "f80": "3 Series",
        "e90": "3 Series",
        "e91": "3 Series",
        "e92": "3 Series",
        "5er": "5 Series",
        "5series": "5 Series",
        "f10": "5 Series",
        "g30": "5 Series",
        "1er": "1 Series",
        "1series": "1 Series",
        "f20": "1 Series",
        "f40": "1 Series",
    },
    "mercedes-benz": {
        "w205": "C-Class",
        "w206": "C-Class",
        "w213": "E-Class",
        "w177": "A-Class",
    },
    "peugeot": {
        # LKQ body kódy po „308 III …“ — ak ostane len roman+junk, mapuj na 308
        "iiifbfhfpf3fmhybridfhdgma": "308",
        "iiihybrid": "308",
    },
}

# Pozor: „Výrobca“ / „Manufacturer“ na B2B často = značka DIELU (Castrol/Bosch),
# nie auto — preto len explicitné vehicle-make kľúče (+ anglické make).
_ATTR_MAKE_KEYS = (
    "make", "Make", "Výrobca vozidla", "Vyrobca vozidla", "Výrobca auta",
    "Vyrobca auta", "VehicleMake", "CarMake", "Značka vozidla", "Znacka vozidla",
    "Vozidlo výrobca", "Vehicle manufacturer", "Car manufacturer", "Marke Fahrzeug",
)
_ATTR_MODEL_KEYS = (
    "model", "Model", "VehicleModel", "CarModel", "Typ", "Typ vozidla",
    "Model vozidla", "Modell", "Typové označenie", "Typove oznacenie",
)
_ATTR_ENGINE_KEYS = (
    "engine", "Engine", "Motor", "Motorizácia", "Motorizacia", "EngineCode",
    "Kód motora", "Kod motora", "Motorcode", "Motorkód", "Motorkod",
)
_ATTR_YEAR_FROM = (
    "year_from", "YearFrom", "Rok od", "RokOd", "from_year", "Baujahr von",
    "Year from", "Rok výroby od", "Rok vyroby od",
)
_ATTR_YEAR_TO = (
    "year_to", "YearTo", "Rok do", "RokDo", "to_year", "Baujahr bis",
    "Year to", "Rok výroby do", "Rok vyroby do",
)
_ATTR_YEAR_RANGE_KEYS = (
    "years", "Years", "Roky", "Rok výroby", "Rok vyroby", "Baujahr",
    "Year", "Model year", "Obdobie", "Obdobie výroby",
)
_ATTR_OE_KEYS = (
    "oe", "OE", "oe_numbers", "OEM", "OeNumber", "OENumber", "CrossNumber",
    "OE číslo", "OE cislo", "OEM číslo", "OEM cislo", "Orig. číslo",
    "Originálne číslo", "Originalne cislo", "OE-Nr", "OEM-Nr",
)
_ATTR_SUITABILITY_KEYS = (
    "suitability", "application", "applicability", "fitment", "vhodné pre",
    "vhodne pre", "použitie", "pouzitie", "kompatibilita", "vehicle",
    "vozidlo", "vozidlá", "vozidla", "použitie vozidlo",
)

# Polia, ktoré nikdy nepreberáme z feedov (cudzie vehicle DB)
_FOREIGN_VEHICLE_ID_KEYS = (
    "ktype", "kType", "ktypenr", "KTypeNr", "tecdoc_id", "tecDocId", "TecDocId",
    "linkage", "linkage_id", "LinkageId", "vehicle_id", "VehicleId", "VehicleID",
    "lkq_vehicle_id", "szekop_vehicle_id", "intercars_id", "autodoc_id",
    "herth_id", "genartnr", "vknzielnr", "GaragePath", "garage_path",
    "catalog_id", "CatalogId", "ownId", "OwnId",
)

_FOREIGN_ID_VALUE_RE = re.compile(
    r"^(ktype|ktypenr|tecdoc|linkage)[:\-_ ]?\d+$",
    re.I,
)

# Badge / UI atribúty LKQ (Text/Css), nie Name/Value fitment
_LKQ_BADGE_ATTR_RE = re.compile(r"^Attributes_", re.I)

# MM/YY alebo YYYY rozsahy v slugoch / textoch
_YEAR_RANGE_RE = re.compile(
    r"(?P<f>(?:19|20)\d{2}|\d{2}/\d{2})\s*[-–—]\s*(?P<t>(?:19|20)\d{2}|\d{2}/\d{2})?",
)
_SUITABILITY_LINE_RE = re.compile(
    r"(?i)(?:vhodn[ée]\s+pre|použitie|kompatibilita|application|fits?|for)\s*[:\-]?\s*(.+)$"
)


def load_vehicle_hierarchy(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or HIERARCHY_PATH
    if not p.is_file():
        return {"makes": {}, "make_index": {}, "version": 0}
    return json.loads(p.read_text(encoding="utf-8"))


def starter_makes() -> List[str]:
    data = load_vehicle_hierarchy()
    return list((data.get("makes") or {}).keys())


STARTER_MAKES = starter_makes()  # spätná kompatibilita


def normalize_make(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    key = re.sub(r"\s+", " ", s).lower()
    if key in MAKE_ALIASES:
        return MAKE_ALIASES[key]
    data = load_vehicle_hierarchy()
    # aliasy z make_index
    for make, meta in (data.get("make_index") or {}).items():
        if make.lower() == key:
            return make
        for al in meta.get("aliases") or []:
            if str(al).lower() == key:
                return make
    for make in starter_makes():
        if make.lower() == key:
            return make
    return s


def models_for_make(make: str) -> List[str]:
    makes = (load_vehicle_hierarchy().get("makes") or {})
    canon = normalize_make(make)
    block = makes.get(canon) or makes.get(make) or {}
    return list(block.keys())


def engines_for(make: str, model: str) -> List[str]:
    makes = (load_vehicle_hierarchy().get("makes") or {})
    canon = normalize_make(make)
    block = (makes.get(canon) or {}).get(model) or {}
    return list(block.get("engines") or [])


def year_range_for(make: str, model: str) -> Tuple[Optional[int], Optional[int]]:
    makes = (load_vehicle_hierarchy().get("makes") or {})
    canon = normalize_make(make)
    block = (makes.get(canon) or {}).get(model) or {}
    yf = block.get("year_from")
    yt = block.get("year_to")
    return (int(yf) if yf else None, int(yt) if yt else None)


def sg_model_id(make: str, model: str) -> str:
    """Kanonické SmartGaráž ID modelu (sgv-model-…); prázdne ak nie je v strome."""
    makes = (load_vehicle_hierarchy().get("makes") or {})
    canon = normalize_make(make)
    meta = (makes.get(canon) or {}).get(model) or {}
    return str(meta.get("sg_id") or "")


def lookup_by_sg_id(sg_id: str) -> Dict[str, Any]:
    """sgv-model-* / sgv-gen-* → {make, model, sg_id, engines, year_from, year_to, generation?}."""
    sid = str(sg_id or "").strip()
    if not sid.startswith("sgv-"):
        return {}
    makes = load_vehicle_hierarchy().get("makes") or {}
    for make, models in makes.items():
        if not isinstance(models, dict):
            continue
        for model, meta in models.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("sg_id") or "") == sid:
                return {
                    "make": make,
                    "model": model,
                    "sg_id": sid,
                    "engines": list(meta.get("engines") or []),
                    "year_from": meta.get("year_from"),
                    "year_to": meta.get("year_to"),
                }
            for g in meta.get("generations") or []:
                if not isinstance(g, dict):
                    continue
                if str(g.get("sg_id") or "") == sid:
                    return {
                        "make": make,
                        "model": model,
                        "sg_id": sid,
                        "generation": str(g.get("label") or ""),
                        "engines": list(meta.get("engines") or []),
                        "year_from": g.get("year_from") or meta.get("year_from"),
                        "year_to": g.get("year_to") or meta.get("year_to"),
                    }
    return {}


def normalize_vehicle_pair(
    make: str = "",
    model: str = "",
    engine: str = "",
    year_from: Any = None,
    year_to: Any = None,
    sg_id: str = "",
) -> Dict[str, Any]:
    """Jeden overený pár make+model (cez SG hierarchiu). Prázdne ak nejde namapovať.

    Explicitné ``sgv-gen-*`` / platné ``sgv-*`` ID sa **neprepisujú** modelovým ID.
    """
    mapped = map_to_sg_hierarchy(make, model, engine)
    canon_make = mapped.get("make") or ""
    canon_model = mapped.get("model") or ""
    incoming = str(sg_id or "").strip()
    # Preferuj gen/explicit sgv-* pred modelovým remapom z hierarchie.
    if incoming.startswith("sgv-gen-") or (
        incoming.startswith("sgv-") and lookup_by_sg_id(incoming)
    ):
        canon_sg = incoming
    else:
        canon_sg = mapped.get("sg_id") or incoming or ""
    if not canon_make or not canon_model:
        if canon_sg:
            hit = lookup_by_sg_id(canon_sg)
            if hit:
                canon_make = hit["make"]
                canon_model = hit["model"]
                # zachovaj gen id ak prišlo; inak model
                if not incoming.startswith("sgv-gen-"):
                    canon_sg = hit["sg_id"]
    if not canon_make or not canon_model:
        return {}
    if not canon_sg:
        canon_sg = sg_model_id(canon_make, canon_model)
    eng = mapped.get("engine") or ""
    if engine and not eng:
        # nechaj voľný text motora len ak je v zozname modelu (fold)
        for e in engines_for(canon_make, canon_model):
            if _fold_model_key(e) == _fold_model_key(engine):
                eng = e
                break
    yf = _as_year(year_from)
    yt = _as_year(year_to)
    # Pri gen ID ber year range z generácie
    if incoming.startswith("sgv-gen-"):
        hit = lookup_by_sg_id(incoming)
        if hit:
            if yf is None:
                yf = _as_year(hit.get("year_from"))
            if yt is None:
                yt = _as_year(hit.get("year_to"))
    else:
        hy_f, hy_t = year_range_for(canon_make, canon_model)
        if yf is None:
            yf = _as_year(hy_f)
        if yt is None:
            yt = _as_year(hy_t)
    return {
        "make": canon_make,
        "model": canon_model,
        "engine": eng,
        "year_from": yf,
        "year_to": yt,
        "sg_id": canon_sg,
    }


def merge_vehicle_lists(*lists: Any) -> List[Dict[str, Any]]:
    """Union párov podľa sg_id (prvý výskyt; engine/years sa zjednotia)."""
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for lst in lists:
        if not lst:
            continue
        for raw in lst:
            if not isinstance(raw, dict):
                continue
            pair = normalize_vehicle_pair(
                make=str(raw.get("make") or ""),
                model=str(raw.get("model") or ""),
                engine=str(raw.get("engine") or ""),
                year_from=raw.get("year_from"),
                year_to=raw.get("year_to"),
                sg_id=str(raw.get("sg_id") or ""),
            )
            if not pair.get("sg_id"):
                continue
            sid = pair["sg_id"]
            if sid not in by_id:
                by_id[sid] = pair
                order.append(sid)
                continue
            cur = by_id[sid]
            cur["engine"] = pipe_union_values(cur.get("engine"), pair.get("engine"))
            yf = [y for y in (_as_year(cur.get("year_from")), _as_year(pair.get("year_from"))) if y]
            yt = [y for y in (_as_year(cur.get("year_to")), _as_year(pair.get("year_to"))) if y]
            if yf:
                cur["year_from"] = min(yf)
            if yt:
                cur["year_to"] = max(yt)
    return [by_id[s] for s in order]


def vehicles_from_sg_id_pipe(
    sg_id_pipe: str,
    engine_pipe: str = "",
    year_from: Any = None,
    year_to: Any = None,
) -> List[Dict[str, Any]]:
    """Z `_sg_fit_sg_id` pipe zlož overené páry (zdroj pravdy pri repair)."""
    engines = [e for e in str(engine_pipe or "").split("|") if e.strip()]
    out: List[Dict[str, Any]] = []
    for sid in str(sg_id_pipe or "").split("|"):
        sid = sid.strip()
        if not sid:
            continue
        hit = lookup_by_sg_id(sid)
        if not hit:
            continue
        # motory: prienik pipe ∩ hierarchie modelu (nie cudzie motory)
        allowed = {_fold_model_key(e) for e in (hit.get("engines") or [])}
        eng_keep = []
        for e in engines:
            if _fold_model_key(e) in allowed:
                eng_keep.append(e)
        pair = normalize_vehicle_pair(
            make=hit["make"],
            model=hit["model"],
            engine="|".join(eng_keep),
            year_from=year_from if year_from is not None else hit.get("year_from"),
            year_to=year_to if year_to is not None else hit.get("year_to"),
            sg_id=hit["sg_id"],
        )
        if pair:
            out.append(pair)
    return merge_vehicle_lists(out)


def parse_vehicles_meta(raw: Any) -> List[Dict[str, Any]]:
    """Načítaj `_sg_fit_vehicles` JSON (alebo list)."""
    if not raw:
        return []
    if isinstance(raw, list):
        return merge_vehicle_lists(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return merge_vehicle_lists(data)
    return []


def vehicles_from_product_meta(meta_data: Any) -> List[Dict[str, Any]]:
    """Preferuj JSON páry; fallback = sg_id pipe (nie chaotický make×model)."""
    flat: Dict[str, Any] = {}
    for row in meta_data or []:
        if isinstance(row, dict) and row.get("key"):
            flat[str(row["key"])] = row.get("value")
    vehicles = parse_vehicles_meta(flat.get("_sg_fit_vehicles"))
    if vehicles:
        return vehicles
    from_ids = vehicles_from_sg_id_pipe(
        str(flat.get("_sg_fit_sg_id") or ""),
        engine_pipe=str(flat.get("_sg_fit_engine") or ""),
        year_from=flat.get("_sg_fit_year_from"),
        year_to=flat.get("_sg_fit_year_to"),
    )
    if from_ids:
        return from_ids
    # Jedno auto bez sg_id (staré meta): make+model bez pipe / rovnaký počet pipe častí
    makes = [x for x in str(flat.get("_sg_fit_make") or "").split("|") if x.strip()]
    models = [x for x in str(flat.get("_sg_fit_model") or "").split("|") if x.strip()]
    engines = [x for x in str(flat.get("_sg_fit_engine") or "").split("|") if x.strip()]
    if len(makes) == 1 and len(models) == 1:
        pair = normalize_vehicle_pair(
            make=makes[0],
            model=models[0],
            engine=engines[0] if engines else "",
            year_from=flat.get("_sg_fit_year_from"),
            year_to=flat.get("_sg_fit_year_to"),
        )
        return [pair] if pair else []
    # Zarovnané pipe (make[i]+model[i]) — len ak rovnaký počet (inak chaos → ignoruj)
    if makes and models and len(makes) == len(models):
        pairs = []
        for i, mk in enumerate(makes):
            pair = normalize_vehicle_pair(
                make=mk,
                model=models[i],
                engine=engines[i] if i < len(engines) else (engines[0] if engines else ""),
                year_from=flat.get("_sg_fit_year_from"),
                year_to=flat.get("_sg_fit_year_to"),
            )
            if pair:
                pairs.append(pair)
        return merge_vehicle_lists(pairs)
    return []


def vehicles_to_meta(
    vehicles: List[Dict[str, Any]],
    oe_numbers: Optional[List[str]] = None,
    oe_makes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Zapíš páry + odvodené pipe polia (make/model vždy z párov, 1:1 poradie)."""
    clean = merge_vehicle_lists(vehicles)
    meta: List[Dict[str, Any]] = []
    if clean:
        meta.append(
            {
                "key": "_sg_fit_vehicles",
                "value": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
            }
        )
        meta.append(
            {"key": "_sg_fit_make", "value": "|".join(v["make"] for v in clean)}
        )
        meta.append(
            {"key": "_sg_fit_model", "value": "|".join(v["model"] for v in clean)}
        )
        engines = pipe_union_values(*(v.get("engine") for v in clean))
        if engines:
            meta.append({"key": "_sg_fit_engine", "value": engines})
        meta.append(
            {"key": "_sg_fit_sg_id", "value": "|".join(v["sg_id"] for v in clean if v.get("sg_id"))}
        )
        yf = [v["year_from"] for v in clean if v.get("year_from") is not None]
        yt = [v["year_to"] for v in clean if v.get("year_to") is not None]
        if yf:
            meta.append({"key": "_sg_fit_year_from", "value": str(min(yf))})
        if yt:
            meta.append({"key": "_sg_fit_year_to", "value": str(max(yt))})
    if oe_numbers:
        meta.append({"key": "_sg_oe_numbers", "value": "|".join(oe_numbers)})
    if oe_makes:
        clean_makes = []
        seen = set()
        for m in oe_makes:
            mk = normalize_make(str(m or "").strip())
            if not mk or mk in seen:
                continue
            seen.add(mk)
            clean_makes.append(mk)
        if clean_makes:
            meta.append({"key": "_sg_oe_makes", "value": "|".join(clean_makes)})
    return meta


def strip_foreign_vehicle_ids(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Odstráň TecDoc/LKQ/… vehicle ID kľúče z attributes pred mapovaním."""
    if not isinstance(attrs, dict):
        return {}
    out: Dict[str, Any] = {}
    banned = {k.lower() for k in _FOREIGN_VEHICLE_ID_KEYS}
    for k, v in attrs.items():
        if str(k).lower() in banned:
            continue
        if isinstance(v, str) and _FOREIGN_ID_VALUE_RE.match(v.strip()):
            continue
        out[k] = v
    return out


def sanitize_inbound_fitment(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Bezpečný vstup z dodávateľa → len voľný text + OE, žiadne cudzie vehicle ID."""
    if not isinstance(raw, dict):
        return {
            "make": "",
            "model": "",
            "engine": "",
            "year_from": None,
            "year_to": None,
            "oe_numbers": [],
            "attributes": {},
        }
    # Top-level + attributes: drop TecDoc/supplier vehicle keys
    cleaned = strip_foreign_vehicle_ids(raw)
    attrs = strip_foreign_vehicle_ids(cleaned.get("attributes") or {})
    return {
        "make": cleaned.get("make") or "",
        "model": cleaned.get("model") or "",
        "engine": cleaned.get("engine") or "",
        "year_from": cleaned.get("year_from"),
        "year_to": cleaned.get("year_to"),
        "oe_numbers": cleaned.get("oe_numbers") or [],
        "attributes": attrs,
    }


def _fold_model_key(s: str) -> str:
    """Porovnanie modelov: pomlčky/medzery/case ignoruj (C-Class ≈ c class)."""
    return re.sub(r"[\s\-_/]+", "", (s or "").strip().lower())


def _alias_model_for_make(canon_make: str, model: str) -> str:
    """Chassis / marketing text → kanonický model z hierarchie (ak známy)."""
    if not canon_make or not model:
        return ""
    make_fold = _fold_model_key(canon_make)
    aliases = _MODEL_TEXT_ALIASES.get(make_fold) or {}
    if not aliases:
        return ""
    raw_fold = _fold_model_key(model)
    if raw_fold in aliases:
        return aliases[raw_fold]
    # tokeny (G20 G80 G28 / 3 (g20) / 3er-reihe)
    tokens = re.findall(r"[a-z0-9]+", (model or "").lower())
    for tok in tokens:
        if tok in aliases:
            return aliases[tok]
    # connected forms: 3g20, 3f30
    compact = "".join(tokens)
    if compact in aliases:
        return aliases[compact]
    for key, canon in aliases.items():
        if len(key) >= 3 and key in raw_fold:
            return canon
    return ""


def map_to_sg_hierarchy(make: str, model: str, engine: str = "") -> Dict[str, str]:
    """Namapuj voľný text na našu hierarchiu; engine nechaj len ak je v zozname."""
    canon_make = normalize_make(make)
    models = models_for_make(canon_make)
    canon_model = ""
    if model:
        ml = model.strip().lower()
        ml_fold = _fold_model_key(ml)
        for m in models:
            if m.lower() == ml or _fold_model_key(m) == ml_fold:
                canon_model = m
                break
        if not canon_model:
            # jemný contains match (Golf VII → Golf; A4 B8 → A4)
            for m in models:
                mf = _fold_model_key(m)
                if mf and (mf in ml_fold or m.lower() in ml or ml in m.lower()):
                    canon_model = m
                    break
        if not canon_model:
            aliased = _alias_model_for_make(canon_make, model)
            if aliased and aliased in models:
                canon_model = aliased
            elif aliased:
                # alias môže byť canonical názov aj keď fold sedí
                for m in models:
                    if _fold_model_key(m) == _fold_model_key(aliased):
                        canon_model = m
                        break
    canon_engine = ""
    if canon_make and canon_model and engine:
        eng_raw = engine.strip()
        eng_fold = _fold_model_key(eng_raw)
        for e in engines_for(canon_make, canon_model):
            if e.lower() == eng_raw.lower() or _fold_model_key(e) == eng_fold:
                canon_engine = e
                break
            # Mercedes slug „C 220 d“ ↔ hierarchia C220d
            if eng_fold and eng_fold.replace(" ", "") == _fold_model_key(e):
                canon_engine = e
                break
    return {
        "make": canon_make if canon_make in (load_vehicle_hierarchy().get("makes") or {}) else "",
        "model": canon_model,
        "engine": canon_engine,
        "sg_id": sg_model_id(canon_make, canon_model) if canon_model else "",
    }


def generations_for(make: str, model: str) -> List[Dict[str, Any]]:
    """Zoznam generácií {label, sg_id, year_from, year_to, engines} pre model."""
    makes = load_vehicle_hierarchy().get("makes") or {}
    canon = normalize_make(make)
    block = (makes.get(canon) or {}).get(model) or {}
    gens = block.get("generations") or []
    return [g for g in gens if isinstance(g, dict) and g.get("sg_id")]


def map_generation(
    make: str,
    model: str,
    generation: str = "",
    year: Optional[int] = None,
) -> Dict[str, str]:
    """Namapuj generation label (III / B8) alebo rok → ``sgv-gen-*``.

    Bez TecDoc ID. Ak nič nesedí, vráti prázdne ``sg_gen_id``.
    """
    mapped = map_to_sg_hierarchy(make, model, "")
    canon_make = mapped.get("make") or ""
    canon_model = mapped.get("model") or ""
    empty = {
        "make": canon_make,
        "model": canon_model,
        "generation": "",
        "sg_gen_id": "",
        "sg_id": mapped.get("sg_id") or "",
    }
    if not canon_make or not canon_model:
        return empty
    gens = generations_for(canon_make, canon_model)
    if not gens:
        return empty

    gen_raw = (generation or "").strip()
    # "Octavia III" / "III" / "3" / "mk3"
    if not gen_raw and (model or ""):
        m = re.search(
            r"\b(I{1,3}|IV|V|VI|VII|VIII|IX|X|B\d|8[A-Z]|G\d{2}|W\d{3}|mk\s*\d+)\b",
            str(model),
            re.I,
        )
        if m:
            gen_raw = m.group(1)

    if gen_raw:
        fold = _fold_model_key(gen_raw)
        # numeric → roman lite
        num_map = {
            "1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v",
            "6": "vi", "7": "vii", "8": "viii",
        }
        if fold in num_map:
            fold = num_map[fold]
        fold = re.sub(r"^mk", "", fold)
        for g in gens:
            label = str(g.get("label") or "")
            if _fold_model_key(label) == fold:
                return {
                    "make": canon_make,
                    "model": canon_model,
                    "generation": label,
                    "sg_gen_id": str(g.get("sg_id") or ""),
                    "sg_id": mapped.get("sg_id") or "",
                }
            # sg_id tail match …-octavia-iii
            sid = str(g.get("sg_id") or "")
            if fold and sid.endswith("-" + fold):
                return {
                    "make": canon_make,
                    "model": canon_model,
                    "generation": label,
                    "sg_gen_id": sid,
                    "sg_id": mapped.get("sg_id") or "",
                }

    y = None
    try:
        if year not in (None, ""):
            y = int(year)
    except (TypeError, ValueError):
        y = None
    if y:
        # Presnosť: rok musí jednoznačne patriť jednej generácii (F48 2015–22
        # vs U11 2022–26 → rok 2022 je nejednoznačný → žiadna generácia).
        cands = []
        for g in gens:
            try:
                yf_i = int(g.get("year_from")) if g.get("year_from") else None
                yt_i = int(g.get("year_to")) if g.get("year_to") else None
            except (TypeError, ValueError):
                continue
            if (yf_i is None or y >= yf_i) and (yt_i is None or y <= yt_i):
                cands.append(g)
        if len(cands) != 1:
            return empty
        gens = cands
        for g in gens:
            yf = g.get("year_from")
            yt = g.get("year_to")
            try:
                yf_i = int(yf) if yf else None
                yt_i = int(yt) if yt else None
            except (TypeError, ValueError):
                continue
            if yf_i is not None and y < yf_i:
                continue
            if yt_i is not None and y > yt_i:
                continue
            return {
                "make": canon_make,
                "model": canon_model,
                "generation": str(g.get("label") or ""),
                "sg_gen_id": str(g.get("sg_id") or ""),
                "sg_id": mapped.get("sg_id") or "",
            }

    return empty



def _pick(attrs: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in attrs and attrs[k] not in (None, ""):
            return attrs[k]
    # case-insensitive
    lower = {str(k).lower(): v for k, v in attrs.items()}
    for k in keys:
        v = lower.get(str(k).lower())
        if v not in (None, ""):
            return v
    return None


def _as_year(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        n = int(str(val).strip()[:4])
        if 1970 <= n <= 2100:
            return n
    except (TypeError, ValueError):
        pass
    m = re.search(r"(19|20)\d{2}", str(val))
    if m:
        return int(m.group(0))
    return None


def _as_oe_list(val: Any) -> List[str]:
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).replace(";", "|").replace(",", "|")
    return [p.strip() for p in s.split("|") if p.strip()]


def normalize_oe_code(code: str) -> str:
    """Zjednotí OE/cross kód na veľké písmená bez zbytočných medzier."""
    s = re.sub(r"\s+", "", (code or "").strip().upper())
    s = s.replace("_", "").replace(".", "")
    return s


def merge_oe_numbers(*groups: Any, exclude: Optional[List[str]] = None) -> List[str]:
    """Spojí viacero zoznamov OE, deduplikuje (case-insensitive), zachová poradie."""
    exclude_n = {normalize_oe_code(x) for x in (exclude or []) if x}
    seen = set()
    out: List[str] = []
    for group in groups:
        for raw in _as_oe_list(group):
            key = normalize_oe_code(raw)
            if not key or key in seen or key in exclude_n:
                continue
            if len(key) < 5 or len(key) > 24:
                continue
            seen.add(key)
            # ulož čitateľnú formu (pôvod bez prebytku medzier)
            out.append(re.sub(r"\s+", " ", raw.strip().upper()))
    return out


def oe_numbers_to_meta_value(oe_numbers: List[str]) -> str:
    return "|".join(oe_numbers)


def _mmYY_to_year(token: str) -> Optional[int]:
    """'08/12' → 2012, '10/21' → 2021, '2013' → 2013."""
    t = (token or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"(\d{2})/(\d{2})", t)
    if m:
        yy = int(m.group(2))
        return 1900 + yy if yy >= 70 else 2000 + yy
    return _as_year(t)


def _parse_year_range(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None
    m = _YEAR_RANGE_RE.search(str(text))
    if not m:
        return _as_year(text), None
    return _mmYY_to_year(m.group("f")), _mmYY_to_year(m.group("t") or "")


def _known_make_tokens() -> List[Tuple[str, str]]:
    """[(alias_or_name_lower_slug, canonical_make), ...] longest first."""
    pairs: List[Tuple[str, str]] = []
    for make in starter_makes():
        pairs.append((re.sub(r"[^a-z0-9]+", "-", make.lower()).strip("-"), make))
        pairs.append((make.lower(), make))
    for alias, canon in MAKE_ALIASES.items():
        pairs.append((re.sub(r"[^a-z0-9]+", "-", alias.lower()).strip("-"), canon))
        pairs.append((alias.lower(), canon))
    data = load_vehicle_hierarchy()
    for make, meta in (data.get("make_index") or {}).items():
        for al in meta.get("aliases") or []:
            pairs.append((re.sub(r"[^a-z0-9]+", "-", str(al).lower()).strip("-"), make))
            pairs.append((str(al).lower(), make))
    # unique, longest token first (volkswagen pred vw)
    seen = set()
    out: List[Tuple[str, str]] = []
    for tok, canon in sorted(pairs, key=lambda x: len(x[0]), reverse=True):
        if not tok or tok in seen:
            continue
        seen.add(tok)
        out.append((tok, canon))
    return out


def normalize_attributes_map(raw_attrs: Any) -> Dict[str, Any]:
    """Zjednoť Name/Value, list-of-dicts aj badge LKQ atribúty na flat dict.

    LKQ list item často má Attributes=[] (oleje) alebo badge
    ``{Text, Css}`` (Novinka) — tie nie sú fitment. Name/Value páry áno.
    """
    out: Dict[str, Any] = {}
    if not raw_attrs:
        return out
    if isinstance(raw_attrs, dict):
        return strip_foreign_vehicle_ids(raw_attrs)
    if not isinstance(raw_attrs, list):
        return out
    for item in raw_attrs:
        if not isinstance(item, dict):
            continue
        # Badge: Text+Css bez Name/Value
        if item.get("Css") and _LKQ_BADGE_ATTR_RE.match(str(item.get("Css") or "")):
            continue
        name = item.get("Name") or item.get("name") or item.get("Key") or item.get("key")
        value = item.get("Value") if "Value" in item else item.get("value")
        if name and value not in (None, ""):
            out[str(name).strip()] = value
            continue
        # Niekedy je Text="Výrobca: VW" bez Name
        text = str(item.get("Text") or "").strip()
        if text and ":" in text and not item.get("Css"):
            k, _, v = text.partition(":")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return strip_foreign_vehicle_ids(out)


def extract_oe_from_lkq_codes(payload: Any) -> List[str]:
    """POST /Product/Codes/{id} → ``{\"Oem\":[{\"Manufacturer\",\"Codes\":[...]}]}``.

    Berieme len čísla dielov (OE), nie Manufacturer ako vehicle taxonomy ID.
    Pri olejoch býva Oem=[] — správne prázdne.
    """
    if not payload:
        return []
    if isinstance(payload, dict):
        rows = payload.get("Oem") or payload.get("oem") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    oes: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        codes = row.get("Codes") or row.get("codes") or []
        if isinstance(codes, str):
            codes = [codes]
        for c in codes:
            s = re.sub(r"\s+", " ", str(c or "")).strip()
            if s and not _FOREIGN_ID_VALUE_RE.match(s):
                oes.append(s)
    # unique preserve order — normalizuj medzery (5Q0 819 669 == 5Q0819669)
    seen = set()
    out = []
    for oe in oes:
        key = re.sub(r"\s+", "", oe).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(oe)
    return out


# LKQ Oem.Manufacturer, ktoré NIE sú osobné autá v našej hierarchii
_OEM_MAKE_SKIP = {
    "man",
    "vag",  # skupina, nie modelová značka — VW/Audi/Seat/Škoda sú zvlášť
    "iveco",
    "scania",
    "daf",
    "volvo trucks",
    "renault trucks",
}


def extract_oem_makes_from_lkq_codes(payload: Any) -> List[str]:
    """Z LKQ Codes vytiahni značky áut (Oem.Manufacturer) → SG make.

    Policy-safe: len voľný text značky namapovaný na našu hierarchiu.
    MAN / VAG / truck značky sa zahodia. Výsledok = kandidáti na
    `_sg_fit_make` union (modely stále z Catalog slug / OE súrodencov).
    """
    if not payload:
        return []
    if isinstance(payload, dict):
        rows = payload.get("Oem") or payload.get("oem") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    hierarchy_makes = set((load_vehicle_hierarchy().get("makes") or {}).keys())
    out: List[str] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("Manufacturer") or row.get("manufacturer") or "").strip()
        if not raw:
            continue
        if raw.lower() in _OEM_MAKE_SKIP:
            continue
        canon = normalize_make(raw)
        if not canon or canon not in hierarchy_makes:
            continue
        if canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return out


def fitment_dict_from_meta(meta_data: Any) -> Dict[str, Any]:
    """WC meta_data → fitment dict pre union."""
    flat: Dict[str, Any] = {}
    for row in meta_data or []:
        if isinstance(row, dict) and row.get("key"):
            flat[str(row["key"])] = row.get("value")
    return {
        "make": str(flat.get("_sg_fit_make") or "").strip(),
        "model": str(flat.get("_sg_fit_model") or "").strip(),
        "engine": str(flat.get("_sg_fit_engine") or "").strip(),
        "sg_id": str(flat.get("_sg_fit_sg_id") or "").strip(),
        "year_from": _as_year(flat.get("_sg_fit_year_from")),
        "year_to": _as_year(flat.get("_sg_fit_year_to")),
        "oe_numbers": [
            x for x in str(flat.get("_sg_oe_numbers") or "").split("|") if x.strip()
        ],
    }


def union_fitment_dicts(*parts: Dict[str, Any]) -> Dict[str, Any]:
    """Union make/model/engine/sg_id/OE (pipe) + min/max years — multi-vehicle."""
    makes = pipe_union_values(*(p.get("make") for p in parts if p))
    models = pipe_union_values(*(p.get("model") for p in parts if p))
    engines = pipe_union_values(*(p.get("engine") for p in parts if p))
    sg_ids = pipe_union_values(*(p.get("sg_id") for p in parts if p))
    oes = merge_oe_numbers(*(p.get("oe_numbers") for p in parts if p))
    y_from = [y for y in (_as_year(p.get("year_from")) for p in parts if p) if y is not None]
    y_to = [y for y in (_as_year(p.get("year_to")) for p in parts if p) if y is not None]
    return {
        "make": makes,
        "model": models,
        "engine": engines,
        "sg_id": sg_ids,
        "oe_numbers": oes,
        "year_from": min(y_from) if y_from else None,
        "year_to": max(y_to) if y_to else None,
    }


_OIL_SPEC_OE_RE = re.compile(
    r"(?i)^(?:"
    r"wss[\-_]?m2c|"  # Ford WSS-M2C…
    r"acea\b|api\b|sae\b|"
    r"\d{3}\.\d{2}$|"  # VW 508.00 / 509.00 oil approvals
    r"ms\s*\d+"  # Chrysler MS 6395 atď.
    r")"
)


def _looks_like_oil_spec(code: str) -> bool:
    s = (code or "").strip()
    if not s:
        return True
    if _OIL_SPEC_OE_RE.search(s):
        return True
    # „VW 508.00“ / „MB 229.52“
    if re.search(r"(?i)\b\d{3}\.\d{2}\b", s):
        return True
    return False


def extract_oe_from_szekop_html(html: str) -> List[str]:
    """HTML z GetProductDetailOENumbers — len čísla pri známych značkách áut.

    Olejové „OE“ tabuľky často obsahujú špecifikácie (WSS-M2C…, VW 508.00,
    ACEA…) — tie zámerne zahodíme. Ostávajú reálne OE dielov.
    """
    if not html:
        return []
    # Lazy: regex bez BS4 dependency v unit testoch
    pairs = re.findall(
        r'flex-manufacturer[^>]*>([^<]+)</span>\s*<span[^>]*flex-number[^>]*>([^<]+)</span>',
        html,
        re.I,
    )
    if not pairs:
        pairs = re.findall(
            r'flex-number[^>]*>([^<]+)</span>',
            html,
            re.I,
        )
        pairs = [("", p) for p in pairs]
    car_makes = {m.lower() for m, _ in _known_make_tokens()}
    car_makes.update({"vw", "vag", "skoda", "škoda", "mercedes", "citroen", "citroën", "mb"})
    oes: List[str] = []
    for manuf, num in pairs:
        m = (manuf or "").strip()
        n = re.sub(r"\s+", " ", (num or "").strip())
        if not n or _looks_like_oil_spec(n):
            continue
        # Ak je manufacturer prítomný a nie je auto-značka, skip (olejové specs)
        if m and m.lower() not in car_makes and normalize_make(m).lower() not in car_makes:
            if normalize_make(m) not in (load_vehicle_hierarchy().get("makes") or {}):
                continue
        if _FOREIGN_ID_VALUE_RE.match(n):
            continue
        oes.append(n)
    seen = set()
    out = []
    for oe in oes:
        k = oe.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(oe)
    return out


def parse_vehicle_suitability_text(text: str) -> Dict[str, Any]:
    """Opatrný regex parser voľného textu vhodnosti — nič nevymýšľa.

    Príklady, ktoré rozpozná:
      - ``VW Golf VII 1.6 TDI 2013-2020``
      - ``vhodné pre: Škoda Octavia 2.0 TDI (2013–2020)``
      - ``Volkswagen / Golf / 1.9 TDI / 2004-2008``
    """
    empty = {
        "make": "", "model": "", "engine": "",
        "year_from": None, "year_to": None,
    }
    if not text or not str(text).strip():
        return empty
    blob = str(text).strip()
    # Preferuj explicitný suitability riadok
    for line in blob.splitlines():
        m = _SUITABILITY_LINE_RE.search(line.strip())
        if m:
            blob = m.group(1).strip()
            break
    # Zober prvý riadok / segment
    blob = re.split(r"[;\n\|]", blob)[0].strip()
    blob = blob.replace("/", " ").replace(",", " ")
    blob = re.sub(r"[()\[\]]", " ", blob)
    blob = re.sub(r"\s+", " ", blob).strip()
    if not blob:
        return empty

    yf, yt = _parse_year_range(blob)
    blob_no_years = _YEAR_RANGE_RE.sub(" ", blob)
    blob_no_years = re.sub(r"\s+", " ", blob_no_years).strip()

    make = ""
    rest = blob_no_years
    lower = blob_no_years.lower()
    for tok, canon in _known_make_tokens():
        # token ako celé slová / začiatok
        if " " in tok or "-" in tok:
            if lower.startswith(tok + " ") or lower == tok:
                make = canon
                rest = blob_no_years[len(tok):].strip(" -/")
                break
        else:
            m = re.match(rf"(?i)\b{re.escape(tok)}\b\s*(.*)$", blob_no_years)
            if m:
                make = canon
                rest = (m.group(1) or "").strip(" -/")
                break
    if not make:
        return empty

    # Engine: 1.6 TDI / 2.0 CDI / 1.4 TSI …
    eng_m = re.search(
        r"\b(\d(?:[.,]\d)?\s*[A-Za-z]{3,4}\b|\d(?:[.,]\d)\s*TDI|\d(?:[.,]\d)\s*TSI|"
        r"\d(?:[.,]\d)\s*CDI|\d(?:[.,]\d)\s*HDi|\d(?:[.,]\d)\s*dCi)",
        rest,
        re.I,
    )
    engine = ""
    if eng_m:
        engine = re.sub(r"\s+", " ", eng_m.group(1)).strip()
        rest = (rest[: eng_m.start()] + " " + rest[eng_m.end():]).strip()

    model = re.sub(r"\s+", " ", rest).strip(" -/")
    # Odstráň čisto numerické zvyšky / ccm
    model = re.sub(r"\b\d{3,5}\s*ccm\b", "", model, flags=re.I).strip()
    model = re.sub(r"\b\d+\s*kW\b", "", model, flags=re.I).strip()
    model = re.sub(r"\s+", " ", model).strip(" -/")

    return {
        "make": make,
        "model": model,
        "engine": engine,
        "year_from": yf,
        "year_to": yt,
    }


def parse_fitment_from_catalog_slug(url_or_slug: str) -> Dict[str, Any]:
    """Voľný text make/model/engine/years z LKQ Catalog slug — bez path ID.

    Príklad slug:
      ``volkswagen-golf-vii-5g1-bq1-be1-be2-08-12-10-21-e-0ccm-85kw-…-filtre``
    Numeric path ``39849642;39850140;…`` sa zahodí.
    Olejové kategórie (``…-naplne-…``) bez značky auta → prázdne (správne).
    """
    empty = {
        "make": "", "model": "", "engine": "",
        "generation": "",
        "year_from": None, "year_to": None,
    }
    if not url_or_slug:
        return empty
    s = str(url_or_slug).strip()
    # drop query / fragment / numeric path tail
    s = s.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in s.split("/") if p]
    slug = ""
    for p in reversed(parts):
        if re.fullmatch(r"\d+(?:;\d+)*", p):
            continue  # foreign catalog path IDs — never keep
        if re.search(r"[A-Za-z]", p):
            slug = p
            break
    if not slug:
        return empty
    slug = slug.lower()
    for prefix in (
        "osobny-automobil-nahradne-diely-",
        "osobny-automobil-naplne-",
        "osobny-automobil-",
        "nakladne-vozidlo-nahradne-diely-",
        "nakladne-vozidlo-",
    ):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    # Náplne / chémia bez vehicle značky
    if any(x in slug for x in ("naplne", "motoroveho-oleja", "prevodoveho-oleja", "nemrznuca")):
        # môže ešte obsahovať značku auta? pri default oil path nie
        pass

    make = ""
    rest = slug
    for tok, canon in _known_make_tokens():
        tok_slug = tok.replace(" ", "-")
        if rest.startswith(tok_slug + "-") or rest == tok_slug:
            make = canon
            rest = rest[len(tok_slug):].lstrip("-")
            break
    if not make:
        return empty

    yf = yt = None
    # LKQ slug: ``08-12-10-21`` (= 08/12–10/21) alebo so slashmi ``08/12-10/21``
    ym = re.search(
        r"(?:^|-)(?:(\d{2})-(\d{2})-(\d{2})-(\d{2})|(\d{2}/\d{2})-(\d{2}/\d{2})?)(?:-|$)",
        rest,
    )
    if ym:
        if ym.group(1) is not None:
            yf = _mmYY_to_year(f"{ym.group(1)}/{ym.group(2)}")
            yt = _mmYY_to_year(f"{ym.group(3)}/{ym.group(4)}")
        else:
            yf = _mmYY_to_year(ym.group(5) or "")
            yt = _mmYY_to_year(ym.group(6) or "")
        rest = (rest[: ym.start()] + "-" + rest[ym.end():]).strip("-")

    engine = ""
    # 1-6-tdi / 2-0-tsi
    em = re.search(r"(?:^|-)(\d)-(\d)-([a-z]{3,4})(?:-|$)", rest)
    if em:
        engine = f"{em.group(1)}.{em.group(2)} {em.group(3).upper()}"
        rest = (rest[: em.start()] + "-" + rest[em.end():]).strip("-")
    else:
        # Mercedes-style: c-220-d / a-180-d / e-220-d (nie e-0ccm…)
        em_mb = re.search(
            r"(?:^|-)([a-z])-(\d{2,3})(?:-([a-z]))?(?:-|$)",
            rest,
        )
        if em_mb and em_mb.group(1) in {"a", "c", "e", "g", "s", "v"}:
            letter = em_mb.group(1).upper()
            num = em_mb.group(2)
            suffix = (em_mb.group(3) or "").lower()
            engine = f"{letter}{num}{suffix}" if suffix else f"{letter}{num}"
            rest = (rest[: em_mb.start()] + "-" + rest[em_mb.end():]).strip("-")
        else:
            # e-…ccm… (napr. e-golf / e-0ccm-85kw) — len label, žiadne ID
            em2 = re.search(r"(?:^|-)(e-[a-z0-9-]*?ccm[a-z0-9-]*)(?:-|$)", rest)
            if em2:
                engine = "e-Golf"
                rest = (rest[: em2.start()] + "-" + rest[em2.end():]).strip("-")

    # Otvorený interval rokov v slug-u: …-06-17-1-0-tsi… alebo …-06-17-cistenie…
    if yf is None:
        ym_open = re.search(r"(?:^|-)(\d{2})-(\d{2})(?:-(?:\d|-|[a-z])|$)", rest)
        if ym_open:
            # neber „1-0“ z motora — len MM-YY (mesiac 01–12)
            mm = int(ym_open.group(1))
            if 1 <= mm <= 12:
                yf = _mmYY_to_year(f"{ym_open.group(1)}/{ym_open.group(2)}")
                rest = (rest[: ym_open.start()] + "-" + rest[ym_open.end():]).strip("-")

    # Odstrihni ďalšie year range / ccm/kw/hp a product-type tail
    rest = re.sub(r"(?:^|-)\d{2}-\d{2}-\d{2}-\d{2}(?=-|$)", "-", rest)
    rest = re.sub(r"(?:^|-)(\d+ccm|\d+kw|\d+hp)(?=-|$)", "-", rest)
    # Mercedes chassis fragment po motore (205-014…) — nie model
    rest = re.sub(r"(?:^|-)\d{3}(?:-\d{3,})+(?=-|$)", "-", rest)
    rest = re.sub(
        r"(?:^|-)(motor|prevodovka|filtre|brzdovy|podvozok|diely|karoseria|"
        r"elektrosucast|nahradne|platnicky|tlmice|cistenie|ostrekovanie|"
        r"stierace|stieracie|skiel|pohon|zapalovanie|zhavenie).*$",
        "",
        rest,
    )
    tokens = [t for t in rest.split("-") if t]
    # drop chassis / body kódy (5g1, 1k1, be1, bq1…) — nie rímske číslice modelu
    _roman = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
    # Audi/Hyundai štýl: a3/q5/x3/i30 — digit v short tokene ≠ chassis
    # BMW g20/f30/e90 = generácia → alias na 3/5/1 Series (nie modelový kód)
    known_model_slugs = {
        re.sub(r"[^a-z0-9]+", "", m.lower())
        for m in models_for_make(make)
    }
    # BMW „3er“ / „5er“ / „1er“ (LKQ DE)
    _bmw_er = {"1er": "1 Series", "3er": "3 Series", "5er": "5 Series", "7er": "7 Series"}
    # Peugeot/Citroën body fragmenty po gen. označení
    _psa_body = {
        "fb", "fh", "fp", "f3", "fm", "fhdgma", "hn", "hnsw", "hns", "hybrid",
    }
    model_tokens: List[str] = []
    bmw_series_hint = ""
    for t in tokens:
        if make == "BMW" and t in _bmw_er:
            bmw_series_hint = _bmw_er[t]
            continue
        if make == "BMW" and t in {"reihe", "series", "er"}:
            continue
        if make == "Peugeot" and t in _psa_body:
            continue
        if t in _roman:
            model_tokens.append(t.upper())
            continue
        # Číselný model (208/308/3008/508) — NECHAJ ak je v hierarchii
        if re.fullmatch(r"\d{2,4}", t):
            if t in known_model_slugs:
                model_tokens.append(t)
            continue
        # Modelové kódy s číslicou: a3/a4/q5/x3/i30 — NECHAJ
        # BMW g20/f30/e90/w… = chassis → cez alias, nie ako „G20“ model
        if re.fullmatch(r"[a-z]\d{1,3}", t):
            if make == "BMW" and t[0] in "efgwu":
                aliased = _alias_model_for_make(make, t)
                if aliased:
                    bmw_series_hint = bmw_series_hint or aliased
                continue
            if make == "Mercedes-Benz" and t[0] == "w":
                continue
            model_tokens.append(t.upper() if len(t) <= 2 else t.title())
            continue
        if t in known_model_slugs or any(
            t == ks or ks.startswith(t) for ks in known_model_slugs if len(t) >= 2
        ):
            model_tokens.append(t)
            continue
        # Chassis / body: 5g1, bq1, be2, 8k2, nj3, aw1, w205, 8v1…
        if re.fullmatch(r"(?:\d[a-z]\d|[a-z]{2}\d|[a-z]\d{2,3})", t):
            if make == "BMW":
                aliased = _alias_model_for_make(make, t)
                if aliased:
                    bmw_series_hint = bmw_series_hint or aliased
            continue
        if re.fullmatch(r"[a-z0-9]{2,4}", t) and re.search(r"\d", t):
            if make == "BMW":
                aliased = _alias_model_for_make(make, t)
                if aliased:
                    bmw_series_hint = bmw_series_hint or aliased
            continue
        model_tokens.append(t)
    if bmw_series_hint and not model_tokens:
        model_tokens = [bmw_series_hint]
    elif bmw_series_hint and not any(
        _fold_model_key(x) == _fold_model_key(bmw_series_hint) for x in model_tokens
    ):
        model_tokens = [bmw_series_hint]
    model = " ".join(model_tokens).strip()
    model = re.sub(r"\s+", " ", model).strip()
    # Title-case slov, rímske + krátke kódy (A3, A4, B8) nechaj UPPER
    if model:
        parts = []
        for w in model.split():
            if w.upper() in {r.upper() for r in _roman}:
                parts.append(w.upper())
            elif re.fullmatch(r"[A-Za-z]\d{1,3}", w):
                parts.append(w.upper())
            elif w.lower() in {"series"} or w in {"1 Series", "3 Series", "5 Series", "7 Series"}:
                parts.append(w if " " in w else w.title())
            else:
                parts.append(w.title() if not w[:1].isdigit() else w)
        model = " ".join(parts)
        # „1 Series“ môže vzniknúť ako token „1“+„Series“ — zjednoť
        model = re.sub(
            r"\b([1357])\s+Series\b",
            lambda m: f"{m.group(1)} Series",
            model,
            flags=re.I,
        )

    # Finálne: chassis/alias → kanonický model z hierarchie
    generation = ""
    # Rímske / mk v model_tokens pred mapovaním (Octavia III → gen III)
    if model:
        gm = re.search(
            r"\b(I{1,3}|IV|V|VI|VII|VIII|IX|X|B\d|mk\s*\d+)\b",
            model,
            re.I,
        )
        if gm:
            generation = gm.group(1).upper().replace(" ", "")
            generation = re.sub(r"^MK", "", generation, flags=re.I)
    if make and model:
        mapped = map_to_sg_hierarchy(make, model, engine)
        if mapped.get("model"):
            model = mapped["model"]
            if mapped.get("engine"):
                engine = mapped["engine"]

    return {
        "make": make,
        "model": model,
        "engine": engine,
        "generation": generation,
        "year_from": yf,
        "year_to": yt,
    }


def extract_fitment_from_attributes(attrs: Any) -> Dict[str, Any]:
    """Robustný parser bežných attribute kľúčov → make/model/engine/years/OE."""
    flat = normalize_attributes_map(attrs)
    make = _pick(flat, _ATTR_MAKE_KEYS) or ""
    model = _pick(flat, _ATTR_MODEL_KEYS) or ""
    engine = _pick(flat, _ATTR_ENGINE_KEYS) or ""
    year_from = _pick(flat, _ATTR_YEAR_FROM)
    year_to = _pick(flat, _ATTR_YEAR_TO)
    if year_from in (None, "") or year_to in (None, ""):
        yr = _pick(flat, _ATTR_YEAR_RANGE_KEYS)
        if yr not in (None, ""):
            yf, yt = _parse_year_range(str(yr))
            year_from = year_from if year_from not in (None, "") else yf
            year_to = year_to if year_to not in (None, "") else yt
    oe = _pick(flat, _ATTR_OE_KEYS) or []
    suitability = _pick(flat, _ATTR_SUITABILITY_KEYS)
    from_text = parse_vehicle_suitability_text(str(suitability)) if suitability else None
    if from_text and from_text.get("make"):
        make = make or from_text["make"]
        model = model or from_text["model"]
        engine = engine or from_text["engine"]
        year_from = year_from if year_from not in (None, "") else from_text.get("year_from")
        year_to = year_to if year_to not in (None, "") else from_text.get("year_to")
    return {
        "make": str(make or "").strip(),
        "model": str(model or "").strip(),
        "engine": str(engine or "").strip(),
        "year_from": _as_year(year_from),
        "year_to": _as_year(year_to),
        "oe_numbers": _as_oe_list(oe),
        "attributes": flat,
    }


def merge_fitment_fields(*parts: Dict[str, Any]) -> Dict[str, Any]:
    """Zlúč viacero zdrojov; prvé neprázdne polia vyhrávajú (okrem OE = union)."""
    out: Dict[str, Any] = {
        "make": "",
        "model": "",
        "engine": "",
        "year_from": None,
        "year_to": None,
        "oe_numbers": [],
    }
    oes: List[str] = []
    for part in parts:
        if not part:
            continue
        for key in ("make", "model", "engine"):
            if not out[key] and part.get(key):
                out[key] = str(part[key]).strip()
        for key in ("year_from", "year_to"):
            if out[key] is None and part.get(key) not in (None, ""):
                out[key] = _as_year(part.get(key))
        oes.extend(_as_oe_list(part.get("oe_numbers")))
    seen = set()
    uniq = []
    for oe in oes:
        k = oe.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(oe)
    out["oe_numbers"] = uniq
    return out


def pipe_union_values(*values: Any) -> str:
    """Zlúč pipe-separated hodnoty bez duplicít (poradie = prvé výskyty)."""
    seen: List[str] = []
    for raw in values:
        if raw is None:
            continue
        for part in str(raw).split("|"):
            p = part.strip()
            if not p:
                continue
            if p not in seen:
                seen.append(p)
    return "|".join(seen)


def merge_fitment_meta_with_existing(
    new_meta: List[Dict[str, Any]],
    existing_meta_data: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Pri re-importe rovnakého SKU: union **párov** + OE.

    Preferuje `_sg_fit_vehicles` JSON. Pipe polia sa vždy odvodia z párov
    (žiadny cartesian make×model „hala-bála“).
    """
    existing_vehicles = vehicles_from_product_meta(existing_meta_data)
    incoming_vehicles = vehicles_from_product_meta(new_meta)

    # Ak incoming nemá páry, skús zložiť z incoming flat make+model+sg_id
    if not incoming_vehicles:
        incoming_flat: Dict[str, Any] = {}
        for row in new_meta or []:
            if isinstance(row, dict) and row.get("key"):
                incoming_flat[str(row["key"])] = row.get("value")
        if incoming_flat.get("_sg_fit_sg_id") or (
            incoming_flat.get("_sg_fit_make") and incoming_flat.get("_sg_fit_model")
        ):
            incoming_vehicles = vehicles_from_sg_id_pipe(
                str(incoming_flat.get("_sg_fit_sg_id") or ""),
                engine_pipe=str(incoming_flat.get("_sg_fit_engine") or ""),
                year_from=incoming_flat.get("_sg_fit_year_from"),
                year_to=incoming_flat.get("_sg_fit_year_to"),
            )
            if not incoming_vehicles:
                pair = normalize_vehicle_pair(
                    make=str(incoming_flat.get("_sg_fit_make") or "").split("|")[0],
                    model=str(incoming_flat.get("_sg_fit_model") or "").split("|")[0],
                    engine=str(incoming_flat.get("_sg_fit_engine") or "").split("|")[0],
                    year_from=incoming_flat.get("_sg_fit_year_from"),
                    year_to=incoming_flat.get("_sg_fit_year_to"),
                    sg_id=str(incoming_flat.get("_sg_fit_sg_id") or "").split("|")[0],
                )
                if pair:
                    incoming_vehicles = [pair]

    merged_vehicles = merge_vehicle_lists(existing_vehicles, incoming_vehicles)

    existing_oe = []
    incoming_oe = []
    other: List[Dict[str, Any]] = []
    for row in existing_meta_data or []:
        if isinstance(row, dict) and row.get("key") == "_sg_oe_numbers":
            existing_oe = [
                x for x in str(row.get("value") or "").split("|") if x.strip()
            ]
    for row in new_meta or []:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        key = str(row["key"])
        if key == "_sg_oe_numbers":
            incoming_oe = [
                x for x in str(row.get("value") or "").split("|") if x.strip()
            ]
        elif key.startswith("_sg_fit_"):
            continue  # regenerujeme z párov
        else:
            other.append(row)

    oes = merge_oe_numbers(existing_oe, incoming_oe)
    out = list(other)
    out.extend(vehicles_to_meta(merged_vehicles, oe_numbers=oes or None))
    return out


def fitment_meta_payload(
    make: str = "",
    model: str = "",
    engine: str = "",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    oe_numbers: Optional[List[str]] = None,
    sg_id: str = "",
    vehicles: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Meta polia na produkt — overené páry + OE dielu."""
    pairs: List[Dict[str, Any]] = []
    if vehicles:
        pairs = merge_vehicle_lists(vehicles)
    else:
        pair = normalize_vehicle_pair(
            make=make,
            model=model,
            engine=engine,
            year_from=year_from,
            year_to=year_to,
            sg_id=sg_id,
        )
        if pair:
            pairs = [pair]
        elif make or model:
            # fallback: flat bez páru (staré chovanie) — len ak mapovanie zlyhá
            mapped = map_to_sg_hierarchy(make, model, engine)
            meta = []
            mk = mapped["make"] or normalize_make(make)
            md = mapped["model"] or (str(model).strip() if model else "")
            en = mapped["engine"] or (str(engine).strip() if engine else "")
            sid = mapped["sg_id"] or sg_id
            if mk:
                meta.append({"key": "_sg_fit_make", "value": mk})
            if md:
                meta.append({"key": "_sg_fit_model", "value": md})
            if en:
                meta.append({"key": "_sg_fit_engine", "value": en})
            if year_from is not None:
                meta.append({"key": "_sg_fit_year_from", "value": str(year_from)})
            if year_to is not None:
                meta.append({"key": "_sg_fit_year_to", "value": str(year_to)})
            if sid:
                meta.append({"key": "_sg_fit_sg_id", "value": sid})
            if oe_numbers:
                meta.append({"key": "_sg_oe_numbers", "value": "|".join(oe_numbers)})
            return meta
    return vehicles_to_meta(pairs, oe_numbers=oe_numbers)


def parse_fitment_from_product_name(name: str) -> Dict[str, Any]:
    """Fitment z názvu — len pri explicitnom „pre Make…“ / „Make (staršie…)“.

    Zámerne NEBERIE make na začiatku názvu (BMW TPT olej ≠ fitment BMW).
    """
    empty = {
        "make": "", "model": "", "engine": "",
        "year_from": None, "year_to": None,
    }
    if not name or not str(name).strip():
        return empty
    text = str(name).strip()

    # „… pre Fiat Punto“ / „… pre Mercedes“ / „… pre Fiat/Renault“
    m = re.search(r"(?i)\bpre\s+(.+)$", text)
    if m:
        blob = m.group(1).strip()
        # prvá značka pred lomítkom (Fiat/Renault → Fiat)
        first = re.split(r"[/|,]", blob)[0].strip()
        got = parse_vehicle_suitability_text(first)
        if got.get("make"):
            return got
        # suitability parser chce make na začiatku — first už je make[+model]
        got = parse_vehicle_suitability_text(blob.replace("/", " "))
        if got.get("make"):
            return got

    # „… konektor VW (staršie modely)“ — len známy make tesne pred zátvorkou
    # (nie ľubovoľné slovo ako „konektor“).
    lower = text.lower()
    for tok, canon in _known_make_tokens():
        if " " in tok:
            continue
        m2 = re.search(
            rf"(?i)\b{re.escape(tok)}\s*\((?:staršie|novšie|older|newer)",
            text,
        )
        if m2:
            return {**empty, "make": canon}
    del lower

    return empty


def parse_fitment_from_supplier(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizácia fitment polí z B2B feedov → mapované na SG strom.

    Zdroje (v poradí priority textových polí): top-level ProductRaw →
    attributes keys → suitability text / description → product name → catalog slug.
    OE = union všetkých zdrojov. Cudzie vehicle ID sa zahodia v sanitize.
    """
    safe = sanitize_inbound_fitment(raw)
    from_attrs = extract_fitment_from_attributes(safe.get("attributes") or {})
    # Suitability text — explicitné polia vždy; description len pri „vhodné pre:“ riadku
    # (olejové popisy obsahujú špecifikácie typu Ford WSS-… — nesmú sa brať ako make).
    text_bits: List[str] = []
    if isinstance(raw, dict) and raw.get("suitability"):
        text_bits.append(str(raw.get("suitability")))
    suit_attr = (safe.get("attributes") or {}).get("suitability") or _pick(
        safe.get("attributes") or {}, _ATTR_SUITABILITY_KEYS
    )
    if suit_attr:
        text_bits.append(str(suit_attr))
    desc = str(raw.get("description") or "") if isinstance(raw, dict) else ""
    if desc:
        for line in desc.splitlines():
            if _SUITABILITY_LINE_RE.search(line.strip()):
                text_bits.append(line.strip())
    from_text = parse_vehicle_suitability_text("\n".join(text_bits)) if text_bits else {}
    from_name = parse_fitment_from_product_name(
        str(raw.get("name") or "") if isinstance(raw, dict) else ""
    )
    slug_src = ""
    if isinstance(raw, dict):
        slug_src = (
            raw.get("catalog_slug")
            or raw.get("supplier_url")
            or (raw.get("raw_extra") or {}).get("catalog_url")
            or ""
        )
    from_slug = parse_fitment_from_catalog_slug(str(slug_src)) if slug_src else {}

    merged = merge_fitment_fields(
        {
            "make": safe.get("make") or "",
            "model": safe.get("model") or "",
            "engine": safe.get("engine") or "",
            "year_from": safe.get("year_from"),
            "year_to": safe.get("year_to"),
            "oe_numbers": safe.get("oe_numbers") or [],
        },
        from_attrs,
        from_text,
        from_name,
        from_slug,
    )
    make, model, engine = merged["make"], merged["model"], merged["engine"]
    mapped = map_to_sg_hierarchy(str(make or ""), str(model or ""), str(engine or ""))

    # Generácia zo slug / model textu → preferuj sgv-gen-* (nie TecDoc ID)
    gen_hint = ""
    if isinstance(from_slug, dict):
        gen_hint = str(from_slug.get("generation") or "")
    if not gen_hint and isinstance(raw, dict):
        # TecDoc URL: …/octavia-iii-…/1-6-tdi/…
        blob = " ".join(
            str(x or "")
            for x in (
                slug_src,
                raw.get("catalog_slug"),
                (raw.get("raw_extra") or {}).get("catalog_url"),
                model,
            )
        )
        gm = re.search(
            r"(?:^|[-/\s])(i{1,3}|iv|v|vi|vii|viii|ix|x)(?:[-/\s]|$)",
            blob.lower(),
        )
        if gm:
            gen_hint = gm.group(1).upper()
        # Chassis kód v slug-u (u11 / f48 / 5e / b8) → presná generácia
        if not gen_hint:
            toks = set(re.split(r"[^a-z0-9]+", blob.lower()))
            for g in generations_for(
                mapped.get("make") or str(make or ""), mapped.get("model") or str(model or "")
            ):
                lab = str(g.get("label") or "").lower()
                if lab and lab in toks:
                    gen_hint = str(g.get("label"))
                    break
    gen = map_generation(
        mapped.get("make") or str(make or ""),
        mapped.get("model") or str(model or ""),
        generation=gen_hint,
        year=_as_year(merged.get("year_from")),
    )
    sg_primary = gen.get("sg_gen_id") or mapped.get("sg_id") or ""

    return {
        "make": mapped["make"] or normalize_make(str(make) if make else ""),
        "model": mapped["model"] or (str(model).strip() if model else ""),
        "engine": mapped["engine"] or (str(engine).strip() if engine else ""),
        "generation": gen.get("generation") or gen_hint or "",
        "year_from": _as_year(merged.get("year_from")),
        "year_to": _as_year(merged.get("year_to")),
        "oe_numbers": _as_oe_list(merged.get("oe_numbers")),
        "sg_id": sg_primary,
        "sg_gen_id": gen.get("sg_gen_id") or "",
    }
