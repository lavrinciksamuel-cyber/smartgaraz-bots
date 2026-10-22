#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
===============================================================================
SMARTGARÁŽ AI ENGINE — univerzálne AI spracovanie produktov pre WooCommerce
===============================================================================

Nahrádza staré, na konkrétneho dodávateľa naviazané skripty (Pro-Tec, Mopar...).
Tento engine je zdrojovo nezávislý — vstupom je jeden štandardizovaný dict
(`product_raw_data`) bez ohľadu na to, či dáta prišli z Pro-Tec, Würth, LKQ,
Szekop, Metabondu alebo akéhokoľvek budúceho importu.

Komunikuje priamo cez WooCommerce REST API (žiadne krehké `subprocess` volania
PHP skriptov cez SSH ako v starých botoch) a cez Anthropic Claude API.

-------------------------------------------------------------------------------
VSTUPNÝ FORMÁT `product_raw_data`
-------------------------------------------------------------------------------
{
    "wc_product_id": 1234,            # voliteľné — ak chýba, vytvorí sa nový draft
    "source": "Pro-Tec",              # voliteľné, len pre logovanie
    "name": "PRO-TEC DPF SUPER CLEAN 375ml",
    "sku": "P6171",
    "ean": "8594...",
    "brand": "PRO-TEC",               # voliteľné
    "description": "...",             # môže byť aj prázdne / veľmi krátke
    "short_description": "...",       # voliteľné
    "regular_price": "12.90",         # voliteľné
    "sale_price": "",                 # voliteľné
    "attributes": {"Objem": "375 ml"},# voliteľné - technické parametre
    "existing_images": [],            # voliteľné - zoznam URL, ak už nejaké má
    "status": "draft",                # voliteľné, default "draft"
}

-------------------------------------------------------------------------------
POUŽITIE
-------------------------------------------------------------------------------
    from ai_engine import process_product_with_ai

    result = process_product_with_ai(product_raw_data)
    print(result)

Alebo hromadne cez CLI:

    python ai_engine.py --input products.json
    python ai_engine.py --resync-missing        # dotiahne WC produkty s chudobným popisom
    python ai_engine.py --continuous            # spustí run_continuous_optimization_loop() na pozadí

Nepretržitá optimalizácia (beží donekonečna, kontroluje kvalitu celého katalógu
po dávkach a AI opravuje kategórie/popisy/obrázky/SEO tam, kde treba):

    from ai_engine import run_continuous_optimization_loop
    run_continuous_optimization_loop(interval_seconds=3600)

Potrebné premenné prostredia (.env alebo systémové):
    WC_URL, WC_CONSUMER_KEY, WC_CONSUMER_SECRET
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default claude-haiku-4-5-20251001)

