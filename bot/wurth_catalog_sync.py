#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Týždenný sync verejného katalógu Würth. Bez spolupráce Würth SK.

Berie len shopy, ktoré odpovedajú obyčajným HTTP 200 a robots.txt dovolí
sitemap aj produktovú pretty URL: eshop.wurth.ba a eshop.wurth-kosova.com.
Žiadne search URL (BA ``?q=``, Kosovo ``/*?``), žiadne impersonovanie.

1. Artikl, ktorý sme už vo verejnom sitemape videli a potom dva behy po sebe
   chýba vo všetkých povolených shopoch, dostane dostupnosť Nedostupné
   (neobjednateľné). To, že ho Bosna ani Kosovo nikdy nemali, Nedostupné
   nespustí. Produkt sa nemaže. Cena ostáva.
2. Doplní názov, popis, fotku a balenie, len kde chýbajú. Slovenský názov,
   ktorý už máme, sa neprepisuje. Popis z iného jazyka sa skráti a preloží
   do slovenčiny (bosnianske produktové frázy). Albánčina sa namiesto
   slovenčiny nepublikuje.
3. Nové artikle okolo auta (autochémia, spojovací materiál, náradie,
   dielenské vybavenie, elektro, spotrebný materiál, príslušenstvo
   servisu a detailingu) sú koncepty bez ceny. Zaradia sa do existujúcej
   kategórie shopu. Nejasná kategória ostane koncept bez kategórie.
   Nové kategórie sa nezakladajú. Stavba, záhrada, CNC a Hahn+Kolb nie.

Cena sa nevymýšľa. Sklad sa mení len pravidlom Nedostupné a jeho zrušením,
keď sa artikl znova objaví (bez množstva kusov).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog_licensing import assert_allowed_catalog_url  # noqa: E402
from wurth_photo_backfill import (  # noqa: E402
    BA_HOST,
    BA_SITEMAP,
    KOS_HOST,
    KOS_SITEMAP,
    PublicHttp,
    article_from_ba_product_url,
    article_from_kosovo_url,
    load_article_index,
    needs_photo,
    normalize_article,
    sitemap_locs,
)

LOG = logging.getLogger("wurth_catalog_sync")

CATALOG_UA = "SmartGarazBot/1.0 (wurth-catalog-sync; plain HTTP; +https://smartgaraz.sk)"
STATE_NAME = "wurth_catalog_sync_state.json"
ABSENT_MARK = "1"
ABSENT_SRC = "Würth verejný katalóg: artikl chýba dva týždne po sebe"
PRICE_KEYS = frozenset({"regular_price", "sale_price", "price"})
# Popis z verejnej stránky držíme krátky.
MAX_SHORT = 320
MAX_BULLETS = 4

# Celý L1 verejného shopu, ak je to okolo auta. Stavba, záhrada, sanita,
# nábytkové kovanie, kotvy, požiarna ochrana budov a Hahn+Kolb tu nie sú.
BUCKET_ROOTS = {
    "hemijsko-tehnicki-proizvodi": "chemistry",
    "elementi-za-spajanje-opsti-metricki-colni": "fasteners",
    "rucni-alati": "tools",
    "mjerna-tehnika": "tools",
    "masine-akumulatorske-pneumatske-elektro-motorne": "tools",
    "radionicka-oprema": "workshop",
    "lemljenje-zavarivanje": "workshop",
    "ewm-program-zavarivanja": "workshop",
    "orsy-sistem": "workshop",
    "masinska-obrada-materijala": "consumables",
    "crijeva-spojnice-obujmice-za-crijeva": "consumables",
    "zastita-na-radu": "consumables",
    "proizvodi-od-tehnicke-gume-i-plastike": "consumables",
    "dijelovi-za-vozila": "vehicle",
    "oprema-za-motorna-vozila-orsymobil": "vehicle_equipment",
    "dijagnostika-za-motorna-vozila-wow": "tools",
}
# Elektro len tam, kde ide o auto alebo dielňu. Domové zásuvky, smart home
# a ističe v tom nie sú.
ELECTRICAL_L2 = frozenset({
    "elektroinstalacije/elektroinstalacioni-materijal",
    "elektroinstalacije/kablovske-spojnice-za-elektroinstalacije",
    "elektroinstalacije/elektroizolacioni-materijali",
    "elektroinstalacije/tehnologija-spajanja",
    "elektroinstalacije/elektroinstalacijski-dijelovi",
    "elektroinstalacije/elektromaterijal-za-izjednacavanje-potencijala",
})
# Podstrom, ktorý v inom prípade povolenom L1 nie je okolo auta.
EXCLUDE_PREFIXES = (
    "hemijsko-tehnicki-proizvodi/gradevinska-hemija",
    "rucni-alati/gradevinski-zanatski-alati",
    "rucni-alati/specificni-alati-za-gradevinarstvo",
    "rucni-alati/alati-za-ciscenje-i-kopanje",
    "elementi-za-spajanje-opsti-metricki-colni/vijci-za-drvo",
    "elementi-za-spajanje-opsti-metricki-colni/montazni-elementi-za-drvo-opcenito",
    "elementi-za-spajanje-opsti-metricki-colni/galanterija-za-biro-i-kucanstvo",
    "elementi-za-spajanje-opsti-metricki-colni/spojni-elementi-za-profilne-sisteme",
    "masinska-obrada-materijala/alati-za-glodanje-i-struganje",
    "masinska-obrada-materijala/cnc-obrada-materijala",
    "crijeva-spojnice-obujmice-za-crijeva/oprema-za-navodnjavanje-connex",
    "zastita-na-radu/bezbjednost-na-radnom-mjestu-gradilistu",
    "radionicka-oprema/administriranje-dokumentacije",
)
EXCLUDE_MARKERS = (
    "gradevinska-hemija",
    "gradevin",
    "poljoprivred",
    "hortikult",
    "vrtni-",
    "alati-za-vrt",
    "hahn-kolb",
    "/cnc",
)
# Najdlhší prefix vyhrá. Hodnota je cesta v našom strome. Prázdne = nejasné.
# Spojovací materiál a ochranné pomôcky nemajú vlastnú kategóriu, ostanú bez nej.
_TOOLS = ("Náradie & Vybavenie", "Dielenské vybavenie")
_KEYS = ("Náradie & Vybavenie", "Kľúče & račne")
_MEASURE = ("Náradie & Vybavenie", "Diagnostika & meranie")
_LABELS = ("Náradie & Vybavenie", "Etikety & označenie")
_CHEM_SEAL = ("Dielenská chémia & Údržba", "Tesnenia & Lepidlá")
_CHEM = ("Dielenská chémia & Údržba",)
_ELECTRO = ("Náhradné diely", "Elektro & senzory")
_CABLES = ("Náhradné diely", "Elektro & senzory", "Káble & konektory")
_AC = ("Náhradné diely", "Chladenie & kúrenie", "Diely klimatizácie")
SLUG_CATEGORY: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("hemijsko-tehnicki-proizvodi/ljepila", _CHEM_SEAL),
    ("hemijsko-tehnicki-proizvodi/zaptivne-mase", _CHEM_SEAL),
    ("hemijsko-tehnicki-proizvodi/maziva-i-odstranjivaci-hrde", _CHEM),
    ("hemijsko-tehnicki-proizvodi/sistemi-za-punjenje-visekratne-boce", _TOOLS),
    ("rucni-alati/program-nasadnih-kljuceva", _KEYS),
    ("rucni-alati/kljucevi-za-vijke", _KEYS),
    ("rucni-alati/program-odvijaca-vijaka", _KEYS),
    ("rucni-alati/bitsevi-i-pribor", _KEYS),
    ("rucni-alati/setovi-rucnih-alata", _KEYS),
    ("rucni-alati/specijalni-alati-za-motorna-vozila", _TOOLS),
    ("rucni-alati/specificni-alati-za-motorna-vozila", _TOOLS),
    ("rucni-alati/specijalni-alati-za-privredna-vozila", _TOOLS),
    ("rucni-alati/kfz-nfz-alat-za-inspekciju", _TOOLS),
    ("rucni-alati/alati-za-karoserije", _TOOLS),
    ("rucni-alati/klijesta-i-pincete", _TOOLS),
    ("rucni-alati/udarni-alati", _TOOLS),
    ("rucni-alati/stezni-alati", _TOOLS),
    ("rucni-alati/alati-za-sjecenje", _TOOLS),
    ("rucni-alati/alati-za-rezanje-i-struganje", _TOOLS),
    ("rucni-alati/izvlakaci", _TOOLS),
    ("rucni-alati/alati-za-izvijanje-vijaka", _TOOLS),
    ("rucni-alati/alati-za-lomljenje-navrtki", _TOOLS),
    ("rucni-alati/reparatura-i-ojacavanje-navoja", _TOOLS),
    ("rucni-alati/tehnika-zakivanja-zakovicama", _TOOLS),
    ("rucni-alati/alati-za-skidanje-izolacije", _TOOLS),
    ("rucni-alati/metle-cetke-i-drske", _TOOLS),
    ("rucni-alati/alati-za-obiljezavanje-i-oznacavanje", _TOOLS),
    ("masine-akumulatorske-pneumatske-elektro-motorne", _TOOLS),
    ("mjerna-tehnika", _MEASURE),
    ("dijagnostika-za-motorna-vozila-wow", _MEASURE),
    ("radionicka-oprema/pneumatska-tehnika", _TOOLS),
    ("radionicka-oprema/sistemi-za-skladistenje", _TOOLS),
    ("radionicka-oprema/radna-rasvjeta-baterije-punjaci", _TOOLS),
    ("radionicka-oprema/radionicka-oprema-za-motorna-vozila", _TOOLS),
    ("radionicka-oprema/trake-kaisevi-lanci-uzadi-ankeri", _TOOLS),
    ("radionicka-oprema/sanduci-za-alate-koferi-tasne", _TOOLS),
    ("radionicka-oprema/sanduci", _TOOLS),
    ("radionicka-oprema/nosaci-alata-omce-za-alat-torbe", _TOOLS),
    ("radionicka-oprema/sistemska-radionicka-oprema", _TOOLS),
    ("radionicka-oprema/uredaji-za-dizanje", _TOOLS),
    ("radionicka-oprema/podizne-platforme", _TOOLS),
    ("radionicka-oprema/sistemi-za-skladistenje-opasnih-materija", _TOOLS),
    ("radionicka-oprema/materijal-za-pakovanje", _TOOLS),
    ("radionicka-oprema/sistemi-za-obiljezavanje", _LABELS),
    ("lemljenje-zavarivanje", _TOOLS),
    ("ewm-program-zavarivanja", _TOOLS),
    ("orsy-sistem", _TOOLS),
    ("oprema-za-motorna-vozila-orsymobil", _TOOLS),
    ("masinska-obrada-materijala", _TOOLS),
    ("crijeva-spojnice-obujmice-za-crijeva", _TOOLS),
    ("elektroinstalacije/kablovske-spojnice-za-elektroinstalacije", _CABLES),
    ("elektroinstalacije/tehnologija-spajanja", _CABLES),
    ("elektroinstalacije/elektroinstalacioni-materijal", _ELECTRO),
    ("elektroinstalacije/elektroizolacioni-materijali", _ELECTRO),
    ("elektroinstalacije/elektroinstalacijski-dijelovi", _ELECTRO),
    ("elektroinstalacije/elektromaterijal-za-izjednacavanje-potencijala", _ELECTRO),
    ("dijelovi-za-vozila/elektrika-za-motorna-vozila", _ELECTRO),
    ("dijelovi-za-vozila/klima-tehnologija-za-motorna-vozila", _AC),
)
# Kľúčové slovo z názvu smie spresniť cestu len v týchto hlavných kategóriách.
KEYWORD_MAINS = {
    "chemistry": frozenset({
        "Dielenská chémia & Údržba", "Autochémia & Aditíva",
        "Autokozmetika & Detail", "Motorové oleje & Kvapaliny",
    }),
    "fasteners": frozenset({"Náhradné diely"}),
    "tools": frozenset({"Náradie & Vybavenie"}),
    "workshop": frozenset({"Náradie & Vybavenie"}),
    "electrical": frozenset({"Náhradné diely", "Náradie & Vybavenie"}),
    "consumables": frozenset({
        "Náradie & Vybavenie", "Dielenská chémia & Údržba", "Autokozmetika & Detail",
    }),
    "vehicle": frozenset({
        "Náhradné diely", "Autokozmetika & Detail", "Dielenská chémia & Údržba",
        "Motorové oleje & Kvapaliny", "Náradie & Vybavenie", "Autochémia & Aditíva",
    }),
    "vehicle_equipment": frozenset({"Náradie & Vybavenie", "Náhradné diely"}),
}

# Najdlhšie frázy prvé. Len produktové výrazy, nie voľný preklad viet.
_BCS_PHRASES: Tuple[Tuple[str, str], ...] = (
    ("vjetrobranskih stakala", "čelných skiel"),
    ("vjetrobranska stakla", "čelné sklá"),
    ("vjetrobransko staklo", "čelné sklo"),
    ("zaptivna masa", "tesniaca hmota"),
    ("zaptivne mase", "tesniace hmoty"),
    ("sredstvo za čišćenje", "čistiaci prostriedok"),
    ("sredstvo za ciscenje", "čistiaci prostriedok"),
    ("odstranjivač hrđe", "odstraňovač hrdze"),
    ("odstranjivaci hrde", "odstraňovače hrdze"),
    ("veličina pakovanja", "veľkosť balenia"),
    ("područje upotrebe", "použitie"),
    ("rezna ploča", "rezný kotúč"),
    ("brusna ploča", "brúsny kotúč"),
    ("zaptivanje", "tesnenie"),
    ("zaptivna", "tesniaca"),
    ("zaptivne", "tesniace"),
    ("vanjsko", "vonkajšie"),
    ("vanjska", "vonkajšia"),
    ("automobilima", "automobiloch"),
    ("automobila", "automobilov"),
    ("čišćenje", "čistenie"),
    ("ciscenje", "čistenie"),
    ("ljepilo", "lepidlo"),
    ("ljepila", "lepidlá"),
    ("vijak", "skrutka"),
    ("vijci", "skrutky"),
    ("podloška", "podložka"),
    ("kliješta", "kliešte"),
    ("ključ", "kľúč"),
    ("kljucevi", "kľúče"),
    ("brusni", "brúsny"),
    ("brusna", "brúsna"),
    ("crna", "čierna"),
    ("crni", "čierny"),
    ("bijela", "biela"),
    ("siva", "sivá"),
    ("osobine", "vlastnosti"),
    ("pakovanje", "balenie"),
    ("sredstvo", "prostriedok"),
    ("hrđe", "hrdze"),
    ("hrde", "hrdze"),
)


def robots_star_rules(text: str) -> List[Tuple[bool, str]]:
    """Pravidlá skupiny ``User-agent: *``. (allow, pattern)."""
    rules: List[Tuple[bool, str]] = []
    capture = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            capture = value == "*"
            continue
        if not capture:
            continue
        if key == "allow" and value:
            rules.append((True, value))
        elif key == "disallow" and value:
            rules.append((False, value))
    return rules


def _rule_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    escaped = re.escape(pattern).replace(r"\*", ".*")
    if pattern.endswith("$"):
        return re.search(escaped, path) is not None
    if "*" in pattern:
        return re.search(escaped, path) is not None
    return path.startswith(pattern)


def robots_allows(text: str, path: str) -> bool:
    """True, ak najdlhšie pravidlo User-agent * cestu nezakazuje. Bez zhody = dovolené."""
    best: Optional[Tuple[int, bool]] = None
    for allow, pattern in robots_star_rules(text):
        if not _rule_matches(pattern, path):
            continue
        leng = len(pattern)
        if best is None or leng > best[0] or (leng == best[0] and allow):
            best = (leng, allow)
    return True if best is None else best[1]


def _path_parts(path: str) -> List[str]:
    return [p for p in unquote(path).strip("/").split("/") if p]


def bucket_for_path(path: str) -> Optional[str]:
    """Skupina okolo auta, alebo None (stavba, záhrada, sanita, CNC, iné)."""
    parts = _path_parts(path)
    if not parts:
        return None
    blob = "/" + "/".join(parts)
    if any(marker in blob for marker in EXCLUDE_MARKERS):
        return None
    joined = "/".join(parts)
    if any(joined == prefix or joined.startswith(prefix + "/") for prefix in EXCLUDE_PREFIXES):
        return None
    root = parts[0]
    if root in BUCKET_ROOTS:
        return BUCKET_ROOTS[root]
    two = "/".join(parts[:2])
    if two in ELECTRICAL_L2:
        return "electrical"
    return None


def strict_category_path(path: Sequence[str]) -> Optional[List[str]]:
    """Cesta len ak je celá v našom strome. Neznámy list sa nedopĺňa prvou položkou."""
    from taxonomy import CATEGORY_TREE, PART_LEAVES

    cleaned = [str(part).strip() for part in path if str(part).strip()]
    if not cleaned or cleaned[0] not in CATEGORY_TREE:
        return None
    if len(cleaned) == 1:
        return [cleaned[0]]
    sub = next((name for name in CATEGORY_TREE[cleaned[0]] if name.lower() == cleaned[1].lower()), None)
    if not sub:
        return None
    if len(cleaned) == 2:
        return [cleaned[0], sub]
    leaf = next((name for name in (PART_LEAVES.get(sub) or []) if name.lower() == cleaned[2].lower()), None)
    if not leaf:
        return None
    return [cleaned[0], sub, leaf]


def slug_category(path: str) -> Optional[List[str]]:
    """Existujúca kategória podľa vetvy verejného shopu. None = z URL nejasné."""
    if not bucket_for_path(path):
        return None
    joined = "/".join(_path_parts(path))
    best: Optional[Tuple[str, ...]] = None
    best_len = -1
    for prefix, shop_path in SLUG_CATEGORY:
        if joined == prefix or joined.startswith(prefix + "/"):
            if len(prefix) > best_len:
                best = shop_path
                best_len = len(prefix)
    if not best:
        return None
    return strict_category_path(best)


def shop_category_for(path: str, slovak_name: str = "") -> Optional[List[str]]:
    """Naša kategória. Názov ju smie spresniť. Keď nesedí, vráti None."""
    bucket = bucket_for_path(path)
    if not bucket:
        return None
    from taxonomy import match_keyword_category

    slug_path = slug_category(path)
    keyword_path = None
    name = (slovak_name or "").strip()
    if name:
        raw = match_keyword_category({"name": name, "sku": "", "brand": "Würth"})
        if raw and raw[0] in KEYWORD_MAINS.get(bucket, frozenset()):
            keyword_path = strict_category_path(raw)
    if keyword_path and slug_path:
        if keyword_path[0] == slug_path[0] and len(keyword_path) >= len(slug_path):
            return keyword_path
        if len(slug_path) >= 2:
            return slug_path
        return keyword_path
    return keyword_path or slug_path


def existing_category_leaf(rows: Sequence[dict], path: Sequence[str]) -> Optional[int]:
    """ID poslednej úrovne, len keď celá cesta vo Woo už existuje. Nič nevytvára."""
    parent = 0
    leaf: Optional[int] = None
    for name in path:
        want = html.unescape(name).strip().lower()
        found = None
        for row in rows:
            if int(row.get("parent") or 0) != parent:
                continue
            if html.unescape(str(row.get("name") or "")).strip().lower() == want:
                found = int(row["id"])
                break
        if found is None:
            return None
        leaf = found
        parent = found
    return leaf


def leaf_paths(paths: Iterable[str]) -> List[str]:
    """Cesty, ktoré nemajú v sitemape dlhšieho potomka. To sú produktové stránky."""
    ordered = sorted(set(paths))
    leaves = []
    for index, path in enumerate(ordered):
        nxt = ordered[index + 1] if index + 1 < len(ordered) else ""
        if nxt.startswith(path + "/"):
            continue
        if len([p for p in path.split("/") if p]) >= 4:
            leaves.append(path)
    return leaves


def looks_slovak(text: str) -> bool:
    return bool(re.search(r"[ľĺŕôä]", text or "", re.I))


def looks_albanian(text: str) -> bool:
    low = f" {text or ''} ".lower()
    return "ë" in low or " për " in low or " dhe " in low


def translate_bcs_to_sk(text: str) -> str:
    """Krátky faktický prevod bosnianskych produktových fráz. Albánčinu nechá prázdnu."""
    raw = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    if not raw or looks_albanian(raw):
        return ""
    if looks_slovak(raw):
        return raw
    low = raw
    for src, dst in _BCS_PHRASES:
        low = re.sub(re.escape(src), dst, low, flags=re.I)
    low = low.replace("ć", "č").replace("Ć", "Č").replace("đ", "ď").replace("Đ", "Ď")
    low = re.sub(r"(?i)\b(?:cijena|cijene|cena|ciene)\b[^.]*(?:\.|$)", "", low)
    low = re.sub(r"\d+[.,]\d+\s*(?:KM|BAM|EUR|€)", "", low, flags=re.I)
    low = re.sub(r"\s+", " ", low).strip(" .")
    return low


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_public_product(page_html: str) -> dict:
    """Názov, popis a balenie z verejnej BA stránky. Cenu nečíta."""
    code_match = re.search(r'data-product-code="?([0-9][0-9\s]{3,})', page_html or "")
    article = normalize_article(code_match.group(1)) if code_match else ""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page_html or "", re.I | re.S)
    if h1:
        name = _plain(h1.group(1))
    else:
        title = re.search(r"<title>(.*?)</title>", page_html or "", re.I | re.S)
        name = _plain(title.group(1)).split(" - ")[0].strip() if title else ""
    desc_match = re.search(
        r'id="description"[^>]*>\s*<div class="ulcontent">(.*?)</div>',
        page_html or "",
        re.I | re.S,
    )
    if not desc_match:
        desc_match = re.search(r'id="description"[^>]*>(.*?)</div>\s*</div>', page_html or "", re.I | re.S)
    raw_desc = desc_match.group(1) if desc_match else ""
    description = _plain(raw_desc)
    description = re.sub(r"^(?:Opis\s+)?Tehnički detalji\s+Informacije o artiklu\s+", "", description).strip()
    bullets = [
        _plain(item)
        for item in re.findall(r"<li[^>]*>(.*?)</li>", raw_desc, re.I | re.S)
    ]
    bullets = [b for b in bullets if b and not re.search(r"\b(cijena|cena|cijene)\b", b, re.I)]
    plain = _plain(page_html or "")
    pack = None
    pack_match = re.search(r"Veličina pakovanja\s+(\d{1,6})\s*kom", plain, re.I)
    if pack_match:
        qty = int(pack_match.group(1))
        if 1 <= qty <= 100000:
            pack = qty
    return {"article": article, "name": name, "description": description, "bullets": bullets[:8], "pack_qty": pack}


def merge_public(base: dict, extra: dict) -> dict:
    """Doplní z kanonickej stránky len polia, ktoré listová URL nemala."""
    if extra.get("article") and base.get("article") and extra["article"] != base["article"]:
        return base
    merged = dict(base)
    if not merged.get("article") and extra.get("article"):
        merged["article"] = extra["article"]
    if not merged.get("name") and extra.get("name"):
        merged["name"] = extra["name"]
    if not merged.get("description") and extra.get("description"):
        merged["description"] = extra["description"]
        merged["bullets"] = list(extra.get("bullets") or [])
    elif not merged.get("bullets") and extra.get("bullets"):
        merged["bullets"] = list(extra["bullets"])
    if not merged.get("pack_qty") and extra.get("pack_qty"):
        merged["pack_qty"] = extra["pack_qty"]
    return merged


def slovak_content(parsed: dict) -> dict:
    """Krátky slovenský názov a popis. Prázdny popis, ak zdroj nie je preložiteľný."""
    name = translate_bcs_to_sk(parsed.get("name") or "")
    if name and not name.lower().startswith("würth") and not name.lower().startswith("wurth"):
        name = f"Würth {name}"
    name = name[:140].strip()
    lead = translate_bcs_to_sk(parsed.get("description") or "")
    lead = lead.split(" Osobine")[0].split(" Vlastnosti")[0].strip()
    if len(lead) > MAX_SHORT:
        lead = lead[: MAX_SHORT - 1].rsplit(" ", 1)[0] + "…"
    points = []
    for bullet in parsed.get("bullets") or []:
        sk = translate_bcs_to_sk(bullet)
        if sk and sk not in points:
            points.append(sk[:160])
        if len(points) >= MAX_BULLETS:
            break
    bits = [lead] if lead else []
    if points:
        bits.append(" ".join(f"• {p}" for p in points))
    if parsed.get("pack_qty"):
        bits.append(f"Balenie výrobcu {parsed['pack_qty']} ks.")
    body = " ".join(b for b in bits if b).strip()
    short = lead or (points[0] if points else "")
    return {"name": name, "description": body[:900], "short": short[:MAX_SHORT]}


def name_is_ours(name: str, sku: str) -> bool:
    """Neprázdny názov, ktorý nie je len artiklové číslo, nechávame."""
    text = re.sub(r"\s+", " ", (name or "")).strip()
    if not text:
        return False
    folded = text.lower()
    sku_l = (sku or "").lower()
    return folded not in {sku_l, f"würth {sku_l}", f"wurth {sku_l}"}


def description_missing(text: str) -> bool:
    plain = _plain(text)
    return len(plain) < 40


def needs_public_page(product: dict, *, media_ok: bool) -> bool:
    """Stránku sťahujeme len keď niečo chýba. Kompletný produkt sa nečíta znova."""
    meta = meta_of(product)
    sku = normalize_article(str(product.get("sku") or ""))
    if not name_is_ours(str(product.get("name") or ""), sku):
        return True
    if description_missing(str(product.get("description") or "")):
        return True
    if pack_missing(meta):
        return True
    images = product.get("images")
    return bool(media_ok and images is not None and needs_photo(images))


def pack_missing(meta: dict) -> bool:
    if str(meta.get("_sg_pack_verified") or "") == "1" and str(meta.get("_sg_pack_qty") or "").strip():
        return False
    return not str(meta.get("_sg_pack_qty") or "").strip()


def meta_of(product: dict) -> dict:
    return {m.get("key"): m.get("value") for m in (product.get("meta_data") or []) if isinstance(m, dict)}


def _forbid_price(payload: dict) -> dict:
    present = PRICE_KEYS.intersection(payload)
    if present:
        raise RuntimeError(f"katalógový zápis nesmie mať cenu: {sorted(present)}")
    if "stock_quantity" in payload:
        raise RuntimeError("katalógový zápis nesmie nastavovať počet kusov")
    return payload


def plan_unavailable(product_id: int) -> dict:
    """Nedostupné = neobjednateľné. Bez ceny a bez počtu kusov."""
    from availability import meta_rows, stock_payload

    payload: dict = {"id": int(product_id), "meta_data": meta_rows({"avail": "na", "src": ABSENT_SRC})}
    payload["meta_data"].append({"key": "_sg_wurth_public_absent", "value": ABSENT_MARK})
    stock_payload(payload, "na")
    payload.pop("stock_quantity", None)
    return _forbid_price(payload)


def plan_return(product_id: int, origin: str = "") -> dict:
    """Artikl sa vrátil. Zruší naše Nedostupné, kusy na sklade nevymýšľa."""
    from availability import classify_wurth, meta_rows, stock_payload, to_legend

    avail = to_legend(classify_wurth(origin or "", "instock"))
    payload: dict = {"id": int(product_id), "meta_data": meta_rows(avail)}
    payload["meta_data"].append({"key": "_sg_wurth_public_absent", "value": ""})
    stock_payload(payload, avail["avail"])
    payload.pop("stock_quantity", None)
    return _forbid_price(payload)


def plan_content_fill(product: dict, public: dict, *, images: Optional[Sequence[str]] = None, media_ok: bool = False) -> Optional[dict]:
    """Doplní len chýbajúce polia. Cenu, sklad aj status vynechá."""
    sku = normalize_article(str(product.get("sku") or ""))
    meta = meta_of(product)
    sk = slovak_content(public)
    payload: dict = {"id": int(product["id"]), "meta_data": []}
    if not name_is_ours(str(product.get("name") or ""), sku) and sk.get("name"):
        payload["name"] = sk["name"]
    if description_missing(str(product.get("description") or "")) and sk.get("description"):
        payload["description"] = f"<p>{html.escape(sk['description'])}</p>"
        if sk.get("short"):
            payload["short_description"] = f"<p>{html.escape(sk['short'])}</p>"
    if media_ok and images and needs_photo(product.get("images")):
        payload["images"] = [{"src": url} for url in list(images)[:8]]
        payload["meta_data"].append({"key": "_sg_image_source", "value": "wurth_public_ba"})
        payload["meta_data"].append({"key": "_sg_image_article", "value": sku})
    if pack_missing(meta) and public.get("pack_qty"):
        payload["meta_data"].append({"key": "_sg_pack_qty", "value": str(public["pack_qty"])})
        payload["meta_data"].append({"key": "_sg_pack_source", "value": "wurth_public_ba"})
        payload["meta_data"].append({"key": "_sg_pack_verified", "value": "1"})
    if not payload["meta_data"]:
        payload.pop("meta_data")
    if set(payload) <= {"id"}:
        return None
    return _forbid_price(payload)


def plan_draft(
    sku: str,
    public: dict,
    *,
    bucket: str,
    source_url: str,
    images: Optional[Sequence[str]] = None,
    media_ok: bool = False,
    category_path: Optional[Sequence[str]] = None,
    category_id: Optional[int] = None,
) -> dict:
    """Nový koncept. Bez ceny a bez skladu. Kategória len ak už v shope je."""
    sk = slovak_content(public)
    name = sk.get("name") or f"Würth {sku}"
    path = [str(part) for part in (category_path or []) if str(part).strip()]
    if category_id and not path:
        raise RuntimeError("kategória bez cesty")
    status = "mapped" if category_id else ("missing_in_shop" if path else "unclear")
    payload: dict = {
        "name": name,
        "sku": sku,
        "type": "simple",
        "status": "draft",
        "meta_data": [
            {"key": "_supplier_source", "value": "wurth"},
            {"key": "_sg_supplier", "value": "wurth"},
            {"key": "_sg_price_source", "value": ""},
            {"key": "_sg_catalog_source", "value": "wurth_public_ba"},
            {"key": "_sg_public_category", "value": bucket},
            {"key": "_sg_public_url", "value": source_url[:500]},
            {"key": "_sg_category_path", "value": " > ".join(path)},
            {"key": "_sg_category_status", "value": status},
        ],
    }
    if category_id:
        payload["categories"] = [{"id": int(category_id)}]
    if sk.get("description"):
        payload["description"] = f"<p>{html.escape(sk['description'])}</p>"
    if sk.get("short"):
        payload["short_description"] = f"<p>{html.escape(sk['short'])}</p>"
    if public.get("pack_qty"):
        payload["meta_data"].append({"key": "_sg_pack_qty", "value": str(public["pack_qty"])})
        payload["meta_data"].append({"key": "_sg_pack_source", "value": "wurth_public_ba"})
    if media_ok and images:
        payload["images"] = [{"src": url} for url in list(images)[:8]]
        payload["meta_data"].append({"key": "_sg_image_source", "value": "wurth_public_ba"})
        payload["meta_data"].append({"key": "_sg_image_article", "value": sku})
    if "status" in payload and payload["status"] != "draft":
        raise RuntimeError("nový Würth artikl musí byť koncept")
    return _forbid_price(payload)


def next_absence(
    previous: Dict[str, int],
    seen: Iterable[str],
    public: set,
    ours: Sequence[str],
) -> Tuple[Dict[str, int], List[str], List[str], List[str]]:
    """Nedostupné len ak artikl už vo verejnom sitemape bol a potom chýba dva behy.

    To, že ho Bosna alebo Kosovo nikdy nemali, nie je dôkaz, že slovenský
    artikl zanikol. ``seen`` sa rozšíri o tie, ktoré sú verejne dnes.
    """
    known_before = {str(sku) for sku in seen}
    known = set(known_before) | {sku for sku in ours if sku in public}
    state: Dict[str, int] = {}
    mark: List[str] = []
    restore: List[str] = []
    for sku in ours:
        if sku in public:
            if int(previous.get(sku) or 0) >= 2:
                restore.append(sku)
            continue
        if sku not in known_before:
            continue
        strikes = int(previous.get(sku) or 0) + 1
        state[sku] = strikes
        if strikes >= 2:
            mark.append(sku)
    return state, mark, restore, sorted(known)


def collect_public(http: PublicHttp, *, kosovo: bool) -> dict:
    """Jeden prechod sitemapov: artikle aj listové URL relevantných kategórií."""
    ba_pages: Dict[str, str] = {}
    relevant: List[str] = []
    index_xml = http.get_text(BA_SITEMAP)
    if "<sitemapindex" in (index_xml or "")[:800]:
        blobs = []
        for child in sitemap_locs(index_xml):
            if "wurth.ba" not in child:
                continue
            blobs.append(http.get_text(child))
    else:
        blobs = [index_xml]
    for blob in blobs:
        for loc in sitemap_locs(blob):
            article = article_from_ba_product_url(loc)
            if article:
                ba_pages.setdefault(article, loc)
            path = unquote(urlparse(loc).path)
            if bucket_for_path(path):
                relevant.append(path.strip("/"))
    kos_pages: Dict[str, str] = {}
    if kosovo:
        kos_pages = load_article_index(http, KOS_SITEMAP, article_from_kosovo_url)
    leaves = leaf_paths(relevant)
    buckets: Dict[str, int] = {}
    for path in leaves:
        bucket = bucket_for_path("/" + path) or "?"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return {
        "ba_pages": ba_pages,
        "kos_pages": kos_pages,
        "public": set(ba_pages) | set(kos_pages),
        "leaves": leaves,
        "buckets": buckets,
    }


def verified_image_urls(http: PublicHttp, page_html: str, sku: str) -> List[str]:
    """Fotky z overenej stránky, ktorých súbor je tento artikl a telo je skutočný JPEG."""
    from wurth_photo_backfill import image_is_real_photo, ordered_ba_candidates

    good: List[str] = []
    for group in ordered_ba_candidates(page_html, sku)[:4]:
        url = group[0]
        try:
            probe = http.inspect_image(url)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("fotka %s: %s", url[:120], exc)
            continue
        if image_is_real_photo(
            status=probe.status,
            content_type=probe.content_type,
            size=probe.size,
            magic=probe.magic,
            final_url=probe.final_url,
            sku=sku,
        ):
            good.append(probe.final_url.split("#")[0])
    return good


def host_allowed(http: PublicHttp, origin: str) -> bool:
    """Sitemap aj produktová cesta musia byť pre User-agent * dovolené."""
    robots_url = origin.rstrip("/") + "/robots.txt"
    assert_allowed_catalog_url(robots_url, context="wurth_catalog_sync")
    status, data, _final = http.get_bytes(robots_url, timeout=30)
    if status != 200:
        LOG.warning("robots %s HTTP %s", origin, status)
        return False
    text = data.decode("utf-8", "replace")
    ok_sitemap = robots_allows(text, "/sitemap.xml")
    ok_product = robots_allows(text, "/08931066/produkt")
    ok_search = robots_allows(text, "/search?q=08931066")
    LOG.info("robots %s sitemap=%s product=%s search=%s", origin, ok_sitemap, ok_product, ok_search)
    return ok_sitemap and ok_product and not ok_search


def _wc_request(method: str, path: str, **kwargs):
    from shop_guard import wait_if_slow
    import requests

    base = os.environ["WC_URL"].rstrip("/") + "/wp-json/wc/v3"
    auth = (os.environ["WC_CONSUMER_KEY"], os.environ["WC_CONSUMER_SECRET"])
    last = None
    for wait in (0, 15, 45):
        if wait:
            time.sleep(wait)
        wait_if_slow()
        response = requests.request(method, base + path, auth=auth, timeout=120, **kwargs)
        if response.status_code < 500 and response.status_code != 429:
            return response
        last = response
    return last


def fetch_shop_categories() -> List[dict]:
    """Existujúce Woo kategórie. Pri chybe prázdny zoznam — koncept ostane bez kategórie."""
    rows: List[dict] = []
    page = 1
    while page <= 30:
        response = _wc_request(
            "GET",
            "/products/categories",
            params={"per_page": 100, "page": page, "_fields": "id,name,parent"},
        )
        if response is None or not response.ok:
            LOG.warning("kategórie WC HTTP %s — nové koncepty ostanú bez kategórie", getattr(response, "status_code", 0))
            return []
        chunk = response.json()
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(row for row in chunk if isinstance(row, dict))
        if len(chunk) < 100:
            break
        page += 1
    return rows


def fetch_shop_wurth(limit: int) -> List[dict]:
    from account_split import mine

    out: List[dict] = []
    page = 1
    fields = "id,name,sku,description,short_description,images,meta_data,stock_status"
    while True:
        response = _wc_request(
            "GET",
            "/products",
            params={"search": "Würth", "status": "any", "per_page": 50, "page": page, "_fields": fields},
        )
        if response is None or not response.ok:
            LOG.error("WC list HTTP %s", getattr(response, "status_code", 0))
            break
        rows = response.json()
        for product in rows:
            meta = meta_of(product)
            source = str(meta.get("_supplier_source") or "").lower()
            if source != "wurth" and "eshop.wurth" not in str(meta.get("supplier_url") or ""):
                continue
            sku = normalize_article(str(product.get("sku") or ""))
            if not sku or not mine(sku):
                continue
            product = dict(product)
            product["sku"] = sku
            out.append(product)
            if limit and len(out) >= limit:
                return out
        if len(rows) < 50:
            break
        page += 1
    return out


def products_from_catalog(path: Path, limit: int) -> List[dict]:
    """Offline snímka 926 popisov. Fotky v nej nie sú, preto ich dry-run neháda."""
    from account_split import mine

    data = json.loads(path.read_text(encoding="utf-8"))
    pack = {}
    pack_path = ROOT / "data" / "wurth_pack_qty.json"
    if pack_path.exists():
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    out = []
    for sku, row in data.items():
        article = normalize_article(str(sku))
        if not article or not mine(article):
            continue
        qty = pack.get(article) or pack.get(sku)
        meta = []
        if qty:
            meta.append({"key": "_sg_pack_qty", "value": str(qty)})
        out.append({
            "id": None,
            "sku": article,
            "name": (row or {}).get("title") or "",
            "description": (row or {}).get("description") or "",
            "images": None,
            "meta_data": meta,
        })
        if limit and len(out) >= limit:
            break
    return out


def load_state(path: Path) -> Tuple[Dict[str, int], List[str]]:
    if not path.exists():
        return {}, []
    raw = json.loads(path.read_text(encoding="utf-8"))
    absent = {str(k): int(v) for k, v in (raw.get("absent") or {}).items()}
    seen = [str(sku) for sku in (raw.get("seen") or [])]
    return absent, seen


def save_state(path: Path, absent: Dict[str, int], seen: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "absent": absent,
        "seen": list(seen),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, ensure_ascii=False), encoding="utf-8")


def post_batches(creates: List[dict], updates: List[dict], batch: int) -> dict:
    created = updated = 0
    for key, rows, counter_name in (("create", creates, "created"), ("update", updates, "updated")):
        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            for item in chunk:
                _forbid_price(item)
            response = _wc_request("POST", "/products/batch", json={key: chunk})
            if response is None or not response.ok:
                LOG.error("batch %s HTTP %s", key, getattr(response, "status_code", 0))
                continue
            result = response.json().get(key) or []
            bad = [row for row in result if isinstance(row, dict) and row.get("error")]
            done = len(chunk) - len(bad)
            if counter_name == "created":
                created += done
            else:
                updated += done
            time.sleep(2)
    return {"created": created, "updated": updated}


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    parser = argparse.ArgumentParser(description="Würth public catalog sync (dry-run unless --apply)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--catalog", default="", help="JSON snímka namiesto Woo (dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="max našich produktov")
    parser.add_argument("--sample", type=int, default=0, help="koľko verejných stránok stiahnuť v dry-run")
    parser.add_argument("--new-limit", type=int, default=40, help="max nových konceptov v jednom behu")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--state", default=str(ROOT / "data" / STATE_NAME))
    args = parser.parse_args(argv)

    from media_licensing import wurth_media_allowed

    media_ok = wurth_media_allowed()
    http = PublicHttp(pause_s=1.0)
    http.session.headers["User-Agent"] = CATALOG_UA
    allowed = {}
    for origin in (f"https://{BA_HOST}", f"https://{KOS_HOST}"):
        try:
            allowed[origin] = host_allowed(http, origin)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("robots %s: %s", origin, exc)
            allowed[origin] = False
    if not allowed.get(f"https://{BA_HOST}"):
        print("SG_RESULT " + json.dumps({"ok": False, "error": "ba_robots_or_http"}, ensure_ascii=False))
        return 2

    public = collect_public(http, kosovo=bool(allowed.get(f"https://{KOS_HOST}")))
    if args.catalog:
        products = products_from_catalog(Path(args.catalog), args.limit)
    else:
        if not os.environ.get("WC_URL"):
            print("SG_RESULT " + json.dumps({"ok": False, "error": "no_wc_use_catalog_for_dry_run"}))
            return 2
        products = fetch_shop_wurth(args.limit)

    ours = [p["sku"] for p in products]
    our_set = set(ours)
    state_path = Path(args.state)
    previous, seen = load_state(state_path)
    absent, mark, restore, seen_now = next_absence(previous, seen, public["public"], ours)
    listed = sum(1 for sku in ours if sku in public["public"])
    prior_seen = set(seen)
    not_in_public = sum(1 for sku in ours if sku not in public["public"] and sku not in prior_seen)

    sample_pack_hit = sample_pack_n = 0
    sample_new = sample_known = sample_pages = 0
    if args.sample and not args.apply:
        have_page = [p for p in products if p["sku"] in public["ba_pages"] and pack_missing(meta_of(p))]
        for product in have_page[: args.sample]:
            page = http.get_text(public["ba_pages"][product["sku"]], timeout=30)
            parsed = parse_public_product(page)
            sample_pack_n += 1
            if parsed.get("article") == product["sku"] and parsed.get("pack_qty"):
                sample_pack_hit += 1
        for path in public["leaves"][: args.sample]:
            page = http.get_text(f"https://{BA_HOST}/{path}", timeout=30)
            parsed = parse_public_product(page)
            article = parsed.get("article") or ""
            if not article:
                continue
            sample_pages += 1
            if article in our_set:
                sample_known += 1
            else:
                sample_new += 1

    report = {
        "ok": True,
        "dry_run": not args.apply,
        "hosts_allowed": allowed,
        "our_products": len(products),
        "public_articles_ba": len(public["ba_pages"]),
        "public_articles_kosovo": len(public["kos_pages"]),
        "still_listed": listed,
        "absent_now": len(products) - listed,
        "not_in_public_catalog": not_in_public,
        "disappeared": len(absent),
        "seen_in_public_catalog": sum(1 for sku in seen_now if sku in our_set),
        "first_strike": sum(1 for n in absent.values() if n == 1),
        "would_mark_unavailable": len(mark),
        "would_restore": len(restore),
        "relevant_leaf_pages": len(public["leaves"]),
        "relevant_leaf_pages_note": "candidate URLs around cars; a leaf is a new article only when the page has data-product-code",
        "leaves_with_shop_category": sum(1 for path in public["leaves"] if slug_category("/" + path)),
        "leaves_category_unclear": sum(1 for path in public["leaves"] if not slug_category("/" + path)),
        "leaves_category_note": "from the public URL aisle; a product name can still map an unclear aisle, otherwise the draft stays uncategorized",
        "relevant_by_bucket": public["buckets"],
        "sample_pack_checked": sample_pack_n,
        "sample_pack_found": sample_pack_hit,
        "sample_new_pages_with_article": sample_pages,
        "sample_new_articles": sample_new,
        "sample_already_ours": sample_known,
        "names_already_ours": sum(1 for p in products if name_is_ours(str(p.get("name") or ""), p["sku"])),
        "descriptions_present": sum(1 for p in products if not description_missing(str(p.get("description") or ""))),
        "pack_missing": sum(1 for p in products if pack_missing(meta_of(p))),
        "photos_missing": (
            None if any(p.get("images") is None for p in products)
            else sum(1 for p in products if needs_photo(p.get("images")))
        ),
        "new_limit": args.new_limit,
        "price_policy": "no price written",
        "stock_policy": "Nedostupné only after 2 misses of an article previously seen in a public sitemap; never-listed articles stay untouched; no quantities",
        "media_ok": media_ok,
    }

    if not args.apply:
        print("SG_RESULT " + json.dumps(report, ensure_ascii=False), flush=True)
        return 0

    updates: List[dict] = []
    creates: List[dict] = []
    by_sku = {p["sku"]: p for p in products}
    for sku in mark:
        product = by_sku.get(sku)
        if product and product.get("id"):
            updates.append(plan_unavailable(int(product["id"])))
    for sku in restore:
        product = by_sku.get(sku)
        if not product or not product.get("id"):
            continue
        if str(meta_of(product).get("_sg_wurth_public_absent") or "") != ABSENT_MARK:
            continue
        updates.append(plan_return(int(product["id"]), str(meta_of(product).get("_sg_origin") or "")))

    for product in products:
        if product["sku"] not in public["ba_pages"] or not product.get("id"):
            continue
        if not needs_public_page(product, media_ok=media_ok):
            continue
        page = http.get_text(public["ba_pages"][product["sku"]], timeout=30)
        parsed = parse_public_product(page)
        if parsed.get("article") != product["sku"]:
            continue
        images = verified_image_urls(http, page, product["sku"]) if media_ok and needs_photo(product.get("images")) else []
        filled = plan_content_fill(product, parsed, images=images, media_ok=media_ok)
        if filled:
            updates.append(filled)

    from account_split import mine

    shop_categories = fetch_shop_categories()
    drafted = 0
    for path in public["leaves"]:
        if args.new_limit and drafted >= args.new_limit:
            break
        page_url = f"https://{BA_HOST}/{path}"
        page = http.get_text(page_url, timeout=30)
        parsed = parse_public_product(page)
        article = parsed.get("article") or ""
        if not article or article in our_set or not mine(article):
            continue
        canonical = public["ba_pages"].get(article) or ""
        if canonical and (not parsed.get("description") or not parsed.get("pack_qty") or not parsed.get("name")):
            richer_html = http.get_text(canonical, timeout=30)
            parsed = merge_public(parsed, parse_public_product(richer_html))
            if parsed.get("description") or parsed.get("pack_qty"):
                page = richer_html
                page_url = canonical
        bucket = bucket_for_path("/" + path) or ""
        images = verified_image_urls(http, page, article)[:1] if media_ok else []
        cat_path = shop_category_for("/" + path, slovak_content(parsed).get("name") or "")
        cat_id = existing_category_leaf(shop_categories, cat_path) if cat_path else None
        creates.append(plan_draft(
            article, parsed, bucket=bucket, source_url=page_url, images=images, media_ok=media_ok,
            category_path=cat_path, category_id=cat_id,
        ))
        our_set.add(article)
        drafted += 1

    written = post_batches(creates, updates, max(1, args.batch))
    save_state(state_path, absent, seen_now)
    report.update(written)
    report["drafts_planned"] = drafted
    report["unavailable_planned"] = len(mark)
    print("SG_RESULT " + json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