Ak zvolený/predvolený model vráti HTTP 404 (napr. bol medzičasom vyradený —
pozri https://platform.claude.com/docs/en/about-claude/model-deprecations),
engine sa naň automaticky natrvalo prepne na fallback model claude-sonnet-4-6,
aby spracovanie produktov okamžite pokračovalo bez zaseknutia.
===============================================================================
"""

import os
import re
import sys
import json
import html
import time
import logging
import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup
from woocommerce import API as WooCommerceAPI

from brands import detect_true_brand, conflicts_with_other_brand, is_distributor_brand
from media_licensing import (
    filter_usable_supplier_images,
    looks_like_supplier_watermark,
    merge_image_provenance_into_meta,
)

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    from PIL import Image
    from io import BytesIO
except ImportError:
    Image = None


# ==============================================================================
# KONFIGURÁCIA
# ==============================================================================

@dataclass
class Config:
    wc_url: str = field(default_factory=lambda: os.getenv("WC_URL", ""))
    wc_consumer_key: str = field(default_factory=lambda: os.getenv("WC_CONSUMER_KEY", ""))
    wc_consumer_secret: str = field(default_factory=lambda: os.getenv("WC_CONSUMER_SECRET", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"))
    min_description_length: int = field(default_factory=lambda: int(os.getenv("AI_MIN_DESC_LEN", "100")))
    min_image_size_px: int = field(default_factory=lambda: int(os.getenv("AI_MIN_IMAGE_PX", "1000")))
    wp_user: str = field(default_factory=lambda: os.getenv("WP_USER", ""))
    wp_app_password: str = field(default_factory=lambda: os.getenv("WP_APP_PASSWORD", ""))
    product_image_size: int = field(default_factory=lambda: int(os.getenv("PRODUCT_IMAGE_SIZE", "800")))
    meta_description_limit: int = 155
    request_timeout: int = 20

    def validate(self, require_anthropic: bool = True) -> List[str]:
        missing = []
        if not self.wc_url:
            missing.append("WC_URL")
        if not self.wc_consumer_key:
            missing.append("WC_CONSUMER_KEY")
        if not self.wc_consumer_secret:
            missing.append("WC_CONSUMER_SECRET")
        if require_anthropic and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        return missing


LOGGER = logging.getLogger("ai_engine")
OPT_LOGGER = logging.getLogger("ai_engine.optimization")

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
OPTIMIZATION_STATE_FILE = os.path.join(LOGS_DIR, "optimization_state.json")
OPTIMIZATION_LOG_FILE = os.path.join(os.path.dirname(__file__), "optimization_log.txt")


def setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, "ai_engine.log")
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(stream),
        ],
        force=True,
    )


def setup_optimization_logging() -> None:
    """Samostatný logger pre `run_continuous_optimization_loop`, ktorý zapisuje
    podrobný priebeh do `bot/optimization_log.txt` aj do konzoly, nezávisle od
    hlavného `ai_engine.log`."""
    if OPT_LOGGER.handlers:
        return
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    # Rotuj príliš veľký log, aby diagnostika ostala čitateľná (UTF-8).
    try:
        if os.path.isfile(OPTIMIZATION_LOG_FILE) and os.path.getsize(OPTIMIZATION_LOG_FILE) > 2_000_000:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            os.makedirs(LOGS_DIR, exist_ok=True)
            os.replace(
                OPTIMIZATION_LOG_FILE,
                os.path.join(LOGS_DIR, f"optimization_log_archive_{stamp}.txt"),
            )
    except OSError:
        pass

    file_handler = logging.FileHandler(OPTIMIZATION_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    OPT_LOGGER.addHandler(file_handler)

    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    console_handler = logging.StreamHandler(stream)
    console_handler.setFormatter(formatter)
    OPT_LOGGER.addHandler(console_handler)

    OPT_LOGGER.setLevel(logging.INFO)
    OPT_LOGGER.propagate = False


def _load_optimization_state() -> Dict[str, Any]:
    try:
        with open(OPTIMIZATION_STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"last_page": 0}


def _save_optimization_state(state: Dict[str, Any]) -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(OPTIMIZATION_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def retry(times: int = 3, delay: float = 2.0, exceptions: Tuple[type, ...] = (Exception,)):
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            attempt, limit = 0, times
            while attempt < limit:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    msg = str(exc)
                    # Preťažený web (502/503/504/429) → viac pokusov s exponenciálnym čakaním
                    transient = any(c in msg for c in ("429", "502", "503", "504", "Bad Gateway", "Too Many Requests", "timed out"))
                    if transient:
                        limit = max(limit, 7)
                    LOGGER.warning("Pokus %d/%d zlyhal pre %s: %s", attempt, limit, func.__name__, exc)
                    if attempt < limit:
                        time.sleep(min(120.0, (5.0 * 2 ** (attempt - 1)) if transient else delay * attempt))
            raise last_exc
        return wrapper
    return decorator


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


# AI/copy občas „pretečie“ do ruštiny/cyriliky (P1301: mechanických частей).
# Mapovanie bežných únikov + lookalike písmen (а→a) pred uložením do WC.
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_CYRILLIC_PHRASE_FIXES: Tuple[Tuple[str, str], ...] = (
    (r"частей", "častí"),
    (r"частиц", "častíc"),
    (r"части", "časti"),
    (r"износом", "opotrebovaním"),
    (r"износу", "opotrebovaniu"),
    (r"износ", "opotrebovanie"),
    (r"движением", "pohybom"),
    (r"французский", "francúzsky"),
    (r"немецкому", "nemeckému"),
    (r"германская", "nemecká"),
    (r"герман", "nemeck"),
    (r"типами", "typmi"),
    (r"известны", "známe"),
    (r"международ", "medzinárod"),
    (r"припарк", "parkov"),
    (r"олей", "olej"),
    (r"британ", "britán"),
)
# Len vizuálne homoglyfy (Cyr ↔ Latin), nie „preklad“ celých ruských slov.
_CYR_LOOKALIKE = str.maketrans(
    {
        "\u0430": "a",
        "\u0410": "A",  # а А
        "\u0435": "e",
        "\u0415": "E",  # е Е
        "\u043e": "o",
        "\u041e": "O",  # о О
        "\u0440": "p",
        "\u0420": "P",  # р Р
        "\u0441": "c",
        "\u0421": "C",  # с С
        "\u0443": "y",
        "\u0423": "Y",  # у У
        "\u0445": "x",
        "\u0425": "X",  # х Х
        "\u0456": "i",
        "\u0406": "I",  # і І
        "\u0458": "j",
        "\u0408": "J",  # ј Ј
        "\u0451": "e",
        "\u0401": "E",  # ё Ё
    }
)


def contains_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def _case_aware_repl(canonical: str):
    """Zachovaj veľkosť prvého písmena zo zdroja (Stěrač → Stierač).

    Pri `re.I` + Unicode môže `group(0)` vrátiť folded case — berieme
    znak priamo z `match.string`.
    """

    def _inner(match: re.Match) -> str:
        start = match.start()
        end = match.end()
        original = match.string[start:end]
        first = match.string[start] if start < len(match.string) else ""
        if original.isupper():
            return canonical.upper()
        if first.isupper():
            return canonical[:1].upper() + canonical[1:]
        return canonical

    return _inner


# TecDoc/Szekop PL/CZ mojibake → správna SK ortografia (názvy produktov).
_SK_PRODUCT_NAME_FIXES = (
    (re.compile(r"\bliżta\b", re.I), _case_aware_repl("lišta")),
    (re.compile(r"\blizta\b", re.I), _case_aware_repl("lišta")),
    (re.compile(r"\bliźta\b", re.I), _case_aware_repl("lišta")),
    (re.compile(r"\bstěrač", re.I), _case_aware_repl("stierač")),
    (re.compile(r"\bstěrače\b", re.I), _case_aware_repl("stierače")),
    (re.compile(r"\bstěrací\b", re.I), _case_aware_repl("stierací")),
)


def normalize_sk_product_name(text: str) -> str:
    """Oprav PL/CZ preklepy v názve + cyriliku (ak je)."""
    if not text:
        return text
    out = text
    for pattern, repl in _SK_PRODUCT_NAME_FIXES:
        out = pattern.sub(repl, out)
    try:
        from sk_part_names import normalize_part_name

        out = normalize_part_name(out)
    except Exception:
        pass
    return sanitize_sk_copy(out)


def sanitize_sk_copy(text: str) -> str:
    """Odstráň skryté formátovanie + cyriliku / lookalike z AI SK copy.

    Nikde na shope nesmie zostať fingerprint (zero-width, soft hyphen, display:none…).
    """
    if not text:
        return text
    out = strip_hidden_formatting(text)
    if not contains_cyrillic(out):
        return out
    for pattern, repl in _CYRILLIC_PHRASE_FIXES:
        out = re.sub(pattern, repl, out, flags=re.I)
    out = out.translate(_CYR_LOOKALIKE)
    # Zvyšok cyriliky — vyhoď runy, nech nezostane narušený text
    out = re.sub(r"[\u0400-\u04FF]+", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


# Neviditeľné / fingerprint Unicode (watermark v texte z cudzích katalógov).
_HIDDEN_UNICODE_RE = re.compile(
    "["
    "\u00AD"  # soft hyphen
    "\u034F"  # combining grapheme joiner
    "\u061C"  # arabic letter mark
    "\u180E"  # mongolian vowel separator
    "\u200B-\u200F"  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "\u202A-\u202E"  # bidi embeddings/overrides
    "\u2060-\u2064"  # word joiner, invisible plus…
    "\u2066-\u2069"  # isolate controls
    "\uFEFF"  # BOM / ZWNBSP
    "\uFFF9-\uFFFB"  # interlinear annotation
    "]"
)

# CSS hinty skrytého obsahu v inline style / class.
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:"
    r"display\s*:\s*none|"
    r"visibility\s*:\s*hidden|"
    r"opacity\s*:\s*0(?:\.0+)?|"
    r"font-size\s*:\s*0(?:px|pt|em|rem)?|"
    r"height\s*:\s*0(?:px|pt|em|rem)?|"
    r"width\s*:\s*0(?:px|pt|em|rem)?|"
    r"clip\s*:\s*rect\s*\(|"
    r"position\s*:\s*absolute[^;]*(?:left|top)\s*:\s*-?\d{3,}"
    r")",
    re.I,
)


def strip_hidden_formatting(text: str) -> str:
    """Vyhoď skryté HTML/Unicode — žiadny fingerprint ani neviditeľný text.

    - HTML komentáre, ``<script>``, ``<style>``
    - elementy s display:none / visibility:hidden / opacity:0 / font-size:0
    - aria-hidden / hidden atributy
    - zero-width znaky, soft hyphen, bidi override, BOM
    """
    if not text:
        return ""
    out = str(text)
    out = _HIDDEN_UNICODE_RE.sub("", out)
    if "<" in out and ">" in out:
        try:
            from bs4 import Comment

            soup = BeautifulSoup(out, "html.parser")
            for tag in soup.find_all(["script", "style", "noscript", "template"]):
                tag.decompose()
            for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
                c.extract()
            for tag in list(soup.find_all(True)):
                style_blob = " ".join(
                    filter(
                        None,
                        [
                            tag.get("style") or "",
                            " ".join(tag.get("class") or []),
                        ],
                    )
                )
                if _HIDDEN_STYLE_RE.search(style_blob):
                    tag.decompose()
                    continue
                if str(tag.get("aria-hidden") or "").lower() == "true" or tag.has_attr("hidden"):
                    tag.decompose()
            out = str(soup)
            out = _HIDDEN_UNICODE_RE.sub("", out)
        except Exception:
            out = _HIDDEN_UNICODE_RE.sub("", out)
    out = _HIDDEN_UNICODE_RE.sub("", out)
    return out


def build_meta_title(name: str, limit: int = 60, suffix: str = " | SMARTgaráž") -> str:
    """Vynúti presný formát meta titulku '[Názov produktu] | SMARTgaráž' bez ohľadu
    na to, čo presne vrátila AI — deterministicky orezané na `limit` znakov."""
    name = " ".join((name or "").split())
    max_name_len = limit - len(suffix)
    if len(name) > max_name_len:
        name = name[: max_name_len - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return f"{name}{suffix}"


# Veľké sudy / dielenské sudy — retail shop ich nepredáva (publish_ready ich aj tak blokuje).
# Negatívny lookbehind: neber `0,25 l` / `0.25 l` (bežné aditíva 250 ml) ako sud 25 L.
BULK_PACK_RE = re.compile(
    r"(?<![\d.,])\b(20|25|55|58|60|200|205|208)\s*(?:l|liter|litrov)\b"
    r"|(?<![\d.,])\b(20|25|55|58|60|200|205|208)L\b",
    re.I,
)

# „40 až 60 litrov nafty/paliva“ = dávkovanie aditíva, nie 60 L sud.
_BULK_DOSING_AFTER_RE = re.compile(
    r"^\s*(?:paliva|nafty|benzínu|benzinu|fuel|diesel|oleja|motorového\s+oleja)\b",
    re.I,
)


def is_bulk_pack(raw_data: Dict[str, Any]) -> bool:
    """True pre 20–208 L sudy — v cheap/bulk importe ich skipujeme (€0 + žiadny balast)."""
    blob = " ".join(
        str(raw_data.get(k) or "")
        for k in ("name", "sku", "short_description", "description")
    )
    attrs = raw_data.get("attributes") or {}
    if isinstance(attrs, dict):
        blob += " " + " ".join(str(v) for v in attrs.values())
    for match in BULK_PACK_RE.finditer(blob):
        after = blob[match.end() : match.end() + 48]
        if _BULK_DOSING_AFTER_RE.search(after):
            continue
        return True
    return False


def is_supplier_in_stock(raw_data: Dict[str, Any]) -> bool:
    """True ak dodávateľ hlási kusy na sklade (qty > 0), alebo jasný instock bez qty.

    Pri známom `stock_qty` rozhoduje výhradne množstvo (0 = nie na sklade).
    `onbackorder` / `outofstock` = nie. Neznámy stav bez qty = nie (radšej skip).
    """
    # LKQ: 0 ks v SR, ale platná dodacia lehota („bežne do 48h/96h/5 dní“) = Sklad v EÚ → importovať
    if str(raw_data.get("source") or "").lower() == "lkq":
        try:
            from availability import classify_raw

            _av = classify_raw(raw_data)
            if _av and _av["avail"] == "eu":
                return True
        except Exception:  # noqa: BLE001
            pass
    status = str(raw_data.get("stock_status") or "").lower().strip().replace("_", "")
    qty_raw = raw_data.get("stock_qty")
    qty: Optional[float]
    try:
        qty = float(qty_raw) if qty_raw is not None and str(qty_raw).strip() != "" else None
    except (TypeError, ValueError):
        qty = None

    if qty is not None:
        return qty > 0
    if status in {"outofstock", "outof", "onbackorder", "backorder"}:
        return False
    if status in {"instock", "available"}:
        return True
    # delivery_text tip (SK/EN)
    delivery = str(raw_data.get("delivery_text") or "").lower()
    if any(k in delivery for k in ("skladom", "na sklade", "in stock", "available")):
        return True
    if any(k in delivery for k in ("vypredané", "nedostupné", "out of stock", "na objednávku")):
        return False
    return False


def _normalize_pack_volume_label(text: str) -> str:
    """'5L' / '355ML' / '0,946 L' → '(5 l)' / '(355 ml)' / '(0,946 l)'."""
    m = re.search(
        r"\b(\d{1,4}(?:[.,]\d+)?)\s*(ml|l|liter|litrov)\b",
        (text or ""),
        flags=re.I,
    )
    if not m:
        return ""
    value = m.group(1).replace(",", ".")
    unit = m.group(2).lower()
    unit = "ml" if unit == "ml" else "l"
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return f"({value} {unit})"


def _strip_html_to_text(value: str) -> str:
    if not value:
        return ""
    if "<" in value and ">" in value:
        try:
            return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        except Exception:
            pass
    return " ".join(value.split())


def strip_supplier_catalog_junk(text: str) -> str:
    """Odstráň Würth/B2B katalógový šum (Ďalší tovar, štruktúra obj. čísla…).

    Dôležité: NIKDY nenechávaj B2B nákupné ceny („X € Y € bez DPH“) v shop
    popise — to je náklad/veľkoobchod, nie predajná cena pre zákazníka.
    """
    if not text:
        return ""
    out = strip_hidden_formatting(_strip_html_to_text(text))
    for marker in (
        "Ďalší tovar:",
        "Dalsi tovar:",
        "Objednávacie císlo Würth",
        "Objednávacie číslo Würth",
        "Císlo tovaru Würth",
        "Číslo tovaru Würth",
        "Balenie uvádza počet artiklov",
    ):
        idx = out.find(marker)
        if idx >= 0:
            out = out[:idx]
    out = re.sub(
        r"\b(Opis|Použitie|Technické informácie|Dokumenty|Príslušenstvo|Upozornenie|"
        r"Oblasť použitia|Informácie o výrobku|Zahrnuté v dodávke|Obsah balenia)\b",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(r"Zobraziť (viac|menej)(?:\.\.\.|…)", " ", out, flags=re.I)
    out = re.sub(r"Katalóg spoločnosti\s*Würth", " ", out, flags=re.I)
    out = re.sub(r"Hárky(?:\s*údajov)?(?:\s*\([^)]*\))?\.?(?:\.\.\.|…)?", " ", out, flags=re.I)
    out = re.sub(r"Certifikáty\s*/\s*Dokumenty", " ", out, flags=re.I)
    # Echo z našej cheap šablóny — inak sa „Balenie 300 ml“ zacyklí na zásuvkách
    out = re.sub(r"Zaradené v kategórii\s+[^.]{1,80}\.", " ", out, flags=re.I)
    out = re.sub(
        r"\bBalenie\s+\d{1,5}(?:[.,]\d+)?\s*(?:ml|l|ks)\b\.?",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"Over kompatibilitu podľa OE(?:\s*/\s*viskozity)? pred nákupom\.",
        " ",
        out,
        flags=re.I,
    )
    # PRO-TEC / B2B UI scrap: nákupná cena + „bez DPH“ + jednotková cena
    out = re.sub(
        r"(?:\d{1,5}[.,]\d{2}\s*€?\s*){1,4}(?:bez\s*DPH|s\s*DPH)\b",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"\b\d{1,5}[.,]\d{2}\s*€\s+\d{1,5}[.,]\d{2}\s*€(?:\s*bez\s*DPH)?\b",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(r"\bJednotková\s+cena\s*:?", " ", out, flags=re.I)
    out = re.sub(
        r"Priemerné\s+hodnotenie\s+produktu[^.]*\.",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(r"\d+\s+hodnoten(?:ie|ia|í)\b", " ", out, flags=re.I)
    out = re.sub(r"Podrobnosti\s+hodnotenia", " ", out, flags=re.I)
    out = re.sub(r"Pridať\s+do\s+koš[ií]ka", " ", out, flags=re.I)
    out = re.sub(r"Pridať\s+do\s*…?", " ", out, flags=re.I)
    out = re.sub(r"Skladom\s*\+\s*[−\-]\s*", " ", out, flags=re.I)
    out = re.sub(
        r"\b(?:Tlač|Opýtať\s+sa|Zdieľať|Tweet|Zavrieť|Detailné\s+informácie)\b",
        " ",
        out,
        flags=re.I,
    )
    out = re.sub(r"\s+", " ", out).strip(" -–|;…")
    return out


def extract_pack_piece_label(raw_data: Dict[str, Any]) -> str:
    """'(100 ks)' z názvu / riadku dodávateľa viazaného na SKU (Würth x 100 ks).

    Čítaj PRED strip_supplier_catalog_junk — počet ks býva v bloku „Ďalší tovar“.
    """
    sku = str(raw_data.get("sku") or "").strip()
    blob = _strip_html_to_text(
        " ".join(
            str(raw_data.get(k) or "")
            for k in ("name", "short_description", "description", "attributes")
        )
    )
    m = re.search(r"\((\d{1,5})\s*ks\)", blob, flags=re.I)
    if m:
        return f"({m.group(1)} ks)"
    if sku:
        m = re.search(
            rf"(?:Obj\.?\s*č\.?\s*)?{re.escape(sku)}[\s\S]{{0,240}}?\bx\s*(\d{{1,5}})\s*ks\b",
            blob,
            flags=re.I,
        )
        if m:
            return f"({m.group(1)} ks)"
        # opačný porad: „x 100 ks … Obj. č. SKU“
        m = re.search(
            rf"\bx\s*(\d{{1,5}})\s*ks\b[\s\S]{{0,160}}?(?:Obj\.?\s*č\.?\s*)?{re.escape(sku)}",
            blob,
            flags=re.I,
        )
        if m:
            return f"({m.group(1)} ks)"
    # Visačka: vlastné SKU často nie je v „Ďalší tovar“, ale súrodenec áno (× 100 ks).
    if re.search(r"visačk|visack|inspection\s*tag|inspektion", blob, flags=re.I):
        tag_packs = re.findall(
            r"In[šs]pekčn[áa]\s+visačka[\s\S]{0,220}?\bx\s*(\d{1,5})\s*ks\b",
            blob,
            flags=re.I,
        )
        if len(set(tag_packs)) == 1:
            return f"({tag_packs[0]} ks)"
    found = re.findall(r"\bx\s*(\d{1,5})\s*ks\b", blob, flags=re.I)
    uniq = list(dict.fromkeys(found))
    if len(uniq) == 1:
        return f"({uniq[0]} ks)"
    return ""


def build_sk_product_name(raw_data: Dict[str, Any]) -> str:
    """Deterministický SK názov bez Claude — značka + vyčistený názov + objem."""
    brand = (raw_data.get("brand") or "").strip()
    name = " ".join((raw_data.get("name") or "").split())
    if not name:
        name = (raw_data.get("sku") or "Produkt").strip()

    # Objem v názve → jednotný tvar (355 ml)
    def _vol_sub(m: re.Match) -> str:
        value = m.group(1).replace(",", ".")
        unit = m.group(2).lower()
        unit = "ml" if unit == "ml" else "l"
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        return f"{value} {unit}"

    name = re.sub(
        r"\b(\d{1,4}(?:[.,]\d+)?)\s*(ml|l|liter|litrov)\b",
        _vol_sub,
        name,
        flags=re.I,
    )
    # Odstráň duplicitnú značku na začiatku
    if brand and name.lower().startswith(brand.lower()):
        rest = name[len(brand):].lstrip(" -–|:")
        name = f"{brand} {rest}".strip() if rest else brand
    elif brand and brand.lower() not in name.lower():
        name = f"{brand} {name}".strip()

    vol = _normalize_pack_volume_label(name)
    if vol and vol not in name:
        name = f"{name} {vol}".strip()
    pack_pcs = extract_pack_piece_label({**raw_data, "name": name})
    if pack_pcs and pack_pcs.lower() not in name.lower():
        name = f"{name} {pack_pcs}".strip()
    return truncate(normalize_sk_product_name(name), 180)


def compose_part_title(raw_data: Dict[str, Any], base_name: str,
                       category_path: Optional[List[str]] = None) -> str:
    """Náhradný diel: „Typ Značka Kód (OE xxx) – Auto“ (napr. Alternátor AS-PL A3299
    12 V 140 A (OE 03L903023L) – Škoda Octavia III 1.6 TDI).

    Auto sa pridá len pri presnej generácii (inak bez auta). Bez OE → pôvodný názov.
    """
    path = " ".join(category_path or []).lower()
    if "náhradné diely" not in path and "nahradne diely" not in path:
        return base_name
    oes = [str(o).strip() for o in (raw_data.get("oe_numbers") or []) if str(o).strip()]
    if not oes:
        return base_name
    brand = (raw_data.get("brand") or "").strip()
    sku = (raw_data.get("sku") or "").strip()
    code = re.sub(r"^[A-Z0-9]{1,4}\s+", "", sku) if " " in sku else sku
    typ = base_name
    if brand:
        typ = re.sub(re.escape(brand), " ", typ, flags=re.I)
    if code:
        typ = typ.replace(code, " ")
    typ = re.sub(r"\s{2,}", " ", typ).strip(" ,-–")
    if typ[:1].islower():
        typ = typ[:1].upper() + typ[1:]
    title = " ".join(x for x in (typ, brand, code) if x)
    title += f" (OE {oes[0]})"
    try:
        from fitment import parse_fitment_from_supplier

        fit = parse_fitment_from_supplier(raw_data) or {}
        if str(fit.get("sg_id") or "").startswith("sgv-gen-") and fit.get("generation"):
            make = "Škoda" if str(fit.get("make")).lower() == "skoda" else str(fit.get("make"))
            car = " ".join(
                x for x in (make, str(fit.get("model") or ""), str(fit.get("generation") or ""),
                            str(fit.get("engine") or "")) if x
            )
            title += f" – {car}"
    except Exception:
        pass
    return truncate(title, 180)


def uses_vehicle_compatibility_copy(
    category_path: Optional[List[str]] = None,
    raw_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """True len pre autodiely / oleje / filtre — nie dielenské náradie.

    Cheap šablóna aj AI prompt: OE / SAE / fitment blurb len tam, kde dáva zmysel.
    Würth pištoľ na kartuše ∈ Náradie → False.
    """
    path = [str(p or "").strip() for p in (category_path or []) if p]
    path_l = " › ".join(path).lower()
    # Explicitné náradie / dielňa / chémia aplikátorov
    if path and path[0] in (
        "Náradie & Vybavenie",
        "Dielenská chémia & Údržba",
    ):
        # Výnimka: Motor & olej – náradie stále nie je náhradný diel vozidla
        return False
    if any(
        k in path_l
        for k in (
            "náradie",
            "naradie",
            "dielenské vybavenie",
            "dielenske vybavenie",
            "diagnostika",
            "etikety",
            "kľúče",
            "kluce",
            "momentové",
            "momentove",
        )
    ):
        return False
    blob = " ".join(
        str((raw_data or {}).get(k) or "")
        for k in ("name", "brand", "sku", "description", "short_description")
    ).lower()
    # Názov jasne náradie / aplikátor
    if re.search(
        r"\b(pišto[ľl]|pistole|vytláčac|vytlacac| dispens|kľúč|kluc|račne|racne|"
        r"skrutkovač|skrutkovac|zdvihák|zdvihak|hever|kompresor|svetlo|"
        r"brúska|bruska|kladivo|klieste|kliešte)\b",
        blob,
        flags=re.I,
    ):
        return False
    # Oleje / filtre / autodiely / kvapaliny
    if any(
        k in path_l
        for k in (
            "olej",
            "kvapalin",
            "filter",
            "filtre",
            "autodiel",
            "brzd",
            "podvozo",
            "motor",
            "prevodov",
            "chladen",
            "stierač",
            "stierac",
            "žiarov",
            "ziarov",
            "zapalovan",
            "zapaľovan",
        )
    ):
        return True
    if path and path[0] in (
        "Motorové oleje & Kvapaliny",
        "Filtre",
        "Autodiely",
        "Osvetlenie & Žiarovky",
        "Brzdy & Podvozok",
    ):
        return True
    # Default: bez OE/SAE blurbu (bezpečnejšie než falošná kompatibilita)
    return False


def build_template_content(
    raw_data: Dict[str, Any],
    category_path: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """€0 copywriting: šablóna z dodávateľských dát (žiadne Claude volanie).

    Kvalita je slabšia ako AI inzerát, ale stačí na bulk katalogizáciu olejov/filtrov.
    Neskôr sa dá dobehnúť plným AI len na top predajoch.
    """
    pack_pcs = extract_pack_piece_label(raw_data)
    product_name_sk = compose_part_title(
        raw_data, build_sk_product_name(raw_data), category_path
    )
    brand = (raw_data.get("brand") or "").strip()
    sku = (raw_data.get("sku") or "").strip()
    ean = (raw_data.get("ean") or "").strip()
    leaf = (category_path or ["Produkt"])[-1] if category_path else "Produkt"
    vehicle_compat = uses_vehicle_compatibility_copy(category_path, raw_data)
    supplier_short = strip_supplier_catalog_junk(str(raw_data.get("short_description") or ""))
    supplier_full = strip_supplier_catalog_junk(str(raw_data.get("description") or ""))
    # Objem len z názvu / atribútov — NIE z short/desc (tam môže ostať staré „Balenie 300 ml“)
    vol = _normalize_pack_volume_label(
        " ".join([product_name_sk, str(raw_data.get("attributes") or "")])
    )
    pack_label = pack_pcs.strip("()") if pack_pcs else ""
    # Kusovka (zásuvka, spojka…) — nikdy nedávaj ml/l balenie
    name_l = product_name_sk.lower()
    looks_piece = bool(
        pack_pcs
        or re.search(
            r"\b(zásuvk|zasuvk|spojk|konektor|kontakt|senzor|čidl|cidl|filter|"
            r"visačk|visack|krúžok|kruzok|skrutk|tesnen|hadic|adaptér|adapter)\b",
            name_l,
            flags=re.I,
        )
    )
    if looks_piece:
        vol = ""

    # Würth junk často „vyzerá“ ako dlhý short — po strip treba vlastnú krátku vetu
    if supplier_short and len(supplier_short) >= 40 and "Ďalší tovar" not in supplier_short:
        short_description = truncate(supplier_short, 420)
        if pack_label and pack_label.lower() not in short_description.lower():
            short_description = truncate(f"{short_description} Balenie {pack_label}.", 420)
    else:
        bits = [product_name_sk]
        if leaf and leaf != "Produkt":
            bits.append(f"Zaradené v kategórii {leaf}.")
        if pack_label:
            bits.append(f"Balenie {pack_label}.")
        elif vol:
            bits.append(f"Balenie {vol.strip('()')}.")
        if supplier_full:
            bits.append(truncate(supplier_full, 220))
        elif vehicle_compat:
            bits.append("Over kompatibilitu podľa OE / viskozity pred nákupom." if re.search(r"olej|kvapalin", name_l)
                        else "Pred nákupom over kompatibilitu podľa OE čísla.")
        else:
            bits.append("Použi podľa návodu výrobcu a určenia produktu.")
        short_description = " ".join(bits)

    path_blob = " ".join(category_path or []).lower()
    is_oil = bool(re.search(r"\boleje?\b|kvapalin|\bsae\b|\b\d{1,2}w-?\d{2}\b", f"{path_blob} {name_l}"))
    if vehicle_compat:
        # SAE viskozita len pri olejoch/kvapalinách — skrutka, filter, … nie.
        compat_hint = (
            "OE čísla, SAE viskozity alebo schválenia výrobcu vozidla" if is_oil
            else "OE čísla dielu a údajov o vozidle"
        )
        desc_fallback = (
            f"{product_name_sk} — autodielenský sortiment SmartGaráž. "
            + ("Technické údaje podľa výrobcu; pri neistote over OE číslo alebo SAE viskozitu."
               if is_oil else "Technické údaje podľa výrobcu; pri neistote over OE číslo.")
        )
        compat_block = (
            f"<h3>Kompatibilita</h3>\n"
            f"<p>Pred montážou / výmenou over kompatibilitu podľa {compat_hint}.</p>"
        )
    else:
        desc_fallback = (
            f"{product_name_sk} — dielenský sortiment SmartGaráž. "
            "Technické údaje podľa výrobcu; dodrž návod na použitie a bezpečnosť práce."
        )
        compat_block = (
            f"<h3>Použitie &amp; Aplikácia</h3>\n"
            f"<p>Použi podľa návodu výrobcu a určenia nástroja / prípravku. "
            f"Dodrž bezpečnosť práce a odporúčané príslušenstvo.</p>"
        )

    desc_body = supplier_full or supplier_short or desc_fallback
    desc_body = truncate(desc_body, 2500)
    rows = [
        ("Výrobca", brand or "—"),
        ("Kód produktu / SKU", sku or "—"),
        ("EAN kód", ean or "—"),
        ("Počet v balení", pack_label or "—"),
        ("Objem/Hmotnosť", vol.strip("()") if vol else "—"),
        ("Kategória", " › ".join(category_path) if category_path else "—"),
    ]
    # Bez tabuliek (dizajn) — čistý zoznam, prázdne „—“ riadky vynechaj.
    table_rows = "".join(
        f"<li><strong>{html.escape(k)}:</strong> {html.escape(v)}</li>"
        for k, v in rows if v and v != "—" and k != "Kategória"
    )
    full_description = (
        f"<h2>{html.escape(product_name_sk)}</h2>\n"
        f"<p>{html.escape(desc_body)}</p>\n"
        f"<h3>Identifikačné a technické údaje</h3>\n"
        f"<ul>{table_rows}</ul>\n"
        f"{compat_block}"
    )

    focus = " ".join(x for x in [brand, leaf, vol.strip("()") if vol else ""] if x).strip() or product_name_sk
    # Tagy len vecné (kategória, objem) — značka ide do brand taxonómie,
    # SKU/„autodiely“/„SmartGaráž“/„Produkt“ ako tag = šum.
    tags = []
    for t in (leaf if leaf != "Produkt" else "", vol.strip("()") if vol else ""):
        if t and t not in tags:
            tags.append(t)
    search_terms = []
    for t in (
        product_name_sk,
        f"{brand} {leaf}".strip(),
        sku,
        ean,
        f"{leaf} kúpiť".strip(),
        f"{brand} {vol.strip('()')}".strip() if brand and vol else "",
        leaf,
        "autodiely online",
    ):
        if t and t not in search_terms:
            search_terms.append(t)

    # Würth (a iné) parser už pripravil čistý popis bez tabuliek → použi ho.
    _extra = raw_data.get("raw_extra") if isinstance(raw_data.get("raw_extra"), dict) else {}
    if _extra.get("clean_html") and raw_data.get("description"):
        full_description = str(raw_data["description"])
        if raw_data.get("short_description"):
            short_description = re.sub(r"<[^>]+>", "", str(raw_data["short_description"])).strip()
    if focus.strip().lower() in ("produkt", f"{brand} produkt".strip().lower()):
        focus = re.sub(rf"^{re.escape(brand)}\s+", "", product_name_sk) if brand else product_name_sk

    seo = {
        "focus_keyword": truncate(focus, 80),
        "alt_text": truncate(f"{product_name_sk} — {leaf}", 120),
        "meta_title": build_meta_title(product_name_sk),
        "meta_description": truncate(
            f"{product_name_sk}. {short_description}",
            155,
        ),
        "image_description": truncate(product_name_sk, 120),
        "tags": tags[:8],
    }
    return {
        "product_name_sk": sanitize_sk_copy(product_name_sk),
        "short_description": sanitize_sk_copy(short_description),
        "full_description": sanitize_sk_copy(full_description),
        "seo_rank_math": seo,
        "search_terms": search_terms[:8],
        "cheap_template": True,
    }


# ==============================================================================
# 1) DYNAMICKÁ KATEGORIZÁCIA + WOOCOMMERCE KLIENT
# ==============================================================================

class WooCommerceClient:
    """Tenká vrstva nad WooCommerce REST API. Kategórie sa cachujú v pamäti,
    aby sa pri hromadnom spracovaní produktov zbytočne neopakovali API volania."""

    STOCK_IMAGE_BLACKLIST = (
        "shutterstock.com", "istockphoto.com", "alamy.com", "dreamstime.com",
        "gettyimages.com", "123rf.com", "depositphotos.com", "adobe.com/stock",
        "vecteezy.com", "freepik.com", "pngtree.com", "rawpixel.com",
        # Značkové/firemné logo galérie — NIKDY nie sú fotky produktu, ale
        # obrázkové vyhľadávanie ich vie občas vrátiť pri všeobecnejších
        # dopytoch (napr. keď názov produktu obsahuje bežné anglické slovo).
        "logos-world.net", "seeklogo.com", "worldvectorlogo.com", "brandlogos.net",
        "freepnglogos.com", "logopng", "1000logos.net", "logotypes101.com",
        "wikipedia.org", "wikimedia.org",
    )

    def __init__(self, config: Config):
        self.config = config
        self.api = WooCommerceAPI(
            url=config.wc_url,
            consumer_key=config.wc_consumer_key,
            consumer_secret=config.wc_consumer_secret,
            version="wc/v3",
            timeout=config.request_timeout,
        )
        self._category_cache: Dict[Tuple[str, int], int] = {}
        self._categories_loaded = False
        self._brand_cache: Dict[str, int] = {}
        self._brands_loaded = False
        self._sku_cache: Dict[str, int] = {}

    # ---- Značky (WooCommerce Brands / product_brand) -------------------

    def _load_all_brands(self) -> None:
        if self._brands_loaded:
            return
        page = 1
        while page <= 20:
            try:
                resp = self.api.get("products/brands", params={"per_page": 100, "page": page})
            except Exception as exc:
                LOGGER.warning("Načítanie brands zlyhalo: %s", exc)
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for brand in data:
                bid = int(brand["id"])
                self._brand_cache[(brand.get("name") or "").strip().lower()] = bid
                self._brand_cache[(brand.get("slug") or "").strip().lower()] = bid
            page += 1
        self._brands_loaded = True
        LOGGER.info("🏷️  Načítaných WC značiek: %d kľúčov", len(self._brand_cache))

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def ensure_brand(self, name: str) -> Optional[int]:
        """Zabezpečí brand term a vráti jeho ID (pre payload `brands`)."""
        display = (name or "").strip()
        if not display:
            return None
        # Preferované zobrazenie pre pár známych aliasov
        aliases = {
            "wurth": "Würth",
            "würth": "Würth",
            "mopar": "MOPAR",
            "dfu": "DFU",
            "kent": "KENT",
            "pro-tec": "PRO-TEC",
        }
        key = display.lower()
        display = aliases.get(key, display)
        key = display.lower()
        self._load_all_brands()
        if key in self._brand_cache:
            return self._brand_cache[key]
        slug_guess = key.replace("ü", "u").replace("ä", "a").replace("ö", "o").replace(" ", "-")
        if slug_guess in self._brand_cache:
            return self._brand_cache[slug_guess]
        resp = self.api.post("products/brands", {"name": display})
        data = resp.json()
        if resp.status_code >= 400:
            existing_id = (data.get("data") or {}).get("resource_id")
            if existing_id:
                bid = int(existing_id)
                self._brand_cache[key] = bid
                self._brand_cache[slug_guess] = bid
                return bid
            LOGGER.warning("Nepodarilo sa vytvoriť značku '%s': %s", display, data)
            return None
        bid = int(data["id"])
        self._brand_cache[key] = bid
        self._brand_cache[(data.get("slug") or slug_guess).lower()] = bid
        LOGGER.info("✨ Nová značka: '%s' (id=%s)", display, bid)
        return bid

    # ---- Kategórie -----------------------------------------------------

    @staticmethod
    def _norm_cat_name(name: str) -> str:
        """WC niekedy vráti '&amp;' — zjednoť s taxonómiou ('&')."""
        return html.unescape((name or "").strip()).lower()

    def _load_all_categories(self) -> None:
        if self._categories_loaded:
            return
        page = 1
        while True:
            resp = self.api.get("products/categories", params={"per_page": 100, "page": page})
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for cat in data:
                key = (self._norm_cat_name(cat["name"]), cat.get("parent", 0))
                self._category_cache[key] = cat["id"]
            page += 1
        self._categories_loaded = True
        LOGGER.info("📂 Načítaných WC kategórií: %d", len(self._category_cache))

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def _create_category(self, name: str, parent_id: int) -> int:
        resp = self.api.post("products/categories", {"name": name, "parent": parent_id})
        data = resp.json()
        if resp.status_code >= 400:
            # WooCommerce vráti term_exists ak medzitým vytvoril iný proces
            existing_id = (data.get("data") or {}).get("resource_id")
            if existing_id:
                LOGGER.info("Kategória '%s' už existuje (id=%s), používam ju.", name, existing_id)
                return int(existing_id)
            raise RuntimeError(f"Nepodarilo sa vytvoriť kategóriu '{name}': {data}")
        LOGGER.info("✨ Vytvorená nová kategória: '%s' (parent=%s, id=%s)", name, parent_id, data["id"])
        return data["id"]

    def ensure_category_path(self, path: List[str]) -> Optional[int]:
        """Zabezpečí existenciu kategórie (max 3 úrovne) vo WooCommerce
        a vráti ID najnižšej (leaf) úrovne. Chýbajúce úrovne automaticky vytvorí."""
        self._load_all_categories()
        clean_path = [p.strip() for p in path if p and p.strip()][:3]
        if not clean_path:
            return None

        parent_id = 0
        leaf_id = None
        for level_name in clean_path:
            key = (self._norm_cat_name(level_name), parent_id)
            leaf_id = self._category_cache.get(key)
            if leaf_id is None:
                leaf_id = self._create_category(level_name, parent_id)
                self._category_cache[key] = leaf_id
                # aj unescaped kľúč (ak WC vráti &amp; neskôr)
                self._category_cache[(self._norm_cat_name(level_name), parent_id)] = leaf_id
            parent_id = leaf_id
        return leaf_id

    def get_existing_category_names(self, limit: int = 300) -> List[str]:
        self._load_all_categories()
        return sorted({name for name, _parent in self._category_cache.keys()})[:limit]

    # ---- Produkty --------------------------------------------------------

    def find_product_id_by_sku(self, sku: str) -> Optional[int]:
        sku = (sku or "").strip()
        if not sku:
            return None
        if sku in self._sku_cache:
            return self._sku_cache[sku]
        try:
            resp = self.api.get("products", params={"sku": sku})
            data = resp.json()
            if isinstance(data, list) and data:
                pid = int(data[0]["id"])
                self._sku_cache[sku] = pid
                return pid
        except Exception as exc:
            LOGGER.warning("SKU lookup zlyhal pre %s: %s", sku, exc)
        return None

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def get_product(self, product_id: int) -> Dict[str, Any]:
        resp = self.api.get(f"products/{product_id}")
        resp.raise_for_status()
        return resp.json()

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def get_products_needing_ai(self, per_page: int = 50, max_pages: int = 20) -> List[Dict[str, Any]]:
        """Vráti produkty, ktoré ešte neboli AI spracované (chýba meta '_ai_processed')."""
        found = []
        for page in range(1, max_pages + 1):
            resp = self.api.get("products", params={"per_page": per_page, "page": page})
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for product in data:
                meta_keys = {m["key"] for m in product.get("meta_data", [])}
                if "_ai_processed" not in meta_keys:
                    found.append(product)
        return found

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def get_audit_batch(self, batch_size: int = 20) -> List[Dict[str, Any]]:
        """Vráti ďalšiu dávku produktov na kontrolu kvality. Postupne prechádza
        celý katalóg stránku po stránke (stav sa ukladá do `optimization_state.json`,
        takže po reštarte pokračuje presne tam, kde skončil) a po dosiahnutí konca
        katalógu sa automaticky vráti na začiatok."""
        state = _load_optimization_state()
        next_page = state.get("last_page", 0) + 1

        resp = self.api.get(
            "products",
            params={"per_page": batch_size, "page": next_page, "orderby": "id", "order": "asc"},
        )
        data = resp.json()

        if not isinstance(data, list) or not data:
            # Koniec katalógu — začíname nový audit cyklus od začiatku.
            next_page = 1
            resp = self.api.get(
                "products",
                params={"per_page": batch_size, "page": next_page, "orderby": "id", "order": "asc"},
            )
            data = resp.json() if isinstance(resp.json(), list) else []

        _save_optimization_state({"last_page": next_page})
        return data

    @retry(times=3, delay=2.0, exceptions=(requests.RequestException,))
    def upsert_product(self, product_id: Optional[int], payload: Dict[str, Any]) -> Dict[str, Any]:
        # SKU upsert — zabráni duplicitám pri re-importe
        if not product_id:
            sku = (payload.get("sku") or "").strip()
            if sku:
                product_id = self.find_product_id_by_sku(sku)
                if product_id:
                    LOGGER.info("🔁 Existujúci SKU %s → update ID %s", sku, product_id)

        if product_id:
            # Pri update NEPRETLAČUJ draft — inak by re-import stiahol publish → koncept.
            payload = dict(payload)
            payload.pop("status", None)
            resp = self.api.put(f"products/{product_id}", payload)
            # Duplicitný/neplatný GTIN nesmie zabiť celý update (cena/sklad/fotka).
            if resp.status_code >= 400 and "global_unique_id" in (resp.text or ""):
                LOGGER.warning(
                    "GTIN global_unique_id odmietnutý pre id=%s — retry bez neho",
                    product_id,
                )
                payload.pop("global_unique_id", None)
                resp = self.api.put(f"products/{product_id}", payload)
        else:
            payload.setdefault("status", "draft")
            resp = self.api.post("products", payload)
            if resp.status_code >= 400 and "global_unique_id" in (resp.text or ""):
                LOGGER.warning("GTIN global_unique_id odmietnutý pri create — retry bez neho")
                payload = dict(payload)
                payload.pop("global_unique_id", None)
                resp = self.api.post("products", payload)
        resp.raise_for_status()
        data = resp.json()
        sku = (payload.get("sku") or data.get("sku") or "").strip()
        if sku and data.get("id"):
            self._sku_cache[sku] = int(data["id"])
        LOGGER.info(
            "📌 WC status id=%s → %s",
            data.get("id"),
            data.get("status") or ("draft" if not product_id else "?"),
        )
        return data

    def is_stock_watermarked_url(self, url: str) -> bool:
        return any(domain in url.lower() for domain in self.STOCK_IMAGE_BLACKLIST)


# ==============================================================================
# 2) EXTERNÝ PRIESKUM (ak sú B2B dáta chudobné)
# ==============================================================================

class ExternalResearcher:
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    def __init__(self, config: Config):
        self.config = config

    def needs_research(self, raw_data: Dict[str, Any]) -> bool:
        desc = (raw_data.get("description") or "") + (raw_data.get("short_description") or "")
        return len(desc.strip()) < self.config.min_description_length

    @staticmethod
    def looks_cryptic(name: str) -> bool:
        """Detekuje názvy, ktoré vyzerajú ako holé číslo dielu/kód (napr. '44-40 44-44',
        'BW1350', '68163849AA') — také produkty AI kategorizácia z názvu ľahko uhádne
        zle, treba ich overiť externe (web) predtým, než sa im priradí kategória."""
        text = (name or "").strip()
        if not text:
            return False
        letters = [ch for ch in text if ch.isalpha()]
        digits = [ch for ch in text if ch.isdigit()]
        if len(digits) >= 4 and len(digits) >= len(letters):
            return True
        if re.search(r"\b\d{2,4}[- ]\d{2,4}\b", text):
            return True
        if re.search(r"\b[A-Z0-9]{6,}\b", text) and len(letters) < len(digits) + 3:
            return True
        return False

    def gather_context(self, raw_data: Dict[str, Any]) -> str:
        if DDGS is None:
            LOGGER.warning("ddgs/duckduckgo_search nie je nainštalovaný, preskakujem externý prieskum.")
            return ""

        name = raw_data.get("name", "")
        ean = raw_data.get("ean", "")
        brand = raw_data.get("brand", "")
        query = " ".join(filter(None, [brand, name, ean, "technický list dávkovanie použitie"]))

        snippets: List[str] = []
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
        except Exception as exc:
            LOGGER.warning("Web vyhľadávanie zlyhalo: %s", exc)
            return ""

        for result in results:
            url = result.get("href") or result.get("url")
            if not url:
                continue
            text = self._extract_page_text(url)
            if text:
                snippets.append(f"Zdroj ({url}):\n{text[:1500]}")
            if len(snippets) >= 3:
                break

        return "\n\n---\n\n".join(snippets)

    def _extract_page_text(self, url: str) -> str:
        try:
            if url.lower().endswith(".pdf"):
                return self._extract_pdf_text(url)
            resp = requests.get(url, headers=self.HEADERS, timeout=self.config.request_timeout, verify=False)
            if resp.status_code != 200:
                return ""
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)
        except Exception:
            return ""

    def _extract_pdf_text(self, url: str) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        try:
            resp = requests.get(url, headers=self.HEADERS, timeout=self.config.request_timeout)
            reader = PdfReader(BytesIO(resp.content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages[:3])
            return text
        except Exception:
            return ""


# ==============================================================================
# 3) HD IMAGE FINDER
# ==============================================================================

def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """PNG s alphou → RGB na bielom pozadí (nie čiernom / garbage)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _is_near_black(rgb: tuple, thr: int = 28) -> bool:
    r, g, b = rgb
    return r < thr and g < thr and b < thr


def _has_black_studio_corners(img: Image.Image) -> bool:
    """True ak ≥3 rohy sú takmer čierne (plný studio-black packshot)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    if w < 8 or h < 8:
        return False
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    return sum(1 for x, y in corners if _is_near_black(rgb.getpixel((x, y))) ) >= 3


def _black_pixel_ratio(img: Image.Image, thr: int = 28) -> float:
    sample = img.convert("RGB").resize((64, 64))
    pixels = list(sample.getdata())
    if not pixels:
        return 0.0
    return sum(1 for p in pixels if _is_near_black(p, thr)) / len(pixels)


def _has_transparency_bg(img: Image.Image) -> bool:
    """PNG s veľkou priesvitnou plochou — WP thumbnails skladajú alpha na ČIERNU
    (zachytené: Autoprofi/Bluechem -300x300.png → čierny obdĺžnik v gride)."""
    if img.mode not in ("RGBA", "LA") and not (img.mode == "P" and "transparency" in getattr(img, "info", {})):
        return False
    rgba = img.convert("RGBA")
    sample = rgba.resize((64, 64))
    pixels = list(sample.getdata())
    if not pixels:
        return False
    transparent = sum(1 for _r, _g, _b, a in pixels if a < 16)
    return transparent / len(pixels) >= 0.12


def _has_black_studio_bg(img: Image.Image) -> bool:
    """Zlé pozadie na shope: studio-black JPEG, alebo PNG s alphou (WP → čierne thumb)."""
    if _has_transparency_bg(img):
        return True
    rgb = _flatten_to_rgb(img)
    if _has_black_studio_corners(rgb):
        return True
    return _black_pixel_ratio(rgb) >= 0.28


def _scrub_black_halo(img: Image.Image, black_thr: int = 70, white_thr: int = 230, dilate: int = 5) -> Image.Image:
    """Zje zubatý čierny okraj po flood-fill (anti-alias studio edge).

    Dilatuje biele pozadie o `dilate` px a pretiahne na near-black pixely.
    Čierny text vo vnútri etikety ostane — biela maska cez telo produktu neprejde.
    """
    from PIL import ImageFilter

    out = img.convert("RGB")
    w, h = out.size
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    px = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if r >= white_thr and g >= white_thr and b >= white_thr:
                mp[x, y] = 255
    for _ in range(max(1, dilate)):
        mask = mask.filter(ImageFilter.MaxFilter(3))
        mp = mask.load()
    for y in range(h):
        for x in range(w):
            if mp[x, y] >= 128 and _is_near_black(px[x, y], black_thr):
                px[x, y] = (255, 255, 255)
    return out


def _whiten_black_studio_bg(img: Image.Image) -> Image.Image:
    """Studio black pozadie → biele (flood-fill + halo scrub).

    1) PNG s alphou → flatten na bielu (hotovo; žiadny flood-fill do etikety)
    2) Plný black packshot → fill z rohov
    3) Biely okraj + čierny plate (Autoprofi PNG) → fill z čierneho seedu
       nájdeného od stredu hrán dovnútra
    4) Zubatý čierny silhouette okraj → scrub dilatáciou bieleho pozadia
       LEN po fill čierneho studio bg — NIE na čistých white packshot
       (inak sa zožerie čierny SKU panel PRO-TEC → glitchy text).
    """
    from PIL import ImageDraw

    alpha_only = _has_transparency_bg(img)
    rgb = _flatten_to_rgb(img)
    # Skutočné studio-black (nie len alpha→biela): rohy alebo vysoký podiel čiernej
    had_black_studio = _has_black_studio_corners(rgb) or _black_pixel_ratio(rgb) >= 0.28
    if alpha_only and not had_black_studio:
        return rgb

    out = rgb.copy()
    w, h = out.size
    did_fill = False
    if had_black_studio:
        seeds = []
        if _has_black_studio_corners(out):
            seeds.extend([(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
        else:
            # hľadaj čierny pixel od okrajov smerom dnu (za bielym marginom)
            for y in (h // 2, h // 3, (2 * h) // 3):
                for x in range(w):
                    if _is_near_black(out.getpixel((x, y))):
                        seeds.append((x, y))
                        break
                for x in range(w - 1, -1, -1):
                    if _is_near_black(out.getpixel((x, y))):
                        seeds.append((x, y))
                        break
            for x in (w // 2, w // 3, (2 * w) // 3):
                for y in range(h):
                    if _is_near_black(out.getpixel((x, y))):
                        seeds.append((x, y))
                        break
        seen = set()
        for seed in seeds:
            if seed in seen:
                continue
            seen.add(seed)
            if _is_near_black(out.getpixel(seed), 40):
                ImageDraw.floodfill(out, seed, (255, 255, 255), thresh=36)
                did_fill = True
    # Halo scrub len po fill — biele packshoty s čiernym SKU panelom nechať
    if did_fill:
        out = _scrub_black_halo(out)
    return out


def _crop_to_square_bytes(raw_bytes: bytes, size: int = 800) -> bytes:
    """Upraví fotku na štvorec `size`x`size` BEZ orezu obsahu.

    Vždy letterbox/pillarbox na biele pozadie (contain). Stredový orez
    u fliaš odrezával vrch/spodok (Metabond CL, MOPAR gear oils) — to je
    pre packshoty neprijateľné. Biele okraje sú OK; orezaná fľaša nie.
    Alpha kanál vždy flatten na bielu (inak JPEG ukáže červené/hnedé bloky).
    Studio-black pozadie sa vybieli; obsah drží ~86 % strany (dýchací priestor).
    """
    img = _whiten_black_studio_bg(Image.open(BytesIO(raw_bytes)))
    width, height = img.size
    side = max(width, height)
    canvas_side = max(int(round(side / 0.86)), side + 2)
    canvas = Image.new("RGB", (canvas_side, canvas_side), (255, 255, 255))
    canvas.paste(img, ((canvas_side - width) // 2, (canvas_side - height) // 2))
    img = canvas.resize((size, size), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _upload_image_via_bridge(image_bytes: bytes, filename: str, config: "Config") -> Optional[str]:
    """Bridge upload s backoffom pri 429/5xx (preťažený web), potom pôvodný fallback."""
    for wait in (0, 10, 30, 60):
        if wait:
            LOGGER.info("Upload fotky: web preťažený, čakám %ss…", wait)
            time.sleep(wait)
        try:
            url = config.wc_url.rstrip("/") + "/wp-json/smartgaraz/v1/upload-image"
            resp = requests.post(url, params={"filename": filename},
                                 headers={"X-SG-Consumer-Key": config.wc_consumer_key,
                                          "X-SG-Consumer-Secret": config.wc_consumer_secret,
                                          "Content-Type": "image/jpeg"},
                                 data=image_bytes, timeout=config.request_timeout, verify=False)
            if resp.status_code in (429, 502, 503, 504):
                continue
            resp.raise_for_status()
            return resp.json().get("url")
        except Exception:
            break
    return _upload_image_via_bridge_once(image_bytes, filename, config)


def _upload_image_via_bridge_once(image_bytes: bytes, filename: str, config: "Config") -> Optional[str]:
    """Nahrá spracovanú fotku do WP Media cez vlastný `smartgaraz-bridge`
    REST endpoint (`/wp-json/smartgaraz/v1/upload-image`), ktorý sa
    autentifikuje TÝM ISTÝM WooCommerce consumer key/secret, ktorý bot aj tak
    už potrebuje na všetko ostatné — takže žiadny nový secret, žiadne ručné
    generovanie WP Application Password. Vracia None ak upload zlyhá
    (napr. bridge plugin ešte nie je nasadený) — volajúci potom padne na
    pôvodnú (nespracovanú) URL fotky ako bezpečný fallback."""
    if not config.wc_url or not config.wc_consumer_key or not config.wc_consumer_secret:
        return None
    try:
        url = config.wc_url.rstrip("/") + "/wp-json/smartgaraz/v1/upload-image"
        resp = requests.post(
            url,
            params={"filename": filename},
            headers={
                "X-SG-Consumer-Key": config.wc_consumer_key,
                "X-SG-Consumer-Secret": config.wc_consumer_secret,
                "Content-Type": "image/jpeg",
            },
            data=image_bytes,
            timeout=config.request_timeout,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("url")
    except Exception as exc:
        LOGGER.warning("Upload fotky cez smartgaraz-bridge zlyhal (%s), skúšam WP Application Password fallback.", exc)
        return _upload_image_via_app_password(image_bytes, filename, config)


def _upload_image_via_app_password(image_bytes: bytes, filename: str, config: "Config") -> Optional[str]:
    """Legacy fallback cez `/wp-json/wp/v2/media` (WP Application Password) —
    použije sa LEN ak bridge endpoint zlyhá a WP_USER/WP_APP_PASSWORD sú
    nastavené. Pre bežnú prevádzku sa nevyžaduje vôbec."""
    if not config.wp_user or not config.wp_app_password:
        return None
    try:
        url = config.wc_url.rstrip("/") + "/wp-json/wp/v2/media"
        resp = requests.post(
            url,
            auth=(config.wp_user, config.wp_app_password),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "image/jpeg",
            },
            data=image_bytes,
            timeout=config.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("source_url")
    except Exception as exc:
        LOGGER.warning("Upload fotky do WP Media (Application Password) zlyhal: %s", exc)
        return None


class HDImageFinder:
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    PLACEHOLDER_HINTS = (
        "placeholder", "no-image", "noimage", "coming-soon", "woocommerce-placeholder",
        "default-image", "dummy", "red-black", "redblack",
        # Vlastný automaticky generovaný placeholder (červený pruh hore +
        # tmavý pruh dole s názvom produktu), viď
        # web/wp-content/mu-plugins/smartgaraz-stabilizer.php::create_placeholder()
        "smartgaraz-placeholders", "smartgaraz-repair",
    )
    # Generické stock-photo súbory (Freepik a pod.) — názov popisuje VŠEOBECNÚ
    # kategóriu ("household cleaning product", "blank white background"), nie
    # konkrétny produkt. Zachytené naživo: 3 rôzne MOPAR oleje omylom dostali
    # tú istú "household-cleaning-product-isolated-white-background" fotku.
    GENERIC_STOCK_HINTS = (
        "isolated-white-background", "household-cleaning-product", "blank-white-background",
        "blank-background", "empty-white-background", "white-background-isolated",
        # 3D/beauty stock omylom pri hydraulickom oleji (Vecteezy „lipstick container“)
        "lipstick", "beauty-product", "beauty-products", "3d-render", "3d-rendering",
        "vecteezy", "freepik", "stock-photo", "mockup-bottle", "cosmetic-bottle",
        # Wholesale food omylom pri oleji (AMALIE → Hilldale cheese)
        "hilldale", "cheese", "american-cheese",
        # Meme / social fotky omylom pri kabínových filtroch (Hello Kitty selfie)
        "hello-kitty", "hellokitty", "snapchat", "meme", "cat-filter",
    )
    # Rodiny produktov v názve súboru — ak je vo fotke iná rodina než v produkte,
    # ide o zlú fotku (zachytené: Autoprofi ADHESIVE GREASE 43201 mal
    # Autoprofi-Engine-Clean.png).
    PRODUCT_FAMILY_TOKENS = (
        "engine-clean", "adhesive-grease", "oil-booster", "nano-engine",
        "engine-flush", "radiator-flush", "dpf-super", "fuel-line",
        "brake-cleaner", "octane-booster", "cetane-booster", "gearbox-stop",
        "stop-leak", "oxicat", "guard-fill",
    )

    def __init__(self, config: Config, wc_client: WooCommerceClient):
        self.config = config
        self.wc_client = wc_client
        self._black_studio_cache: Dict[str, bool] = {}
        self._http = requests.Session()
        self._http.headers.update(self.HEADERS)

    def needs_image(self, raw_data: Dict[str, Any]) -> bool:
        images = raw_data.get("existing_images") or []
        if not images:
            return True
        # Placeholder ALEBO generický stock (household-cleaning-product…) —
        # zachytené: MOPAR DOT 3 mal Freepik „cleaning bottles + gloves“.
        if any(
            self._looks_like_placeholder(url) or self._looks_like_generic_stock(url)
            for url in images
        ):
            return True
        if any(self._image_mismatches_product(url, raw_data) for url in images):
            return True
        # Studio-black packshot na shope = „čierna diera" — treba prepísať
        # (whitening v prepare_image, alebo nový white-bg kandidát).
        return any(self._looks_like_black_studio(url) for url in images)

    def prepare_image(self, url: str, name_hint: str = "product") -> Optional[str]:
        """Stiahne kandidátsku fotku, orežе ju na stred do štvorca a zmenší na
        `product_image_size` (default 800x800), presne podľa vzoru, ktorý si
        zákazník ručne pripravil pre Metabond fotky, a nahrá ju do WP Media
        Library cez smartgaraz-bridge (autentifikácia existujúcimi WC
        kľúčmi — žiadne ďalšie nastavovanie netreba). Ak orez/upload z
        akéhokoľvek dôvodu zlyhá, vráti pôvodnú (nespracovanú) URL ako
        bezpečný fallback — obrázok sa vždy priradí, nanajvýš neorezaný."""
        if not url:
            return None
        if Image is None:
            LOGGER.warning("Pillow nie je nainštalovaný, preskakujem orez fotky na štvorec.")
            return url
        try:
            resp = self._http.get(url, headers=self.HEADERS, timeout=self.config.request_timeout, verify=False)
            resp.raise_for_status()
            processed = _crop_to_square_bytes(resp.content, self.config.product_image_size)
        except Exception as exc:
            LOGGER.warning("Spracovanie fotky zlyhalo (%s), používam pôvodnú URL.", exc)
            return url

        safe_name = re.sub(r"[^a-z0-9]+", "-", name_hint.lower()).strip("-")[:60] or "product"
        uploaded_url = _upload_image_via_bridge(processed, f"{safe_name}.jpg", self.config)
        return uploaded_url or url

    def find_best_image(self, raw_data: Dict[str, Any]) -> Optional[str]:
        if DDGS is None:
            LOGGER.warning("ddgs nie je nainštalovaný, preskakujem hľadanie obrázka.")
            return None

        expected_brand = detect_true_brand(
            raw_data.get("name", ""), raw_data.get("description", ""), fallback=(raw_data.get("brand") or "").strip()
        )

        for query in self._build_search_queries(raw_data):
            LOGGER.info("🔍 Hľadám HD fotku: %s", query)
            try:
                with DDGS() as ddgs:
                    candidates = list(ddgs.images(
                        query,
                        size="Large",
                        type_image="photo",
                        color="White",
                        max_results=12,
                    ))
            except Exception as exc:
                LOGGER.warning("Vyhľadávanie obrázkov zlyhalo pre '%s': %s", query, exc)
                continue

            for candidate in candidates:
                url = candidate.get("image")
                try:
                    width = int(candidate.get("width") or 0)
                except (TypeError, ValueError):
                    width = 0
                try:
                    height = int(candidate.get("height") or 0)
                except (TypeError, ValueError):
                    height = 0
                title = (candidate.get("title") or "").lower()
                lowered_url = (url or "").lower()

                if not url or self.wc_client.is_stock_watermarked_url(url):
                    continue
                if looks_like_supplier_watermark(url, title):
                    continue
                if self._looks_like_placeholder(url) or "watermark" in title or "vodoznak" in title:
                    continue
                if self._looks_like_generic_stock(url):
                    continue
                # OEM parts-catalog diagramy / ilustrácie (RevolutionParts…) —
                # nie sú packshot fľaše (zachytené pri MOPAR DOT 5.1).
                if any(
                    h in lowered_url
                    for h in (
                        "cdn-illustrations", "/diagram/", "exploded", "illustration",
                    )
                ) or any(h in title for h in ("exploded view", "parts diagram", "illustration")):
                    continue
                # Blog/tutoriál/„ako fotiť produkty“ — NIKDY nie je packshot
                # (zachytené: Feniks Lava Wheel dostal "How to Achieve a Pure
                # White Background in Product Photography").
                if any(
                    h in title
                    for h in (
                        "how to", "tutorial", "photography tip", "product photography",
                        "white background in", "blogging", "tips for", "guide to",
                    )
                ):
                    continue
                # Lak / spray paint / touch-up pen ≠ olej/kvapalina
                # (zachytené: NV246 → paint pen; CVTF+4 5 l → lacquer spray 5163213AB).
                name_blob = (raw_data.get("name") or "").lower()
                is_fluid = any(
                    k in name_blob
                    for k in ("olej", "oil", "fluid", "kvapalin", "lubricant", "atf", "nv246", "cvtf", "cvt ")
                )
                if is_fluid:
                    if any(
                        h in title or h in lowered_url
                        for h in (
                            "touch-up", "touch up", "paint pen", "paintpen",
                            "stylo de retouche", "lakova", "lakovac", "touchup",
                            "lacquer spray", "spray paint", "peinture", "aerosol paint",
                            "lakier", "spray can",
                        )
                    ):
                        continue
                    # Filter / kabínový filter / diagram dielu ≠ fľaša oleja
                    # (zachytené: MOPAR 75W90 AWD PTU → cabin air filter;
                    #  68280933AA → OEM exploded-view diagram;
                    #  MOPAR 10W30 68524000AA → biely spin-on oil filter).
                    if any(
                        h in title or h in lowered_url
                        for h in (
                            "air filter", "cabin filter", "cabin air", "oil filter",
                            "oilfilter", "spin-on", "spin on filter", "filtre a huile",
                            "filtre a air", "filtre d'habitacle", "vzduchový filter",
                            "kabínový filter", "kabinovy filter", "olejový filter",
                            "olejovy filter", "filtr olejowy",
                            "part can be found", "reference #", "exploded-view",
                            "parts catalog", "illustration only",
                        )
                    ):
                        continue
                    # Potraviny / nesúvisiaci wholesale stock (zachytené:
                    # AMALIE 5W40 → Hilldale American cheese case).
                    if any(
                        h in title or h in lowered_url
                        for h in (
                            "cheese", "hilldale", "food service", "grocery",
                            "pasteurized", "rebanada", "keep refrigerated",
                        )
                    ):
                        continue
                # Prevodový/diferenciálny olej (75W/70W/GL-4/GL-5) ≠ motorový
                # 0W-20 / MaxPro engine oil (zachytené: 68218654AB 75W90 dostal
                # fotku MOPAR MaxPro SAE 0W-20).
                is_gear_oil = bool(
                    re.search(r"\b(?:7[05]w|8[05]w)[-\s]?\d{2,3}\b", name_blob)
                    or any(k in name_blob for k in (
                        "prevodov", "diferenciál", "diferencial", "gear oil",
                        "axle lubricant", "transfer case", "gl-4", "gl-5", "gl4", "gl5",
                        "ptu", "awd",
                    ))
                )
                if is_gear_oil:
                    if any(
                        h in title or h in lowered_url
                        for h in (
                            "0w-20", "0w20", "5w-20", "5w20", "5w-30", "5w30",
                            "5w-40", "10w-40", "0w-30", "0w30",
                            "maxpro", "maxpro+", "engine oil", "motorový olej",
                            "motorovy olej", "motor oil", "huile moteur",
                            "aceite de motor", "helix", "edge 0w",
                            "ilsac", "gf-6", "gf-5", "api-sp", "5 liter", "5 litre",
                        )
                    ):
                        continue
                # Logo galérie/firemné loga NIKDY nie sú fotka produktu —
                # ochrana proti "vtipným" zhodám na všeobecnejšie slovo v názve.
                if "logo" in lowered_url or "logo" in title:
                    continue
                # Konkurenčná značka v titulku/URL = takmer vždy nesprávny
                # produkt (napr. hľadáme "STARLINE 5W-30", obrázkové
                # vyhľadávanie ale vráti populárnejšiu vizuálne podobnú
                # "Castrol EDGE" fľašu) — zachytené naživo, preto tvrdo
                # zamietnuť namiesto len logovania.
                if expected_brand and conflicts_with_other_brand(f"{title} {lowered_url}", expected_brand):
                    continue
                if width and height and (width < self.config.min_image_size_px or height < self.config.min_image_size_px):
                    continue
                if width and height:
                    long_side, short_side = max(width, height), max(min(width, height), 1)
                    if long_side / short_side > 2.2:
                        continue  # príliš pretiahnutý tvar na produktovú fotku (banner/logo)
                    # Fľaša/balenie chémie je takmer VŽDY na výšku alebo
                    # štvorcová, nikdy výrazne na šírku — na šírku bývajú
                    # propagačné ilustrácie/autá (zachytené naživo: MOPAR
                    # promo obrázok Dodge Challenger namiesto fľaše oleja).
                    if width > height * 1.25:
                        continue
                # Generické stock-photo názvy súborov (napr. z Freepik) NIKDY
                # nie sú skutočná fotka KONKRÉTNEHO produktu, aj keď majú
                # biele pozadie a správnu veľkosť (zachytené naživo: rovnaká
                # "household-cleaning-product-isolated..." fotka sa použila
                # pre 3 rôzne motorové oleje).
                if any(h in lowered_url for h in self.GENERIC_STOCK_HINTS):
                    continue
                if self._verify_image(url):
                    return url
        return None

    def _build_search_queries(self, raw_data: Dict[str, Any]) -> List[str]:
        sku = (raw_data.get("sku") or "").strip()
        ean = (raw_data.get("ean") or "").strip()
        name = (raw_data.get("name") or "").strip()
        description = (raw_data.get("description") or "").strip()
        # Priorita: skutočný VÝROBCA rozpoznaný z názvu (napr. "Mopar" v
        # "Mopar MaxPro SAE 0W20") pred distribútorskou značkou z feedu
        # (napr. "PRO-TEC") — tá by hľadanie fotky vždy odklonila na
        # nesúvisiace produkty distribútora.
        brand = detect_true_brand(name, description, fallback=(raw_data.get("brand") or "").strip())
        # Krátke/generické značky (napr. LKQ vlastná značka "XT") dokážu bez
        # kontextu vrátiť úplne nesúvisiaci produkt (napr. "XT M3 Fanless" PC
        # skrinka namiesto motorového oleja) — ak názov vyzerá ako olej,
        # pridaj disambiguačné slovo aj do SKU-based dopytov, nielen do
        # name-based (tie SKU dopyty idú v poradí PRVÉ).
        name_l = name.lower()
        # Dôležité: prevodové/diferenciálne oleje (75W…) NEMÔŽU dostať hint
        # "motorový olej engine oil" — to DDG odkloní na MaxPro 0W-20 a pod.
        oil_hint = ""
        is_gear = bool(
            re.search(r"\b(?:7[05]w|8[05]w)[-\s]?\d{2,3}\b", name_l)
            or any(k in name_l for k in (
                "prevodov", "diferenciál", "diferencial", "gear oil",
                "axle", "transfer", "gl-4", "gl-5", "ptu",
            ))
        )
        if is_gear:
            oil_hint = "gear oil axle lubricant bottle "
        elif re.search(r"\b\d{1,2}w[-\s]?\d{2,3}\b", name_l) or "olej" in name_l or "oil" in name_l:
            oil_hint = "motorový olej engine oil "
        queries = []
        if sku:
            queries.append(f"{brand} {oil_hint}{sku} product white background".strip())
            queries.append(f"{oil_hint}{sku} white background".strip())
            if is_gear and sku:
                # SKU-first bez oil_hint často vráti OEM diagram — pridaj
                # explicitný "bottle packshot" dopyt pred name-based.
                queries.append(f"{brand} {sku} bottle packshot white background".strip())
        if ean:
            queries.append(f"{ean} product white background")
        if name:
            queries.append(f"{brand} {name} produkt biele pozadie".strip())
            queries.append(f"{name} white background packshot".strip())
            if brand and brand.lower() not in name_l:
                queries.append(f"{brand} {name} white background packshot")
        # unikátne, zachovať poradie
        seen = set()
        unique = []
        for q in queries:
            key = q.lower()
            if q and key not in seen:
                seen.add(key)
                unique.append(q)
        return unique

    def _looks_like_placeholder(self, url: str) -> bool:
        lowered = (url or "").lower()
        return any(hint in lowered for hint in self.PLACEHOLDER_HINTS)

    def _looks_like_generic_stock(self, url: str) -> bool:
        lowered = (url or "").lower()
        return any(hint in lowered for hint in self.GENERIC_STOCK_HINTS)

    def _image_mismatches_product(self, url: str, raw_data: Dict[str, Any]) -> bool:
        """True ak názov súboru fotky ukazje inú produktovú rodinu než názov/SKU.

        Príklad: `Autoprofi-Engine-Clean.png` pri „ADHESIVE GREASE SPRAY 43201“.
        """
        if not url:
            return False
        basename = (url or "").lower().rsplit("/", 1)[-1].split("?", 1)[0]
        basename_compact = re.sub(r"[^a-z0-9]+", "", basename)
        product_blob = " ".join(
            str(raw_data.get(k) or "") for k in ("name", "sku", "brand")
        ).lower()
        product_compact = re.sub(r"[^a-z0-9]+", "", product_blob)

        for token in self.PRODUCT_FAMILY_TOKENS:
            tok_compact = token.replace("-", "")
            if tok_compact not in basename_compact:
                continue
            in_product = tok_compact in product_compact or token.replace("-", " ") in product_blob
            if not in_product:
                return True
        return False

    def _looks_like_black_studio(self, url: str) -> bool:
        """Stiahne náhľad a zistí studio-black pozadie (rohy alebo veľká čierna plocha)."""
        if not url or Image is None:
            return False
        if url in self._black_studio_cache:
            return self._black_studio_cache[url]
        try:
            resp = self._http.get(url, timeout=12, verify=False)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content))
            result = _has_black_studio_bg(img)
        except Exception:
            result = False
        self._black_studio_cache[url] = result
        return result

    def _verify_image(self, url: str) -> bool:
        if Image is None:
            return True  # bez Pillow dôverujeme metadátam z vyhľadávača
        try:
            resp = requests.get(url, timeout=10, stream=True)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            width, height = img.size
            if width < self.config.min_image_size_px or height < self.config.min_image_size_px:
                return False
            # Produktová fotka (fľaša/balenie na bielom pozadí) nikdy nie je
            # extrémne pretiahnutá — loga/bannery často sú (ochrana proti
            # napr. logos-world.net firemným logám omylom nájdeným hľadaním).
            long_side, short_side = max(width, height), max(min(width, height), 1)
            if long_side / short_side > 2.2:
                return False
            # Fľaša/balenie je vždy na výšku alebo štvorcová — na šírku bývajú
            # propagačné ilustrácie/autá (zachytené naživo: MOPAR promo obrázok
            # auta namiesto fľaše oleja prešiel len preto, že nebol "extrémne"
            # pretiahnutý podľa kontroly vyššie).
            if width > height * 1.25:
                return False
            # Detekcia červeno-čierneho placeholdera (dominujúce červené + čierne pixely).
            sample = img.resize((32, 32))
            pixels = list(sample.getdata())

            # Takmer prázdna/čisto biela fotka (napr. stock "blank white
            # background" súbor) — real produktová fotka má vždy nejaký
            # viditeľný obsah (fľašu, štítok...), nie len biely šum.
            near_white = sum(1 for r, g, b in pixels if r > 245 and g > 245 and b > 245)
            if near_white / (len(pixels) or 1) > 0.97:
                return False

            def _is_black(px):
                r, g, b = px
                return r < 35 and g < 35 and b < 35

            def _is_red(px):
                r, g, b = px
                return r > 150 and g < 90 and b < 90

            redish = sum(1 for p in pixels if _is_red(p))
            blackish = sum(1 for p in pixels if _is_black(p))
            total = len(pixels) or 1
            if (redish + blackish) / total > 0.55 and redish > 0 and blackish > 0:
                return False

            # Presnejšia detekcia BANNEROVÉHO placeholdera (napr. náš vlastný
            # smartgaraz-stabilizer.php generátor: plný červený pruh hore ~15%
            # výšky + plný tmavý pruh dole ~15% výšky) — chytí to aj vtedy, keď
            # stred fotky je prevažne biely/sivý a celkový pomer vyššie
            # nedosiahne 55 %.
            row_px = 32
            top_band = pixels[: row_px * 5]  # ~top 15 % (5 z 32 riadkov)
            bottom_band = pixels[row_px * 27 :]  # ~bottom 15 %
            if top_band and bottom_band:
                top_red_ratio = sum(1 for p in top_band if _is_red(p)) / len(top_band)
                bottom_black_ratio = sum(1 for p in bottom_band if _is_black(p)) / len(bottom_band)
                if top_red_ratio > 0.8 and bottom_black_ratio > 0.8:
                    return False

            # Hľadáme explicitne "white background packshot" — skutočná
            # produktová fotka má takmer vždy svetlé/biele rohy (pozadie).
            # Ak sú všetky 4 rohy tmavé/sýto farebné, s veľkou
            # pravdepodobnosťou ide o logo/banner/nesúvisiaci obrázok, ktorý
            # vyhľadávanie omylom vrátilo (napr. firemné logo namiesto fľaše).
            def _is_light(px):
                r, g, b = px
                return r > 195 and g > 195 and b > 195

            corner = 4  # 4x4 blok v rohu z 32x32 mriežky
            corners = []
            for row0 in (0, row_px - corner):
                for col0 in (0, row_px - corner):
                    for r in range(row0, row0 + corner):
                        corners.extend(pixels[r * row_px + col0: r * row_px + col0 + corner])
            if corners:
                light_ratio = sum(1 for p in corners if _is_light(p)) / len(corners)
                if light_ratio < 0.35:
                    return False
            return True
        except Exception:
            return False


# ==============================================================================
# CLAUDE AI CONTENT ENGINE (kategorizácia + copywriting + SEO)
# ==============================================================================

from taxonomy import (  # jediný zdroj pravdy pre kategórie
    CATEGORY_TREE,
    format_category_tree,
    sanitize_category_path,
    match_keyword_category,
    all_allowed_names,
)


class ClaudeContentEngine:
    # Ak zvolený/predvolený model vráti 404 (napr. bol medzičasom vyradený Anthropicom), engine sa
    # naň natrvalo prepne, aby sa spracovanie produktov nezaseklo a pokračovalo okamžite ďalej.
    # Aktuálny zoznam podporovaných modelov: https://platform.claude.com/docs/en/about-claude/model-deprecations
    FALLBACK_MODEL = "claude-sonnet-4-6"

    def __init__(self, config: Config):
        if anthropic is None:
            raise RuntimeError("Knižnica 'anthropic' nie je nainštalovaná (pip install anthropic).")
        self.config = config
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.active_model = config.anthropic_model

    @staticmethod
    def _is_model_not_found(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 404:
            return True
        not_found_type = getattr(anthropic, "NotFoundError", None) if anthropic else None
        if not_found_type and isinstance(exc, not_found_type):
            return True
        text = str(exc).lower()
        return "404" in text and ("model" in text or "not_found" in text)

    def _call(self, system: str, user: str, max_tokens: int = 2000) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                message = self.client.messages.create(
                    model=self.active_model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(block.text for block in message.content if hasattr(block, "text"))
            except Exception as exc:  # noqa: PERF203
                last_exc = exc
                if self._is_model_not_found(exc) and self.active_model != self.FALLBACK_MODEL:
                    LOGGER.warning(
                        "Model '%s' vrátil HTTP 404 (nedostupný): %s. Prepínam natrvalo na "
                        "fallback model '%s' a okamžite pokračujem v spracovaní.",
                        self.active_model, exc, self.FALLBACK_MODEL,
                    )
                    self.active_model = self.FALLBACK_MODEL
                    continue  # okamžitý pokus s fallback modelom, bez čakania
                LOGGER.warning("Pokus %d/3 zlyhal pri volaní Claude API (model=%s): %s", attempt, self.active_model, exc)
                if attempt < 3:
                    time.sleep(3.0 * attempt)
        raise last_exc

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError(f"Claude nevrátil validný JSON: {text[:300]}")
        return json.loads(match.group(0))

    # ---- Bod 1: Prísna kategorizácia (iba povolený strom) ---------------

    def categorize(
        self,
        raw_data: Dict[str, Any],
        existing_categories: List[str] = None,
        research_context: str = "",
    ) -> List[str]:
        # 1) Deterministické keyword pravidlá (ochrana pred "tesnenie → Žiarovky")
        keyword_path = match_keyword_category(raw_data)
        if keyword_path:
            return sanitize_category_path(keyword_path)

        # 2) AI len ak keyword nestačí — stále len z pevného stromu
        system = (
            "Si expert na taxonómiu autodielenského e-shopu SmartGaráž optimalizovanú na zber zákazníkov.\n"
            "Cieľ: zákazník/mechanik musí nájsť produkt na MAXIMÁLNE 2 KLIKNUTIA.\n"
            "PRÍSNE PRAVIDLÁ:\n"
            "1) AI NESMIE vytvárať nové, náhodné ani malými písmenami písané kategórie.\n"
            "2) Každý produkt MUSÍ byť zaradený EXKLUZÍVNE do jedného z nižšie uvedených "
            "hlavných stromov (presne tieto názvy, vrátane veľkých písmen a znakov & /).\n"
            "3) Podkategória musí byť jedna z povolených pod danou hlavnou kategóriou.\n"
            "4) Nikdy nedávaj motorové diely/tesnenia/chladiče do elektro/žiaroviek.\n"
            "5) Metabond SPIRIT / CL / ochrana = Autochémia & Aditíva > Ochrana motora; "
            "Metabond Petrol Detox / Megasel / Megalene = Aditíva do nafty & benzínu; "
            "DPFCC = Čističe DPF / EGR.\n\n"
            f"POVOLENÁ TAXONÓMIA:\n{format_category_tree()}"
        )
        user = json.dumps({
            "name": raw_data.get("name"),
            "sku": raw_data.get("sku"),
            "brand": raw_data.get("brand"),
            "description": raw_data.get("description"),
            "short_description": raw_data.get("short_description"),
            "attributes": raw_data.get("attributes"),
        }, ensure_ascii=False)
        if research_context:
            user += (
                "\n\nEXTERNÝ PRIESKUM (webové zdroje k tomuto presnému názvu/číslu dielu — over si, "
                "čo produkt SKUTOČNE je, kódy/čísla dielov môžu byť zavádzajúce, napr. '44-40'/'44-44' "
                "sú kódy prevodovky/transfer case BorgWarner, NIE chladiaca kvapalina):\n" + research_context[:3000]
            )
        user += (
            "\n\nOdpovedz IBA JSON-om v tvare: "
            '{"category": "Presný názov hlavnej kategórie zo stromu", '
            '"subcategory": "Presný názov podkategórie zo stromu alebo null"}'
        )
        raw = self._call(system, user, max_tokens=300)
        data = self._extract_json(raw)
        path = [data.get("category"), data.get("subcategory")]
        return sanitize_category_path(path)

    # ---- Bod 2 + 4: Copywriting a SEO — PRÍSNA ŠABLÓNA INZERÁTU ----------

    def generate_content(
        self,
        raw_data: Dict[str, Any],
        research_context: str,
        category_path: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        vehicle_compat = uses_vehicle_compatibility_copy(category_path, raw_data)
        if vehicle_compat:
            section6 = (
                '6) <h3>Kompatibilita & Aplikácia</h3> — jasný návod krok za krokom (<ol><li>) pre '
                'dielňu aj bežného zákazníka (OE / SAE / schválenie vozidla, ak je relevantné).'
            )
            table_oe = "Súvisiace / krížové OE čísla, Objem/Hmotnosť, "
        else:
            section6 = (
                '6) <h3>Použitie & Aplikácia</h3> — návod na použitie nástroja / prípravku '
                '(<ol><li>). NIKDY nespomínaj OE čísla, SAE viskozitu ani fitment vozidla — '
                'toto NIE JE náhradný diel ani olej.'
            )
            table_oe = "Objem/Hmotnosť / balenie, "
        system = (
            "Si špičkový copywriter a SEO špecialista pre luxusný e-shop s autodielmi a autochémiou "
            "SmartGaráž. Pre KAŽDÝ produkt musíš vygenerovať inzerát PRÍSNE podľa pevnej šablóny "
            "e-shopu popísanej v inštrukciách nižšie — dodrž presne všetky sekcie, ich poradie aj "
            "JSON štruktúru. Píšeš presvedčivo, dôveryhodne a technicky presne, v slovenčine.\n\n"
            "SLOVENSKÉ NÁZVY PRODUKTOV (povinné):\n"
            "- Prelož a vyčisti názov produktu z angličtiny do slovenčiny.\n"
            "- Formát: '[ZNAČKA] [Slovenský popis] [technická špecifikácia] ([objem/veľkosť])'\n"
            "- Príklady:\n"
            "  * 'DOT 5.1 BRAKE FLUID 355ML' -> 'MOPAR Brzdová kvapalina DOT 5.1 (355 ml)'\n"
            "  * 'LOWER INTAKE MANIFOLD GASKET V6' -> 'MOPAR Spodné tesnenie nasávania V6'\n"
            "- Značku (DFU, MOPAR, PRO-TEC, KENT, Würth, Metabond...) ponechaj veľkými písmenami na začiatku.\n"
            "- Objem vždy v tvare '(355 ml)', '(5 l)', '(1 ks)' — nie '355ML'.\n\n"
            "ZNAČKOVÁ EXKLUZIVITA (povinné pravidlo pri písaní 'full_description'):\n"
            "- Ak je značka produktu DFU (vlastná private label), výslovne zdôrazni, že DFU "
            "je naša vlastná exkluzívna značka a produkt je dostupný len na SmartGaráž. "
            "Slogan 'Don't F*ck Up' použi len vtedy, ak je už v názve/zdroji — nevymýšľaj "
            "marketing, ktorý ešte nie je oficiálne spustený.\n"
            "- Ak je značka produktu KENT, výslovne zdôrazni, že produkt je exkluzívne dostupný "
            "na našom e-shope SmartGaráž.\n"
            "- Ak je značka PRO-TEC, Metabond alebo Würth, zdôrazni ich špecifiká: nemecká kvalita "
            "'Made in Germany' a nano-technológia (ak je pre daný produkt relevantná).\n\n"
            "Ak sú vstupné dáta chudobné, využi priložený externý technický kontext (ak je k dispozícii) "
            "na doplnenie presného dávkovania, schválení výrobcu a výhod — nikdy si nič nevymýšľaj, "
            "ak informáciu naozaj nevieš, radšej ju vynechaj."
        )
        user_payload = {
            "produkt": {
                "name": raw_data.get("name"),
                "sku": raw_data.get("sku"),
                "ean": raw_data.get("ean"),
                "brand": raw_data.get("brand"),
                "description": raw_data.get("description"),
                "short_description": raw_data.get("short_description"),
                "attributes": raw_data.get("attributes"),
                "regular_price": raw_data.get("regular_price"),
                "category_path": category_path or [],
                "vehicle_compatibility_section": vehicle_compat,
            },
            "externy_technicky_kontext": research_context or "(nie je k dispozícii)",
        }
        instructions = (
            "\n\nVygeneruj JSON PRESNE s touto štruktúrou (žiadne iné kľúče, žiadny text mimo JSON):\n"
            "{\n"
            '  "product_name_sk": "Vyčistený a preložený slovenský názov produktu podľa pravidiel vyššie",\n'
            '  "short_description": "Krátky popis nad tlačidlo Pridať do košíka — stručný, úderný '
            'odsek (cca 3-4 vety), ktorý MUSÍ obsahovať: hlavný účel, pre koho je produkt určený, '
            'kľúčový prínos, objem/veľkosť a výdatnosť.",\n'
            '  "full_description": "Kompletný HTML predajný text (použi <h2>, <h3>, <p>, <ul><li>, '
            '<ol><li>, <table>) presne v tomto poradí sekcií: '
            '1) <h2> titulok s hlavným úžitkom, '
            '2) uvedenie do problému zákazníka a jeho riešenie týmto produktom (rešpektuj pravidlo '
            'značkovej exkluzivity vyššie), '
            '3) <h3>Hlavné vlastnosti a benefity</h3> — odrážky <ul><li> s <strong>bold</strong> '
            'kľúčovými slovami, '
            '4) <h3>Prečo práve tento produkt?</h3> — presne 3 kľúčové dôvody, '
            '5) <h3>Identifikačné a technické údaje</h3> — prehľadná <table> s riadkami: Výrobca, '
            f'EAN kód, Kód produktu / SKU, {table_oe}'
            f'{section6}",\n'
            '  "seo_rank_math": {\n'
            '    "focus_keyword": "Názov produktu + Značka + Objem/Rozmer skombinované do jedného '
            'prirodzeného kľúčového slovného spojenia",\n'
            '    "alt_text": "Názov + Značka + Objem + hlavná funkcia (ALT atribút hlavného obrázka)",\n'
            '    "meta_title": "Názov produktu | SMARTgaráž",\n'
            '    "meta_description": "SEO meta popis presne do 155 znakov",\n'
            '    "image_description": "Krátky popis obrázka pre galériu",\n'
            '    "tags": ["6 až 8 relevantných tagov - značka, kód, funkcia, použitie"]\n'
            "  },\n"
            '  "search_terms": ["presne 8 long-tail hľadaných výrazov / kľúčových slov, ktoré by '
            'do vyhľadávača zadal mechanik alebo zákazník"]\n'
            "}\n"
            "Dodrž presný počet prvkov: search_terms musí mať PRESNE 8 položiek, tags 6 až 8 položiek. "
            "Odpovedz IBA týmto JSON objektom, bez ďalšieho textu."
        )
        user = json.dumps(user_payload, ensure_ascii=False) + instructions
        raw = self._call(system, user, max_tokens=4000)
        data = self._extract_json(raw)

        seo = data.setdefault("seo_rank_math", {})
        # Meta titulok je pevná šablóna — vynútime ho deterministicky, nespoliehame sa na AI.
        product_name_sk = normalize_sk_product_name(
            (data.get("product_name_sk") or raw_data.get("name") or "").strip()
        )
        data["product_name_sk"] = product_name_sk
        if data.get("short_description"):
            data["short_description"] = normalize_sk_product_name(str(data["short_description"]))
        if data.get("full_description"):
            data["full_description"] = normalize_sk_product_name(str(data["full_description"]))
        seo["meta_title"] = build_meta_title(product_name_sk)
        seo["meta_description"] = truncate(
            normalize_sk_product_name(seo.get("meta_description", "")),
            self.config.meta_description_limit,
        )
        if isinstance(seo.get("tags"), str):
            seo["tags"] = [t.strip() for t in seo["tags"].split(",") if t.strip()]
        seo["tags"] = (seo.get("tags") or [])[:8]

        search_terms = data.get("search_terms") or []
        if not isinstance(search_terms, list):
            search_terms = [str(search_terms)]
        data["search_terms"] = search_terms[:8]

        return data


# ==============================================================================
# ZDIEĽANÉ POMOCNÉ FUNKCIE (WC produkt <-> raw_data, audit kvality)
# ==============================================================================

UNCATEGORIZED_NAMES = {"uncategorized", "nezaradené", "nezaradene", "bez kategórie", "bez kategorie"}


def wc_product_to_raw_data(product: Dict[str, Any]) -> Dict[str, Any]:
    """Prevedie surový WooCommerce REST API produkt na štandardný `product_raw_data`
    vstup pre `process_product_with_ai`, zachovávajúc jeho ID a aktuálny stav."""
    meta = {m.get("key"): m.get("value") for m in product.get("meta_data", []) if isinstance(m, dict)}
    return {
        "wc_product_id": product["id"],
        "source": meta.get("_supplier_source") or "wc-audit",
        "supplier_url": meta.get("supplier_url") or "",
        "name": product.get("name", ""),
        "sku": product.get("sku", ""),
        "ean": meta.get("_ean") or meta.get("_gpg_gtin") or "",
        "description": product.get("description", ""),
        "short_description": product.get("short_description", ""),
        "regular_price": product.get("regular_price", ""),
        "sale_price": product.get("sale_price", ""),
        "existing_images": [img["src"] for img in product.get("images", []) if img.get("src")],
        "status": product.get("status", "publish"),
    }


def assess_product_quality(product: Dict[str, Any], config: Config) -> Dict[str, Any]:
    """Skontroluje kvalitu zaradenia a textov existujúceho WooCommerce produktu
    a vráti príznaky, ktoré presne určujú, PREČO treba produkt znova preohnať cez AI."""
    categories = product.get("categories") or []
    category_names = {(c.get("name") or "").strip().lower() for c in categories}
    category_slugs = {(c.get("slug") or "").strip().lower() for c in categories}
    needs_category = (not categories) or bool(category_names & UNCATEGORIZED_NAMES) or ("uncategorized" in category_slugs)

    # Aj kategórie mimo pevného stromu SmartGaráž považujeme za nepresné.
    allowed_names = {n.lower() for n in all_allowed_names()}
    if categories and not needs_category:
        if not any((c.get("name") or "").strip().lower() in allowed_names for c in categories):
            needs_category = True

    description_len = len((product.get("description") or "") + (product.get("short_description") or ""))
    needs_content = description_len < config.min_description_length
    has_cyrillic = any(
        contains_cyrillic(product.get(f) or "")
        for f in ("name", "short_description", "description")
    )
    if has_cyrillic:
        needs_content = True

    images = [img.get("src", "") for img in product.get("images", []) if img.get("src")]
    placeholder_hints = HDImageFinder.PLACEHOLDER_HINTS
    needs_image = (not images) or any(any(h in url.lower() for h in placeholder_hints) for url in images)

    # Anglické ALL-CAPS názvy (typické B2B importy) treba preložiť do slovenčiny.
    product_name = product.get("name") or ""
    letters = [ch for ch in product_name if ch.isalpha()]
    needs_slovak_name = bool(letters) and (sum(1 for ch in letters if ch.isupper()) / len(letters) > 0.7)

    meta = {m.get("key"): m.get("value") for m in product.get("meta_data", []) if isinstance(m, dict)}
    has_ean = bool(meta.get("_ean") or meta.get("_gpg_gtin"))
    has_meta_title = bool(meta.get("rank_math_title"))
    has_meta_description = bool(meta.get("rank_math_description"))
    needs_seo = not (has_ean and has_meta_title and has_meta_description)

    reasons = []
    if needs_category:
        reasons.append("kategória chýba/Uncategorized/mimo pevnú taxonómiu")
    if description_len < config.min_description_length:
        reasons.append(f"chudobný popis (<{config.min_description_length} znakov)")
    if has_cyrillic:
        reasons.append("popis obsahuje cyriliku / zmiešaný skript")
    if needs_image:
        reasons.append("chýba HD fotka alebo je placeholder")
    if needs_slovak_name:
        reasons.append("názov nie je v slovenčine")
    if needs_seo:
        reasons.append("chýba EAN/SEO meta pre Google Merchant")

    return {
        "needs_category": needs_category,
        "needs_content": needs_content or needs_slovak_name,
        "needs_image": needs_image,
        "needs_seo": needs_seo,
        "needs_reprocessing": needs_category or needs_content or needs_image or needs_seo or needs_slovak_name,
        "reasons": reasons,
    }


# ==============================================================================
# HLAVNÁ ORCHESTRAČNÁ FUNKCIA
# ==============================================================================

def _apply_pricing_and_stock(payload: Dict[str, Any], product_raw_data: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Cena z nákupnej + sklad/akcia do WC payloadu (shared full + fast path)."""
    cost_price_raw = product_raw_data.get("cost_price")
    try:
        cost_price_val = float(str(cost_price_raw).replace(",", ".")) if cost_price_raw not in (None, "") else None
    except (TypeError, ValueError):
        cost_price_val = None
    retail_floor: Optional[float] = None

    def _fprice(raw: Any) -> Optional[float]:
        try:
            if raw in (None, ""):
                return None
            v = float(str(raw).replace(",", ".").replace("€", "").strip())
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    # PRO-TEC akcia: pôvodná MOC (list 42,32) vs predaj (final 33,86).
    # retail_hint / sale = čo zákazník platí; regular = prečiarknutá pôvodná.
    list_price_val = _fprice(product_raw_data.get("list_price"))
    extra_early = product_raw_data.get("raw_extra") or {}
    if list_price_val is None and isinstance(extra_early, dict):
        list_price_val = _fprice(extra_early.get("list_price"))
        b2b_early = extra_early.get("b2b") if isinstance(extra_early.get("b2b"), dict) else {}
        if list_price_val is None:
            list_price_val = _fprice(b2b_early.get("list_price"))

    if cost_price_val and cost_price_val > 0:
        try:
            from pricing import calculate_retail

            supplier_hint = " ".join(
                str(x)
                for x in (
                    product_raw_data.get("source"),
                    product_raw_data.get("brand"),
                    product_raw_data.get("name"),
                )
                if x
            )
            # Predajná MOC (nie list/pôvodná) — retail_hint pred regular_price.
            retail_hint_val: Optional[float] = None
            for key in ("retail_hint",):
                h = _fprice(product_raw_data.get(key))
                if h:
                    retail_hint_val = h
                    break
            sale_as_hint = _fprice(product_raw_data.get("sale_price"))
            if retail_hint_val is None and sale_as_hint:
                retail_hint_val = sale_as_hint
            if retail_hint_val is None:
                # Bez akcie: regular = predajná MOC
                retail_hint_val = _fprice(product_raw_data.get("regular_price"))
            extra = product_raw_data.get("raw_extra") or {}
            if isinstance(extra, dict) and retail_hint_val is None:
                for key in ("retail_hint",):
                    h = _fprice(extra.get(key))
                    if h:
                        retail_hint_val = h
                        break
                b2b = extra.get("b2b") if isinstance(extra.get("b2b"), dict) else {}
                if retail_hint_val is None and b2b.get("retail_hint"):
                    retail_hint_val = _fprice(b2b.get("retail_hint"))

            pricing_result = calculate_retail(
                cost_price_val,
                supplier=supplier_hint,
                name=str(product_raw_data.get("name") or ""),
                brand=str(product_raw_data.get("brand") or ""),
                sku=str(product_raw_data.get("sku") or ""),
                retail_hint=retail_hint_val,
            )
            payload["regular_price"] = f"{pricing_result.retail_price:.2f}"
            retail_floor = float(pricing_result.retail_price)
            report["pricing"] = {
                "cost_price": pricing_result.cost_price,
                "retail_price": pricing_result.retail_price,
                "profit_eur": pricing_result.profit_eur,
            }
        except Exception:
            LOGGER.exception("Výpočet ceny z nákupnej zlyhal")
            if product_raw_data.get("regular_price"):
                payload["regular_price"] = str(product_raw_data["regular_price"])
    elif product_raw_data.get("regular_price"):
        payload["regular_price"] = str(product_raw_data["regular_price"])

    # Dodávateľská „akcia“: PRO-TEC Shoptet list→sale OK (pôvodná MOC 42,32 /
    # predaj 33,86). B2B partner cenu pod nákup stále zahodíme.
    if "sale_price" in product_raw_data:
        sale = product_raw_data.get("sale_price")
        sale_f: Optional[float] = _fprice(sale)
        # PRO-TEC: nekopíruj B2B/akciu pod jednotkový cost (min. objednávka
        # je prevádzka, nie floor predajnej ceny — viď pricing.py).
        cost_floor = cost_price_val
        try:
            from pricing import is_protec_source, _protec_apply_min_order_to_moc, protec_effective_unit_cost

            supplier_blob = " ".join(
                str(x)
                for x in (
                    product_raw_data.get("source"),
                    product_raw_data.get("brand"),
                    product_raw_data.get("name"),
                )
                if x
            )
            if (
                cost_price_val
                and is_protec_source(source=supplier_blob)
                and _protec_apply_min_order_to_moc()
            ):
                cost_floor = protec_effective_unit_cost(cost_price_val)
        except Exception:
            cost_floor = cost_price_val

        drop_sale = False
        if sale_f is None or sale_f <= 0:
            drop_sale = True
        elif cost_floor and sale_f <= cost_floor + 0.009:
            drop_sale = True
            LOGGER.warning(
                "⛔ Sale %.2f € ≤ cost/floor %.2f € — ignorujem dodávateľskú akciu (pod nákup)",
                sale_f,
                cost_floor,
            )
        else:
            try:
                reg_f = float(str(payload.get("regular_price") or retail_floor or 0))
            except (TypeError, ValueError):
                reg_f = 0.0
            # List (pôvodná MOC) môže byť vyššia ako predaj — to je OK akcia.
            list_f = list_price_val or _fprice(product_raw_data.get("regular_price"))
            if list_f and list_f > sale_f + 0.009:
                reg_f = list_f
            if reg_f > 0 and sale_f >= reg_f - 0.009:
                drop_sale = True
            # Aj „akcia“ musí nechať aspoň min. zisk nad efektívnym nákladom
            elif cost_floor and (sale_f - cost_floor) < 1.0:
                drop_sale = True
                LOGGER.warning(
                    "⛔ Sale %.2f € pri cost/floor %.2f € — marža < 1 €, akciu ruším",
                    sale_f,
                    cost_floor,
                )
        if drop_sale:
            payload["sale_price"] = ""
        else:
            # Predaj = sale; regular = pôvodná maloobchodná (42,32), nie B2B.
            if list_price_val and list_price_val > sale_f + 0.009:
                payload["regular_price"] = f"{list_price_val:.2f}"
            elif _fprice(product_raw_data.get("regular_price")) and (
                _fprice(product_raw_data.get("regular_price")) or 0
            ) > sale_f + 0.009:
                payload["regular_price"] = f"{_fprice(product_raw_data.get('regular_price')):.2f}"
            payload["sale_price"] = f"{sale_f:.2f}"
            if retail_floor is None:
                retail_floor = sale_f

    qty = product_raw_data.get("stock_qty")
    if qty is not None and qty != "":
        try:
            payload["stock_quantity"] = int(float(qty))
            payload["manage_stock"] = True
        except (TypeError, ValueError):
            pass
    if product_raw_data.get("stock_status"):
        payload["stock_status"] = str(product_raw_data["stock_status"])
    # Dostupnosť SR / EÚ / nedostupné (availability.py) — meta _sg_avail + stock_status
    try:
        from availability import apply_to_payload

        _av = apply_to_payload(payload, product_raw_data)
        if _av:
            report["sg_avail"] = _av
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("availability skip: %s", exc)


def _fast_sync_product(
    product_raw_data: Dict[str, Any],
    existing: Dict[str, Any],
    wc_client: WooCommerceClient,
) -> Dict[str, Any]:
    """Bez Anthropic — len cena/sklad/dodanie/meta. Použiť keď obsah už je OK."""
    pid = int(existing["id"])
    name = existing.get("name") or product_raw_data.get("name") or f"#{pid}"
    report: Dict[str, Any] = {
        "name": name,
        "product_name_sk": name,
        "warnings": [],
        "success": False,
        "fast_path": True,
        "wc_product_id": pid,
        "wc_status": existing.get("status"),
        "category_path": [c.get("name") for c in (existing.get("categories") or []) if c.get("name")],
    }
    meta = [
        {"key": "supplier_url", "value": product_raw_data.get("supplier_url") or ""},
        {"key": "_supplier_source", "value": product_raw_data.get("source") or ""},
        {"key": "_supplier_sku", "value": product_raw_data.get("sku") or existing.get("sku") or ""},
        {"key": "_cost_price", "value": str(product_raw_data.get("cost_price") or "")},
        {"key": "_delivery_days", "value": str(product_raw_data.get("delivery_days") or "")},
        {"key": "_delivery_text", "value": str(product_raw_data.get("delivery_text") or "")},
        {"key": "_supplier_stock", "value": str(product_raw_data.get("stock_qty") or "")},
        {"key": "_last_b2b_sync", "value": time.strftime("%Y-%m-%d %H:%M:%S")},
    ]
    if product_raw_data.get("ean"):
        ean_s = str(product_raw_data["ean"]).strip()
        meta.append({"key": "_ean", "value": ean_s})
        meta.append({"key": "_gpg_gtin", "value": ean_s})
    sku_s = str(product_raw_data.get("sku") or existing.get("sku") or "").strip()
    if sku_s:
        meta.append({"key": "_mpn", "value": sku_s})

    # Multi-vehicle: rovnaké SKU pod iným Catalog URL → union `_sg_fit_*`
    # (inak --fast len cena/sklad a fitment ostane pri prvom aute).
    try:
        from fitment import (
            fitment_meta_payload,
            merge_fitment_meta_with_existing,
            parse_fitment_from_supplier,
        )

        fit = parse_fitment_from_supplier(product_raw_data)
        oe = fit.get("oe_numbers") or []
        if isinstance(oe, str):
            oe = [oe]
        if fit.get("make") or fit.get("model") or oe:
            fit_meta = fitment_meta_payload(
                make=fit.get("make") or "",
                model=fit.get("model") or "",
                engine=fit.get("engine") or "",
                year_from=fit.get("year_from"),
                year_to=fit.get("year_to"),
                oe_numbers=oe or None,
                sg_id=fit.get("sg_id") or "",
            )
            fit_meta = merge_fitment_meta_with_existing(
                fit_meta, existing.get("meta_data") or []
            )
            meta.extend(fit_meta)
    except Exception as exc:
        LOGGER.debug("Fast fitment meta skip: %s", exc)

    try:
        from oem_quality import quality_meta_rows

        for row in quality_meta_rows(
            product_raw_data if isinstance(product_raw_data, dict) else {}
        ):
            meta.append(row)
    except Exception as exc:
        LOGGER.debug("Fast quality tier skip: %s", exc)

    payload: Dict[str, Any] = {"meta_data": meta}
    if product_raw_data.get("ean"):
        # WooCommerce / Google for WooCommerce číta GTIN z global_unique_id
        payload["global_unique_id"] = str(product_raw_data["ean"]).strip()
    _apply_pricing_and_stock(payload, product_raw_data, report)

    # Zlá/placeholder fotka → nahraj z dodávateľa
    finder = HDImageFinder(Config(), wc_client)
    wc_imgs = [img.get("src", "") for img in (existing.get("images") or []) if img.get("src")]
    needs_img = (not wc_imgs) or any(
        finder._looks_like_placeholder(u) or finder._looks_like_generic_stock(u) for u in wc_imgs
    )
    if needs_img:
        supplier_imgs = filter_usable_supplier_images(
            [
                u for u in (product_raw_data.get("existing_images") or [])
                if u and "smartgaraz.sk" not in u.lower() and "/wp-content/uploads/" not in u.lower()
                and not finder._looks_like_placeholder(u)
            ],
            supplier_source=str(product_raw_data.get("source") or ""),
        )
        if supplier_imgs:
            prepared = finder.prepare_image(supplier_imgs[0], name_hint=product_raw_data.get("sku") or name)
            if prepared:
                payload["images"] = [{"src": prepared, "alt": name}]
                payload["meta_data"] = merge_image_provenance_into_meta(
                    list(payload.get("meta_data") or meta),
                    original_url=supplier_imgs[0],
                    supplier_source=str(product_raw_data.get("source") or ""),
                )

    try:
        result = wc_client.upsert_product(pid, payload)
        report["success"] = True
        report["wc_status"] = result.get("status") or existing.get("status")
        LOGGER.info("⚡ Fast sync #%s (bez AI) — cena/sklad", pid)
    except Exception as exc:
        report["warnings"].append(str(exc))
        LOGGER.exception("Fast sync zlyhal #%s", pid)
    return report


def process_product_with_ai(
    product_raw_data: Dict[str, Any],
    config: Optional[Config] = None,
    wc_client: Optional[WooCommerceClient] = None,
    ai_engine: Optional[ClaudeContentEngine] = None,
    researcher: Optional[ExternalResearcher] = None,
    image_finder: Optional[HDImageFinder] = None,
    fast: bool = False,
    cheap: bool = False,
    extra_meta: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Spracuje JEDEN produkt z akéhokoľvek zdroja cez celý AI pipeline:
    1) dynamická kategorizácia (+ auto-vytvorenie kategórie vo WooCommerce)
    2) externý prieskum + luxusný copywriting (ak sú B2B dáta chudobné)
    3) HD image finder (ak produktu chýba kvalitná fotka)
    4) Google Merchant & SEO sync (meta title/description, EAN/GTIN meta polia)

    `fast=True`: ak SKU už existuje a kvalita je OK → len cena/sklad/meta (bez Anthropic).
    `cheap=True` / AI_MODE=cheap: €0 Anthropic — keyword kategória + template copy
      + fotka len z dodávateľa (žiadny DDG / web research). Bulk oleje/filtre.

    Vracia report dict so stavom každého kroku — jednotlivé kroky zlyhávajú
    izolovane (napr. ak sa nenájde obrázok, produkt sa aj tak uloží).
    """
    if not cheap:
        env_mode = (os.getenv("AI_MODE") or "").strip().lower()
        cheap = env_mode in {"cheap", "template", "budget", "zero"}

    config = config or Config()
    missing = config.validate(require_anthropic=not cheap)
    if missing:
        raise RuntimeError(f"Chýbajú povinné premenné prostredia: {', '.join(missing)}")

    wc_client = wc_client or WooCommerceClient(config)
    if not cheap:
        ai_engine = ai_engine or ClaudeContentEngine(config)
        researcher = researcher or ExternalResearcher(config)
    image_finder = image_finder or HDImageFinder(config, wc_client)

    name = product_raw_data.get("name", "(bez názvu)")
    # Jidoka: žiadne skryté formátovanie / fingerprint z dodávateľského HTML
    for _fld in ("name", "description", "short_description"):
        if product_raw_data.get(_fld):
            product_raw_data[_fld] = strip_hidden_formatting(str(product_raw_data[_fld]))
    name = product_raw_data.get("name", name)
    LOGGER.info(
        "▶️  Spracúvam produkt: %s [%s]%s",
        name,
        product_raw_data.get("source", "?"),
        " [CHEAP]" if cheap else "",
    )

    # Distribútor (napr. PRO-TEC) ≠ výrobca (napr. Mopar/Pennzoil/Mobil 1).
    # Oprav "brand" HNEĎ na vstupe, nech sa správna značka použije všade
    # ďalej v pipeline (kategorizácia, AI copywriting, tagy, hľadanie fotky).
    detected_brand = detect_true_brand(
        name,
        product_raw_data.get("description", ""),
        fallback=str(product_raw_data.get("brand") or ""),
        sku=str(product_raw_data.get("sku") or ""),
    )
    if detected_brand and detected_brand != product_raw_data.get("brand"):
        LOGGER.info("🏷️  Značka opravená: '%s' -> '%s'", product_raw_data.get("brand") or "(žiadna)", detected_brand)
        product_raw_data = dict(product_raw_data)
        product_raw_data["brand"] = detected_brand

    # Skoré SKU → ID (cache) + voliteľný fast path
    existing_wc: Optional[Dict[str, Any]] = None
    pid = product_raw_data.get("wc_product_id")
    if not pid and product_raw_data.get("sku"):
        pid = wc_client.find_product_id_by_sku(str(product_raw_data["sku"]))
        if pid:
            product_raw_data = dict(product_raw_data)
            product_raw_data["wc_product_id"] = pid
    if pid:
        try:
            existing_wc = wc_client.get_product(int(pid))
        except Exception as exc:
            LOGGER.debug("get_product(%s) skip: %s", pid, exc)
            existing_wc = None

    if fast and existing_wc:
        quality = assess_product_quality(existing_wc, config)
        # Pre --fast NEblokuj len chýbajúcim EAN/SEO — oleje často nemajú GTIN.
        # AI treba pri zlej kategórii, chudobnom texte/názve alebo zlej fotke.
        must_ai = bool(
            quality.get("needs_category")
            or quality.get("needs_content")
            or quality.get("needs_image")
        )
        if not must_ai:
            LOGGER.info(
                "⚡ Fast path #%s — obsah/foto OK (SEO/EAN voliteľné), len sync ceny/skladu",
                existing_wc.get("id"),
            )
            return _fast_sync_product(product_raw_data, existing_wc, wc_client)
        if cheap:
            LOGGER.info(
                "⚡ Cheap+fast: existujúci #%s potrebuje obsah, ale idem template (0 € Claude)",
                existing_wc.get("id"),
            )
        else:
            LOGGER.info(
                "Fast vyžiadaný, ale treba AI (#%s): %s",
                existing_wc.get("id"),
                "; ".join(
                    r for r in (quality.get("reasons") or [])
                    if "EAN/SEO" not in r
                )
                or "; ".join(quality.get("reasons") or [])
                or "quality",
            )

    report: Dict[str, Any] = {"name": name, "warnings": [], "success": False, "cheap_path": cheap}

    # ---- 0) Externý prieskum PRED kategorizáciou pre "záhadné" kódy dielov ----
    # (napr. "44-40 44-44" vyzerá ako chladiaca kvapalina podľa balenia, ale je to
    # kód transfer case prevodovky — bez overenia to AI/keyword pravidlá uhádnu zle)
    pre_research_context = ""
    if (
        not cheap
        and match_keyword_category(product_raw_data) is None
        and ExternalResearcher.looks_cryptic(name)
    ):
        try:
            LOGGER.info("🔍 Názov '%s' vyzerá ako kód/číslo dielu — overujem externe pred zaradením...", name)
            pre_research_context = researcher.gather_context(product_raw_data)
        except Exception:
            LOGGER.debug("Externé overenie kódu dielu zlyhalo, pokračujem bez neho.")

    # ---- 1) Kategorizácia ------------------------------------------------
    category_path: List[str] = []
    category_id: Optional[int] = None
    try:
        if cheap:
            keyword_path = match_keyword_category(product_raw_data)
            if keyword_path:
                category_path = sanitize_category_path(keyword_path)
            else:
                report["warnings"].append("cheap_no_keyword_category")
                LOGGER.warning(
                    "💸 Cheap: žiadny keyword match pre '%s' — import bez AI kategórie (draft)",
                    name,
                )
        else:
            category_path = ai_engine.categorize(product_raw_data, research_context=pre_research_context)
        if category_path:
            category_id = wc_client.ensure_category_path(category_path)
        report["category_path"] = category_path
        LOGGER.info("📂 Kategória: %s", " > ".join(category_path) if category_path else "(nezaradené)")
    except Exception as exc:
        LOGGER.exception("Kategorizácia zlyhala pre '%s'", name)
        report["warnings"].append(f"categorization_failed: {exc}")

    # ---- 2) Externý prieskum + copywriting -------------------------------
    content: Dict[str, Any] = {}
    try:
        if cheap:
            content = build_template_content(product_raw_data, category_path)
            LOGGER.info("💸 Cheap template: %s", content.get("product_name_sk") or name)
        else:
            research_context = pre_research_context
            if not research_context and researcher.needs_research(product_raw_data):
                LOGGER.info("🔎 Popis je chudobný, spúšťam externý prieskum...")
                research_context = researcher.gather_context(product_raw_data)
            content = ai_engine.generate_content(
                product_raw_data, research_context, category_path=category_path
            )
            if content.get("product_name_sk"):
                LOGGER.info("🇸🇰 Slovenský názov: %s -> %s", name, content["product_name_sk"])
    except Exception as exc:
        LOGGER.exception("Generovanie obsahu zlyhalo pre '%s'", name)
        report["warnings"].append(f"content_generation_failed: {exc}")

    # ---- 3) HD Image Finder -----------------------------------------------
    # Priorita: 1) fotka z dodávateľskej stránky (PRO-TEC/Shoptet/…)
    #           2) DuckDuckGo HD search (len keď dodávateľ nemá použiteľnú fotku)
    # Cheap: len dodávateľ / existujúca WC fotka — žiadny DDG.
    # Watermark LKQ/Szekop → zahodiť; proveniencia do `_sg_image_*` meta.
    image_url = None
    deferred_image = None
    image_original_url = None
    try:
        force_image = bool(product_raw_data.get("force_image")) and not cheap
        existing = [
            u for u in (product_raw_data.get("existing_images") or [])
            if u and not image_finder._looks_like_placeholder(u)
            and not looks_like_supplier_watermark(u)
        ]
        # Externé URL z crawlu dodávateľa (myshoptet CDN, …) — NIE lokálne
        # WP uploady, ktoré môžu byť staré/zlé DDG zásahy.
        supplier_imgs = filter_usable_supplier_images(
            [
                u for u in existing
                if "smartgaraz.sk" not in u.lower() and "/wp-content/uploads/" not in u.lower()
            ],
            supplier_source=str(product_raw_data.get("source") or ""),
        )
        wc_local_good = False
        wc_local_black = None
        if existing_wc and not force_image:
            for img in existing_wc.get("images") or []:
                src = (img.get("src") or "").strip()
                if not src:
                    continue
                if image_finder._looks_like_placeholder(src) or image_finder._looks_like_generic_stock(src):
                    continue
                if image_finder._image_mismatches_product(src, product_raw_data):
                    # Zlá rodina produktu vo filename (Engine Clean na Grease…)
                    # — neber ako „OK lokálnu“, nech prebehne supplier/DDG výmena.
                    LOGGER.info("🖼️  Lokálna fotka nesedí k produktu: %s", src[-80:])
                    continue
                if image_finder._looks_like_black_studio(src):
                    wc_local_black = src
                    continue
                if "smartgaraz.sk" in src.lower():
                    wc_local_good = True
                    break

        if force_image:
            LOGGER.info("🖼️  force_image — hľadám novú HD fotku / whiten…")
            found = image_finder.find_best_image(product_raw_data)
            src_fallback = (existing_wc.get("images") or [{}])[0].get("src") if existing_wc else None
            pick = found or src_fallback or (existing[0] if existing else None)
            if pick:
                image_original_url = pick
                image_url = image_finder.prepare_image(pick, name_hint=product_raw_data.get("sku") or name)
                LOGGER.info("✅ Fotka pripravená (force): %s", image_url)
            else:
                report["warnings"].append("no_hd_image_found")
        elif wc_local_black and not wc_local_good:
            # Studio-black nikdy nie je OK — whiten aj v --cheap (inak topsell
            # karty ostanú s čiernym plate po re-importe).
            LOGGER.info("🖼️  Studio-black lokálna fotka — vybielujem pozadie: %s", wc_local_black[:100])
            image_original_url = wc_local_black
            image_url = image_finder.prepare_image(
                wc_local_black, name_hint=product_raw_data.get("sku") or name
            )
            if image_url:
                LOGGER.info("✅ Vybielená fotka: %s", image_url)
            else:
                report["warnings"].append("black_studio_whiten_failed")
        elif supplier_imgs and wc_local_good:
            LOGGER.info("🖼️  Skip re-upload — WC už má OK lokálnu fotku (#%s)", existing_wc.get("id"))
        elif supplier_imgs and os.environ.get("SG_DEFER_IMAGES") == "1" and not product_raw_data.get("wc_product_id"):
            # Rýchly koncept: fotku doplní neskôr enrich_images.py (1 worker, šetrne).
            deferred_image = supplier_imgs[0]
            LOGGER.info("🖼️  Fotka odložená (enrich neskôr): %s", deferred_image[:120])
        elif supplier_imgs:
            LOGGER.info("🖼️  Používam fotku z dodávateľa (nie DuckDuckGo): %s", supplier_imgs[0][:120])
            image_original_url = supplier_imgs[0]
            image_url = image_finder.prepare_image(
                supplier_imgs[0], name_hint=product_raw_data.get("sku") or name
            )
            if image_url:
                LOGGER.info("✅ Fotka z dodávateľa pripravená: %s", image_url)
        elif cheap:
            if not wc_local_good:
                report["warnings"].append("cheap_no_supplier_image")
                LOGGER.info("💸 Cheap: bez fotky dodávateľa — ukladám bez DDG")
        elif image_finder.needs_image(product_raw_data) and not wc_local_good:
            LOGGER.info("🖼️  Hľadám HD obrázok produktu (SKU/EAN/názov, biele pozadie)...")
            found = image_finder.find_best_image(product_raw_data)
            if found:
                image_original_url = found
                image_url = image_finder.prepare_image(found, name_hint=product_raw_data.get("sku") or name)
                LOGGER.info("✅ Nájdený HD obrázok: %s", image_url)
            else:
                report["warnings"].append("no_hd_image_found")
        elif not product_raw_data.get("wc_product_id") and existing:
            # NOVÝ produkt bez supplier CDN URL, ale s nejakou existing_images
            # (napr. už nahratá lokálna) — musí ísť do payloadu, inak bez fotky.
            image_original_url = existing[0]
            image_url = image_finder.prepare_image(existing[0], name_hint=product_raw_data.get("sku") or name)
    except Exception as exc:
        LOGGER.exception("Hľadanie obrázka zlyhalo pre '%s'", name)
        report["warnings"].append(f"image_search_failed: {exc}")

    # ---- 4) Zostavenie WooCommerce payloadu (vrátane SEO/Merchant sync) --
    seo = content.get("seo_rank_math", {})
    ean = product_raw_data.get("ean", "")
    product_name_sk = content.get("product_name_sk") or name

    meta_data = [
        {"key": "_ai_processed", "value": "cheap" if cheap else "yes"},
        {"key": "_ai_mode", "value": "cheap" if cheap else "full"},
        {"key": "_ean", "value": ean},
        {"key": "_gpg_gtin", "value": ean},
        {"key": "supplier_url", "value": product_raw_data.get("supplier_url", "")},
        {"key": "_supplier_source", "value": product_raw_data.get("source", "")},
        {"key": "_supplier_sku", "value": product_raw_data.get("sku", "")},
        {"key": "_mpn", "value": str(product_raw_data.get("sku") or "")},
        {"key": "_cost_price", "value": str(product_raw_data.get("cost_price") or "")},
        {"key": "_delivery_days", "value": str(product_raw_data.get("delivery_days") or "")},
        {"key": "_delivery_text", "value": str(product_raw_data.get("delivery_text") or "")},
        {"key": "_supplier_stock", "value": str(product_raw_data.get("stock_qty") or "")},
    ]
    if deferred_image:
        meta_data.append({"key": "_sg_pending_image", "value": deferred_image})
    # Würth Art.-Nr. / proveniencia dodávateľa (C&D / audit)
    extra_raw = product_raw_data.get("raw_extra") if isinstance(product_raw_data.get("raw_extra"), dict) else {}
    if extra_raw.get("_sg_supplier"):
        meta_data.append({"key": "_sg_supplier", "value": str(extra_raw["_sg_supplier"])})
    if extra_raw.get("_sg_supplier_art_no"):
        meta_data.append(
            {"key": "_sg_supplier_art_no", "value": str(extra_raw["_sg_supplier_art_no"])}
        )
    elif str(product_raw_data.get("source") or "").lower() in ("wurth", "würth"):
        meta_data.append({"key": "_sg_supplier", "value": "wurth_sk"})
        if product_raw_data.get("sku"):
            meta_data.append(
                {"key": "_sg_supplier_art_no", "value": str(product_raw_data["sku"])}
            )
    if str(product_raw_data.get("source") or "").lower() in ("wurth", "würth"):
        # Balenie / pôvod (SK/DE) — cena je za CELÉ balenie (1 WC kus = 1 balenie).
        _pv = bool(extra_raw.get("pack_verified"))
        meta_data.append({"key": "_sg_pack_verified", "value": "1" if _pv else "0"})
        for _k, _mk in (("pack_qty", "_sg_pack_qty"), ("pack_price", "_sg_pack_cost"),
                        ("unit_cost", "_sg_unit_cost"), ("price_basis", "_sg_price_basis"),
                        ("origin", "_sg_origin"), ("origin_text", "_sg_origin_text")):
            if extra_raw.get(_k) not in (None, ""):
                meta_data.append({"key": _mk, "value": str(extra_raw[_k])})
        _oe = product_raw_data.get("oe_numbers") or []
        if _oe:
            meta_data.append({"key": "_sg_oe_numbers", "value": "|".join(str(x) for x in _oe)})
        meta_data.append({"key": "_sg_wurth_fee_note", "value": (
            "Würth VOP čl. 2.6/5.12: poplatok za spracovanie objednávky 6,80 € pri objednávke do 100 €.")})
    if seo.get("focus_keyword"):
        meta_data.append({"key": "rank_math_focus_keyword", "value": seo["focus_keyword"]})
    if seo.get("meta_title"):
        meta_data.append({"key": "rank_math_title", "value": seo["meta_title"]})
    if seo.get("meta_description"):
        meta_data.append({"key": "rank_math_description", "value": seo["meta_description"]})
    if content.get("search_terms"):
        meta_data.append({"key": "_ai_search_terms", "value": ", ".join(content["search_terms"])})
    if seo.get("image_description"):
        meta_data.append({"key": "_ai_image_description", "value": seo["image_description"]})

    if image_url and image_original_url:
        meta_data = merge_image_provenance_into_meta(
            meta_data,
            original_url=image_original_url,
            supplier_source=str(product_raw_data.get("source") or ""),
        )

    # Elektrické parametre batérie / alternátora (Ah, CCA, V) — Lavro meta, nie TecDoc.
    try:
        from electrical_specs import electrical_specs_meta

        for row in electrical_specs_meta(product_raw_data if isinstance(product_raw_data, dict) else {}):
            meta_data.append(row)
    except Exception as exc:
        LOGGER.debug("electrical specs meta skip: %s", exc)

    # Motorový olej (viskozita / VW·Toyota approvals) + OEM quality tier.
    try:
        from service_kit_specs import oil_specs_meta
        from oem_quality import quality_meta_rows

        for row in oil_specs_meta(product_raw_data if isinstance(product_raw_data, dict) else {}):
            meta_data.append(row)
        for row in quality_meta_rows(product_raw_data if isinstance(product_raw_data, dict) else {}):
            meta_data.append(row)
    except Exception as exc:
        LOGGER.debug("service kit / quality meta skip: %s", exc)

    if extra_meta:
        meta_data.extend(extra_meta)

    # Fitment podľa typu auta (ak dodávateľ poslal make/model/…)
    try:
        from fitment import (
            fitment_meta_payload,
            merge_fitment_meta_with_existing,
            parse_fitment_from_supplier,
        )

        fit = parse_fitment_from_supplier(product_raw_data)
        oe = fit.get("oe_numbers") or []
        if isinstance(oe, str):
            oe = [oe]
        fit["oe_numbers"] = oe
        if fit.get("make") or fit.get("model") or oe:
            fit_meta = fitment_meta_payload(
                make=fit.get("make") or "",
                model=fit.get("model") or "",
                engine=fit.get("engine") or "",
                year_from=fit.get("year_from"),
                year_to=fit.get("year_to"),
                oe_numbers=oe or None,
                sg_id=fit.get("sg_id") or "",
            )
            # Rovnaké SKU na viac áut (MQB filtre) — union, neprepíš predchádzajúci model
            if existing_wc:
                fit_meta = merge_fitment_meta_with_existing(
                    fit_meta, existing_wc.get("meta_data") or []
                )
            meta_data.extend(fit_meta)
    except Exception as exc:
        LOGGER.debug("Fitment meta skip: %s", exc)

    full_description = normalize_sk_product_name(
        content.get("full_description") or product_raw_data.get("description", "")
    )
    short_description = normalize_sk_product_name(
        content.get("short_description") or product_raw_data.get("short_description", "")
    )
    product_name_sk = normalize_sk_product_name(product_name_sk)

    # Cheap bez keyword kategórie → vždy draft (žiadne publikovanie slepých zásahov)
    status = product_raw_data.get("status", "draft")
    if cheap and "cheap_no_keyword_category" in (report.get("warnings") or []):
        status = "draft"
    # Nejasný typ dielu → NIKDY fallback kategória (WC default „Auto chémia“); draft + dôvod v HQ.
    _cp = report.get("category_path") or []
    if (not category_id) or (match_keyword_category(product_raw_data) is None and len(_cp) < 2) or any(
        (c or "").strip().lower() == "auto chémia" for c in _cp
    ):
        status = "draft"
        meta_data = list(meta_data or []) + [{"key": "_sg_hold_reason", "value": "nejasná kategória"}]
        report.setdefault("warnings", []).append("unclear_category_draft")
    # Hlavný diel bez OE čísla nikdy nejde von (výnimka: drobné komponenty).
    if status == "publish":
        try:
            from oe_publish_rule import has_oe_numbers, needs_oe

            _m = {m.get("key"): m.get("value") for m in (meta_data or []) if isinstance(m, dict)}
            _m.setdefault("_supplier_source", product_raw_data.get("source") or "")
            if needs_oe(product_name_sk, _m) and not has_oe_numbers(_m):
                status = "draft"
                meta_data = list(meta_data or []) + [{"key": "_sg_hold_reason", "value": "chýba OE číslo"}]
                report.setdefault("warnings", []).append("no_oe_draft")
        except Exception:
            pass

    # Würth: nikdy nepublikuj bez overeného balenia + pôvodu (SK/DE).
    if status == "publish" and str(product_raw_data.get("source") or "").lower() in ("wurth", "würth"):
        if not (extra_raw.get("pack_verified") and extra_raw.get("origin")):
            status = "draft"
            meta_data = list(meta_data or []) + [
                {"key": "_sg_hold_reason", "value": "Würth: overiť balenie (ks) a pôvod SK/DE pred predajom"}]

    payload: Dict[str, Any] = {
        "name": product_name_sk,
        "sku": product_raw_data.get("sku", ""),
        "description": full_description,
        "short_description": short_description,
        "meta_data": meta_data,
        "status": status,
    }
    if ean:
        # Merchant Center / GLA — GTIN musí byť v WC poli global_unique_id
        payload["global_unique_id"] = str(ean).strip()
    if category_id:
        payload["categories"] = [{"id": category_id}]

    # Brand taxonómia (chipy na webe / brand archív) — nie len tagy v názve
    brand_for_tax = (product_raw_data.get("brand") or "").strip()
    if brand_for_tax and not (
        is_distributor_brand(brand_for_tax)
        and brand_for_tax.lower() not in (product_name_sk or name or "").lower()
    ):
        try:
            brand_id = wc_client.ensure_brand(brand_for_tax)
            if brand_id:
                payload["brands"] = [{"id": brand_id}]
        except Exception as exc:
            LOGGER.warning("Brand assign skip pre '%s': %s", brand_for_tax, exc)
            report["warnings"].append(f"brand_assign_failed: {exc}")

    # Cenotvorba + sklad (shared helper)
    _apply_pricing_and_stock(payload, product_raw_data, report)
    if image_url:
        payload["images"] = [{"src": image_url, "alt": seo.get("alt_text") or product_name_sk}]
    if seo.get("tags"):
        payload["tags"] = [{"name": tag} for tag in seo["tags"]]

    report["product_name_sk"] = product_name_sk
    report["search_terms"] = content.get("search_terms", [])
    report["seo_tags"] = seo.get("tags", [])
    report["name"] = product_name_sk

    try:
        result = wc_client.upsert_product(product_raw_data.get("wc_product_id"), payload)
        report["success"] = True
        report["wc_product_id"] = result.get("id")
        report["wc_status"] = result.get("status") or product_raw_data.get("status") or "draft"
        LOGGER.info("💾 Produkt uložený vo WooCommerce (ID: %s)", result.get("id"))
    except Exception as exc:
        # WooCommerce sťahuje `images[].src` na svojej strane — ak zdrojový
        # web zablokuje priamy hotlink (403/Forbidden) alebo URL inak zlyhá,
        # CELÝ update zlyhá a stratíme aj korektnú kategóriu/popis/cenu, hoci
        # s obrázkom to nesúvisí. Skús to isté uložiť BEZ obrázka, nech aspoň
        # zvyšok prejde (fotku dorieši ďalší beh, keď bude smartgaraz-bridge
        # nasadený). Neriešime PREČO update zlyhal — ak má payload obrázok,
        # skúsiť bez neho je vždy bezpečný fallback.
        if payload.get("images"):
            LOGGER.warning("Uloženie s obrázkom zlyhalo (%s), skúšam bez obrázka...", exc)
            payload_without_image = {k: v for k, v in payload.items() if k != "images"}
            try:
                result = wc_client.upsert_product(product_raw_data.get("wc_product_id"), payload_without_image)
                report["success"] = True
                report["wc_product_id"] = result.get("id")
                report["wc_status"] = result.get("status") or "draft"
                report["warnings"].append(f"image_upload_failed_saved_without_image: {exc}")
                LOGGER.info("💾 Produkt uložený BEZ obrázka (ID: %s)", result.get("id"))
            except Exception as exc2:
                LOGGER.exception("Uloženie do WooCommerce zlyhalo aj bez obrázka pre '%s'", name)
                report["warnings"].append(f"woocommerce_save_failed: {exc2}")
        else:
            LOGGER.exception("Uloženie do WooCommerce zlyhalo pre '%s'", name)
            report["warnings"].append(f"woocommerce_save_failed: {exc}")

    return report


# ==============================================================================
# NEPRETRŽITÁ OPTIMALIZAČNÁ SLUČKA (BEH NA POZADÍ)
# ==============================================================================

def run_continuous_optimization_loop(
    interval_seconds: int = 3600,
    batch_size: int = 20,
    config: Optional[Config] = None,
    max_iterations: Optional[int] = None,
) -> None:
    """Beží donekonečna (`while True`) na pozadí a priebežne udržiava kvalitu
    celého WooCommerce katalógu:

    1) Načíta ďalšiu dávku produktov (napr. 20), postupne prechádza celý katalóg.
    2) Skontroluje kvalitu zaradenia a textov každého produktu:
       - "Uncategorized" / chýbajúca alebo nepresná kategória -> AI produkt znova
         zaradí (`process_product_with_ai` prekategorizuje a uloží).
       - chudobný popis alebo chýbajúci HD obrázok -> AI dovyhľadá externé zdroje
         a inzerát aktualizuje (nový copywriting + nový obrázok).
       - chýbajúce SEO meta dáta alebo EAN pre Google Merchant -> AI ich doplní.
       Produkty, ktoré sú už v poriadku, sa preskočia (šetria sa AI/API náklady).
    3) Po spracovaní dávky počká `interval_seconds` a pokračuje ďalšou dávkou.
    4) Podrobne loguje do konzoly aj do `bot/optimization_log.txt` — presne vidno,
       ktoré produkty AI vylepšila, ako sa zmenila kategória a čo bolo doplnené.

    Parameter `max_iterations` slúži len na testovanie (obmedzí počet cyklov);
    v produkcii sa nechá `None` a slučka beží donekonečna, kým ju niekto
    neukončí (Ctrl+C / zastavenie systemd služby / Task Scheduler).
    """
    setup_logging()
    setup_optimization_logging()

    config = config or Config()
    missing = config.validate()
    if missing:
        message = f"Chýbajú povinné premenné prostredia: {', '.join(missing)}"
        OPT_LOGGER.error(message)
        raise RuntimeError(message)

    wc_client = WooCommerceClient(config)
    ai_engine = ClaudeContentEngine(config)
    researcher = ExternalResearcher(config)
    image_finder = HDImageFinder(config, wc_client)

    OPT_LOGGER.info("=" * 79)
    OPT_LOGGER.info(
        "🚀 SPÚŠŤAM NEPRETRŽITÚ AI OPTIMALIZÁCIU KATALÓGU (dávka=%d ks, interval=%ds)",
        batch_size, interval_seconds,
    )
    OPT_LOGGER.info("=" * 79)

    iteration = 0
    try:
        while True:
            iteration += 1
            cycle_started = time.time()
            OPT_LOGGER.info("")
            OPT_LOGGER.info("🔁 --- CYKLUS #%d --- %s", iteration, time.strftime("%Y-%m-%d %H:%M:%S"))

            try:
                batch = wc_client.get_audit_batch(batch_size=batch_size)
            except Exception as exc:
                OPT_LOGGER.exception("Nepodarilo sa načítať dávku produktov z WooCommerce: %s", exc)
                batch = []

            if not batch:
                OPT_LOGGER.info("📭 Žiadne produkty na kontrolu v tejto dávke.")

            improved, skipped, failed = 0, 0, 0

            for product in batch:
                product_id = product.get("id")
                name = product.get("name", "(bez názvu)")

                try:
                    quality = assess_product_quality(product, config)
                except Exception as exc:
                    OPT_LOGGER.exception("Kontrola kvality zlyhala pre ID %s ('%s'): %s", product_id, name, exc)
                    failed += 1
                    continue

                if not quality["needs_reprocessing"]:
                    OPT_LOGGER.info("✅ [ID %s] '%s' — kvalita v poriadku, preskakujem.", product_id, name)
                    skipped += 1
                    continue

                OPT_LOGGER.info(
                    "⚠️  [ID %s] '%s' — zistené problémy: %s",
                    product_id, name, "; ".join(quality["reasons"]),
                )
                category_before = ", ".join(c.get("name", "") for c in product.get("categories", [])) or "Uncategorized"

                try:
                    raw_data = wc_product_to_raw_data(product)
                    result = process_product_with_ai(
                        raw_data, config=config, wc_client=wc_client,
                        ai_engine=ai_engine, researcher=researcher, image_finder=image_finder,
                    )
                except Exception as exc:
                    OPT_LOGGER.exception("AI reprocessing zlyhal pre ID %s ('%s'): %s", product_id, name, exc)
                    failed += 1
                    continue

                if result.get("success"):
                    improved += 1
                    category_after = " > ".join(result.get("category_path", [])) or category_before
                    OPT_LOGGER.info(
                        "✨ [ID %s] '%s' VYLEPŠENÉ AI ENGINom. Kategória: '%s' -> '%s'. "
                        "Doplnené: %s. Varovania: %s",
                        product_id, name, category_before, category_after,
                        "; ".join(quality["reasons"]),
                        ", ".join(result.get("warnings", [])) or "žiadne",
                    )
                else:
                    failed += 1
                    OPT_LOGGER.warning(
                        "❌ [ID %s] '%s' — spracovanie dokončené s chybami: %s",
                        product_id, name, ", ".join(result.get("warnings", [])),
                    )

            cycle_duration = time.time() - cycle_started
            OPT_LOGGER.info(
                "📊 Súhrn cyklu #%d: vylepšených=%d, preskočených=%d, zlyhaných=%d (trvanie %.1fs)",
                iteration, improved, skipped, failed, cycle_duration,
            )

            if max_iterations is not None and iteration >= max_iterations:
                OPT_LOGGER.info("🏁 Dosiahnutý limit %d iterácií, ukončujem slučku.", max_iterations)
                break

            OPT_LOGGER.info("😴 Čakám %d sekúnd do ďalšej dávky...", interval_seconds)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        OPT_LOGGER.info("🛑 Prerušené používateľom (Ctrl+C) po %d cykloch, končím.", iteration)


# ==============================================================================
# CLI
# ==============================================================================

def _load_products_from_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else [data]


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="SmartGaráž AI Engine")
    parser.add_argument("--input", help="Cesta k JSON súboru so zoznamom product_raw_data objektov")
    parser.add_argument("--resync-missing", action="store_true",
                         help="Dotiahne z WooCommerce produkty bez meta '_ai_processed' a spracuje ich")
    parser.add_argument("--continuous", action="store_true",
                         help="Spustí run_continuous_optimization_loop() — beží donekonečna na pozadí")
    parser.add_argument("--interval", type=int, default=3600,
                         help="Interval (v sekundách) medzi dávkami pri --continuous, default 3600")
    parser.add_argument("--batch-size", type=int, default=20,
                         help="Počet produktov na dávku pri --continuous, default 20")
    parser.add_argument("--sleep", type=float, default=1.0, help="Pauza (v sekundách) medzi produktmi")
    args = parser.parse_args()

    config = Config()
    missing = config.validate()
    if missing:
        LOGGER.error("Chýbajú povinné premenné prostredia: %s", ", ".join(missing))
        sys.exit(1)

    if args.continuous:
        run_continuous_optimization_loop(interval_seconds=args.interval, batch_size=args.batch_size, config=config)
        return

    wc_client = WooCommerceClient(config)
    ai_engine = ClaudeContentEngine(config)
    researcher = ExternalResearcher(config)
    image_finder = HDImageFinder(config, wc_client)

    products: List[Dict[str, Any]] = []
    if args.input:
        products = _load_products_from_file(args.input)
    elif args.resync_missing:
        LOGGER.info("🔄 Načítavam produkty bez AI spracovania z WooCommerce...")
        wc_products = wc_client.get_products_needing_ai()
        products = [wc_product_to_raw_data(p) for p in wc_products]
    else:
        parser.print_help()
        sys.exit(1)

    LOGGER.info("📦 Produktov na spracovanie: %d", len(products))
    results = []
    for idx, product in enumerate(products, 1):
        LOGGER.info("--- [%d/%d] ---", idx, len(products))
        try:
            result = process_product_with_ai(
                product, config=config, wc_client=wc_client,
                ai_engine=ai_engine, researcher=researcher, image_finder=image_finder,
            )
            results.append(result)
        except Exception as exc:
            LOGGER.exception("Kritická chyba pri spracovaní produktu '%s'", product.get("name"))
            results.append({"name": product.get("name"), "success": False, "error": str(exc)})
        time.sleep(args.sleep)

    ok = sum(1 for r in results if r.get("success"))
    LOGGER.info("🎉 Hotovo. Úspešne spracovaných: %d/%d", ok, len(results))


if __name__ == "__main__":
    main()
