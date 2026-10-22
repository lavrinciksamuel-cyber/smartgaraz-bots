#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Würth: balenie (ks), základ ceny, pôvod (SK/DE), čistý popis + OE čísla.

Würth B2B cena je za BALENIE (napr. 11,90 € net = 1 × balenie 10 ks), niekedy
„za 100 ks“. Predávame CELÉ balenie ako jeden kus vo WooCommerce:
cena = z ceny balenia (bežná marža), názov „… (balenie 10 ks)“.

Bez overeného balenia (`pack_verified=False`) produkt NESMIE ísť do predaja.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, List, Optional

PACK_PATTERNS = (
    # „Počet x Balenie 1 x 10 ks“ / „1 x 10 ks“
    r"po[čc]et\s*x\s*balenie[^0-9]{0,40}\d+\s*[x×]\s*(\d{1,6})\s*ks",
    r"\b1\s*[x×]\s*(\d{1,6})\s*ks\b",
    r"balenie\s*[:\-]?\s*(\d{1,6})\s*ks",
    r"obsah\s+balenia\s*[:\-]?\s*(\d{1,6})\s*ks",
    r"v\s+balen[ií]\s*[:\-]?\s*(\d{1,6})\s*ks",
    r"(?:baliaca\s+jednotka|predajn[áa]\s+jednotka|VE)\s*[:\-]?\s*(\d{1,6})\s*ks",
    r"min(?:imálne|\.)?\s*(?:objedn[áa]vacie\s*)?(?:množstvo|balenie)\s*[:\-]?\s*(\d{1,6})\s*ks",
)
PRICE_PER_N = re.compile(r"(?:cena\s*)?(?:za|/)\s*(\d{1,5})\s*ks", re.I)
OE_RE = re.compile(
    r"Origin[áa]lne\s+č[íi]slo\s+n[áa]hradn[ée]ho\s+dielu\s*[:\-]?\s*([0-9A-Z][0-9A-Z ,;./\-]{3,400})",
    re.I,
)

JUNK_LINE_RE = re.compile(
    r"^(certifik[áa]ty\s*/?|opis|použitie|technické informácie|dokumenty|príslušenstvo|"
    r"zobraziť (všetky )?verzi[ue]|\||/|—|-|ďalší tovar.*|identifikačné a technické údaje|"
    r"kompatibilita.*|použitie\s*&(amp;)?\s*aplikácia)$",
    re.I,
)
DISCLAIMER_RE = re.compile(
    r"Pokyny na(?: použitie)?\s*(?:predstavujú|sú)\s*odporúčania.*$", re.I | re.S
)
GENERIC_BLURB_RE = re.compile(
    r"(Pred montážou\s*/\s*výmenou over kompatibilitu[^.]*\.|"
    r"Over kompatibilitu podľa OE / viskozity pred nákupom\.|"
    r"pri neistote over OE číslo alebo SAE viskozitu\.)",
    re.I,
)


def parse_pack_qty(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text or "")
    for pat in PACK_PATTERNS:
        m = re.search(pat, t, re.I)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if 1 <= n <= 100000:
                return n
    return None


def parse_price_basis(text: str) -> Optional[int]:
    """„Cena za 100 ks“ / „/100 ks“ → 100. None = cena za balenie (Počet x Balenie)."""
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(r"cena\s*(?:za|/)\s*(\d{1,5})\s*ks", t, re.I) or re.search(
        r"€\s*/\s*(\d{1,5})\s*ks", t, re.I
    )
    if m:
        n = int(m.group(1))
        return n if n > 1 else None
    return None


def parse_origin(text: str) -> Dict[str, str]:
    """„Dodávané z Nemecka“ → DE, zo Slovenska/SR sklad → SK."""
    t = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    out = {"origin": "", "origin_text": ""}
    m = re.search(r"(dod[áa]van[ée]\s+z[o]?\s+[A-Za-zÁ-ž]+[^.|\n]{0,60})", t, re.I)
    if m:
        out["origin_text"] = m.group(1).strip()[:120]
    low = t.lower()
    if re.search(r"dod[áa]van[ée]\s+z\s+nemecka|z\s+nemeck[ée]ho\s+skladu|sklad\s+nemecko", low):
        out["origin"] = "DE"
    elif re.search(r"dod[áa]van[ée]\s+zo?\s+slovensk|sklad(?:om)?\s+(?:sr|slovensko|bratislava)|z\s+centr[áa]lneho\s+skladu", low):
        out["origin"] = "SK"
    return out


def parse_oe_numbers(text: str) -> List[str]:
    out: List[str] = []
    for m in OE_RE.finditer(text or ""):
        for tok in re.split(r"[,;]\s*|\s{2,}", m.group(1)):
            tok = tok.strip(" .-/")
            if re.fullmatch(r"[0-9A-Z][0-9A-Z.\-/ ]{3,24}", tok or "") and any(c.isdigit() for c in tok):
                tok = tok.split(" ")[0] if len(tok.split(" ")) > 1 and not tok.replace(" ", "").isdigit() else tok
                if tok not in out:
                    out.append(tok)
    return out[:40]


def clean_description_lines(text: str, name: str = "") -> List[str]:
    """Riadky dodávateľského popisu bez navigácie, „Certifikáty /“, OE riadku, disclaimeru."""
    t = _html.unescape(text or "")
    t = re.sub(r"<[^>]+>", "\n", t)
    t = DISCLAIMER_RE.sub("", t)
    t = GENERIC_BLURB_RE.sub("", t)
    t = re.sub(r"Certifik[áa]ty\s*/\s*[|\s]*", "\n", t, flags=re.I)
    t = OE_RE.sub("\n", t)
    # Navigácia Würth PDP (tabs / „Zobraziť viac…“ / dokumenty)
    t = re.sub(r"\b(Opis|Použitie|Technické informácie|Dokumenty|Príslušenstvo|Oblasť použitia|"
               r"Informácie o výrobku|Katalóg spoločnosti Würth|Hárky údajov\s*\(\w*\)|"
               r"Zobraziť viac\.*|Zobraziť menej\.*|Pokyny na použitie)\b\.*", "\n", t)
    t = re.sub(r"[^\n]*›[^\n]*", "\n", t)  # breadcrumb / kategória echo
    t = re.sub(r"Ďalšie\s*:\s*Výrobok\s+Popis[^\n]*", "\n", t)  # príslušenstvo s cenami/baleniami
    t = re.sub(r"Karta bezpečnostných údajov|&\s*Aplikácia|Použi podľa návodu výrobcu[^.]*\.(\s*Dodrž[^.]*\.)?|"
               r"Dodrž bezpečnosť práce[^.]*\.|Over kompatibilitu[^.]*\.", "\n", t)
    t = re.sub(r"\s*\|\s*", "\n", t)
    lines: List[str] = []
    nl = (name or "").strip().lower()
    for raw in t.split("\n"):
        ln = re.sub(r"\s+", " ", raw).strip(" |/·-–")
        if len(ln) < 3 or JUNK_LINE_RE.match(ln):
            continue
        if nl and ln.lower() == nl:
            continue
        if ln.lower() in ("würth", "wurth", "produkt") or re.fullmatch(r"[\d\s.,/-]+", ln):
            continue
        if ln.lower().startswith(("výrobca", "kód produktu", "ean kód", "počet v balení", "objem/hmotnosť", "kategória")):
            continue
        if ln not in lines:
            lines.append(ln)
    return lines[:40]


def build_clean_html(
    name: str,
    lines: List[str],
    *,
    sku: str = "",
    ean: str = "",
    pack_qty: Optional[int] = None,
    oe_numbers: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Elegantný popis: úvod + odrážky + Balenie + OE čísla (bez tabuliek)."""
    esc = _html.escape
    paras = to_paragraphs(lines)
    intro = [l for l in paras if len(l) >= 90][:6]
    bullets = [l for l in paras if l not in intro and len(l) < 90][:16]
    parts = [f"<h2>{esc(name)}</h2>"]
    for p in intro:
        parts.append(f"<p>{esc(p)}</p>")
    if bullets:
        parts.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>")
    facts = []
    if pack_qty and pack_qty > 1:
        facts.append(f"<li><strong>Balenie výrobcu:</strong> {pack_qty} ks</li>")
    elif pack_qty == 1:
        facts.append("<li><strong>Balenie:</strong> 1 ks</li>")
    if sku:
        facts.append(f"<li><strong>Katalógové číslo Würth:</strong> {esc(sku)}</li>")
    if ean:
        facts.append(f"<li><strong>EAN:</strong> {esc(ean)}</li>")
    if facts:
        parts.append("<h3>Údaje o produkte</h3><ul>" + "".join(facts) + "</ul>")
    if oe_numbers:
        parts.append(
            "<h3>Originálne čísla dielu (OE)</h3><p>" + esc(", ".join(oe_numbers)) + "</p>"
            "<p>Pred objednaním over zhodu OE čísla s dielom na tvojom vozidle.</p>"
        )
    first = intro[0] if intro else (
        f"{name} – " + ", ".join(b.rstrip(".").lower() if i else b.rstrip(".") for i, b in enumerate(bullets[:3])) + "."
        if bullets else (f"{name} – originálne čísla dielu (OE): {', '.join(oe_numbers[:4])}." if oe_numbers else name)
    )
    if len(first) > 260:
        first = first[:257].rsplit(" ", 1)[0] + "…"
    short_bits = [first]
    if pack_qty and pack_qty > 1 and "balenie" not in short_bits[0].lower():
        short_bits.append(f"Balenie výrobcu {pack_qty} ks.")
    short = " ".join(short_bits)
    if len(short) > 420:
        short = short[:417].rsplit(" ", 1)[0] + "…"
    return {"description": "\n".join(parts), "short_description": f"<p>{esc(short)}</p>"}


def pack_title(name: str, pack_qty: Optional[int]) -> str:
    """Pridaj „(balenie N ks)“ do názvu, ak tam ešte nie je."""
    n = re.sub(r"\s*\((?:balenie\s*)?\d{1,6}\s*ks\)\s*$", "", (name or "").strip(), flags=re.I)
    if pack_qty and pack_qty > 1:
        return f"{n} (balenie {pack_qty} ks)"
    return n


def pack_from_supplier_block(text: str, sku: str) -> Optional[int]:
    """Würth tabuľka „Výrobok Popis Počet Balenie Cena / balenie … Obj. č. SKU … x N ks“
    pre VLASTNÉ SKU → počet ks v balení (cena Würth je „Cena / balenie“)."""
    if not sku:
        return None
    t = re.sub(r"<[^>]+>", " ", _html.unescape(text or ""))
    t = re.sub(r"\s+", " ", t)
    m = re.search(rf"Obj\.?\s*č\.?\s*{re.escape(sku)}\b(.{{0,200}}?)\bx\s*(\d{{1,6}})\s*ks\b", t, re.I)
    if m and "Obj." not in m.group(1):
        return int(m.group(2))
    return None


_LC = "a-záäčďéíľĺňóôŕšťúýž"
_UC = "A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ"
FEATURE_SPLIT = re.compile(rf"(?<=[{_LC}0-9\)%])\s+(?=[{_UC}][{_LC}])")


def split_features(sentence: str) -> List[str]:
    """Würth odrážky zlepené do jedného riadku („…neoprénu Široký rozsah…“)."""
    parts = [x.strip() for x in FEATURE_SPLIT.split(sentence) if x.strip()]
    return parts if len(parts) >= 3 else [sentence]


def to_paragraphs(lines: List[str], max_sent: int = 3) -> List[str]:
    out: List[str] = []
    for ln in lines:
        sents = re.split(rf"(?<=[.!?])\s+(?=[{_UC}0-9])", ln)
        buf: List[str] = []
        for snt in sents:
            if len(snt) > 160:
                if buf:
                    out.append(" ".join(buf)); buf = []
                out.extend(split_features(snt))
            else:
                buf.append(snt)
                if len(buf) >= max_sent:
                    out.append(" ".join(buf)); buf = []
        if buf:
            out.append(" ".join(buf))
    return [o.strip() for o in out if len(o.strip()) >= 3]


def name_pack_hint(name: str) -> Optional[int]:
    """Odhad z názvu („(5 ks)“, „5 kusov“, „Multibalenie“) — LEN hint, nie overenie."""
    m = re.search(r"\((\d{1,5})\s*ks\)|(\d{1,5})\s*kus(?:ov|y)\b", name or "", re.I)
    if m:
        return int(m.group(1) or m.group(2))
    return None


# =============================================================== v3 (2026-10-22)
# Parametre z Würth technického bloku („Obsah 400 ml Hustota 0,9 g/cm³ …“).
SPEC_LABELS = (
    "Hmotnosť výrobku (na kus)", "Hmotnosť obsahu", "Obsah", "DĺžkaxŠírka", "Dĺžka vo zvitku",
    "Dĺžka hadice", "Dĺžka kábla", "Pracovná dĺžka", "Celková dĺžka", "Dĺžka", "Šírka zvitku", "Šírka",
    "Výška", "Celková hrúbka", "Hrúbka", "Vonkajší priemer", "Vnútorný priemer", "Priemer otvoru",
    "Priemer kolíka", "Priemer", "Rozmer závitu", "Závit", "Stúpanie", "Veľkosť kľúča (s)", "Veľkosť",
    "Materiál", "Farba", "Obal", "Chemický základ", "Menovité napätie", "Menovitý prúd",
    "Intenzita prúdu max.", "Výkon druhého špirálového vlákna", "Výkon", "Označenie pätice",
    "Teplota farby", "Teplotná odolnosť max.", "Teplotná odolnosť min.", "Teplotný rozsah použitia min.",
    "Teplotný rozsah použitia max.", "Teplota spracovania min.", "Teplota spracovania max.",
    "Prevádzkový tlak max.", "Max. šírka zovretia", "Hustota / podmienky", "Bod vzplanutia min.",
    "Skladovateľnosť od dátumu výroby / podmienky", "Vhodnosť pre vonkajší priemer rúry",
    "Vhodnosť pre vonkajší priemer hadice", "Vhodnosť pre priemer kábla", "Schválenie", "Norma",
    "Kapacita batérie", "Napätie batérie", "Hmotnosť", "Dĺžkaxšírkaxhrúbka", "DĺžkaxŠírkaxHrúbka",
    "Dĺžkaxšírka", "Počet dielov", "Hustota", "Podmienka pre hustotu pri teplote",
    "Podmienka pre skladovateľnosť od dátumu výroby", "Skladovateľnosť od dátumu výroby",
    "Teplota skladovania", "Skladovacia teplota", "Normy", "Kategória ochrany",
)
# štítky, ktoré len ukončia hodnotu (do parametrov ich nedávame)
SPEC_SKIP = {"Podmienka pre hustotu pri teplote", "Podmienka pre skladovateľnosť od dátumu výroby",
             "Hustota", "Hustota / podmienky"}
EN_WORDS = re.compile(r"\b(permissible|storage|period|frost|fragile|prone|expose|directly|elements|product|"
                      r"suitable|surface|surfaces|easy|clean|protection|use|using|apply|applied|"
                      r"glove|gloves|coating|technology|robust|extra|strong|impact|which|while|without)\b", re.I)
_SPEC_RE = re.compile(
    r"(?<![A-Za-zÁ-ž])((?:" + "|".join(re.escape(x) for x in sorted(SPEC_LABELS, key=len, reverse=True))
    + r")(?:[ ,]+[a-záäčďéíľĺňóôŕšťúýž.()/]+){0,3})\s+(?=[0-9A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ+\-–])"
)
ENGLISH_SEEN: set = set()
EN_RE = re.compile(r"\b(the|and|with|for|of|is|are|this|that|from|your|can|be|to|in|on|or|by|as|at|it|such|into|when|used)\b", re.I)


def is_english(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text or "")
    if len(words) < 4:
        sk = re.search(r"[áäčďéíľĺňóôŕšťúýž]", (text or "").lower())
        return bool(words) and not sk and len(EN_WORDS.findall(text or "")) >= 1
    hits = len(EN_RE.findall(text or ""))
    sk = len(re.findall(r"[áäčďéíľĺňóôŕšťúýž]", (text or "").lower()))
    return hits >= 2 and hits / max(len(words), 1) > 0.08 and sk <= len(words) * 0.15


_UNIT = r"(?:mm|cm|ml|m|l|g/cm³|kg|g|bar|V/DC|V/AC|V|W|°C|ks|KS|Ah|Nm|K|h|A|Mesiac(?:ov)?|mesiacov|%|palcov|\")(?![A-Za-zá-ž])"
_NUM_VAL = re.compile(rf"^([+\-–]?\s?\d[\d.,]*(?:\s?(?:x|×|až|do|-|–|/)\s?[+\-]?\d[\d.,]*)*\s?{_UNIT}?(?:\s?/\s?(?:pri|pri teplote)\s?[\d.,]+\s?°C)?)")


def _tidy_value(val: str) -> str:
    v = (val or "").strip()
    if re.match(r"^[+\-–]?\s?\d", v):
        m = _NUM_VAL.match(v)
        if m:
            v = m.group(1).strip()
        v = re.sub(r"(\d),000\b", r"\1", v)
        return v
    # textová hodnota: koniec pred ďalším „Slovom“ (nový štítok), mimo zátvoriek
    depth = 0
    for i, ch in enumerate(v):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i > 0 and v[i - 1] == " " and re.match(r"[A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ][a-záäčďéíľĺňóôŕšťúýž]{2,}", v[i:]):
            return v[:i].strip()
    return v


def extract_specs(text: str) -> tuple:
    """(specs[(label, value)], zvyšný text). Hodnota = text po ďalší label (max 60 zn.)."""
    t = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    t = re.sub(r" ?\n ?", "\n", t)
    hits = list(_SPEC_RE.finditer(t))
    specs: List[tuple] = []
    cut: List[tuple] = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(t), m.end() + 60)
        val = t[m.end():end].strip(" ,;.\n").split("\n")[0]
        # hodnota končí na vete / ďalšej veľkej vete
        val = re.split(r"(?<=[a-zá-ž0-9)%³²°])\.\s+(?=[A-ZÁ-Ž])", val)[0]
        if not val or len(val) > 60:
            val = val[:60].rsplit(" ", 1)[0] if val else ""
        has_num = bool(re.search(r"\d", val))
        label = m.group(1).strip(" ,")
        val = _tidy_value(val)
        val = re.sub(r"(\d+)\s*Mesiac\b", r"\1 mesiacov", val)
        val = re.sub(r"(\d+)\s*KS\b", r"\1 ks", val)
        base = next((x for x in SPEC_LABELS if label.startswith(x)), label)
        textual_ok = base in ("Materiál", "Farba", "Obal", "Chemický základ", "Označenie pätice", "Schválenie", "Norma",
                              "Normy", "Hustota")
        if base in SPEC_SKIP:
            _nl = t.find("\n", m.end())
            _end = end if i + 1 < len(hits) else m.end() + len(val)
            cut.append((m.start(), min(_end, _nl) if _nl >= 0 else _end))
            continue
        if val and (has_num or (textual_ok and len(val) <= 40)):
            if label not in [s[0] for s in specs]:
                specs.append((label, val))
            _nl = t.find("\n", m.end())
            _end = end if i + 1 < len(hits) else m.end() + len(val)
            cut.append((m.start(), min(_end, _nl) if _nl >= 0 else _end))
    rest = t
    for a, b in sorted(cut, reverse=True):
        rest = rest[:a] + " \n " + rest[b:]
    return specs[:14], rest


USE_RE = re.compile(r"^(Na |Pre |Vhodn|Určen|Použitie|Ideáln|Používa sa|Slúži)", re.I)
BOILER_RE = re.compile(
    r"(Vyhradzujeme si právo|Garantujeme stálu kvalitu|nepreberáme žiadnu zodpovednosť|"
    r"Pred každou aplikáciou vykonajte vlastné testy|zákaznícky servis|Katalóg spoločnosti|"
    r"Hárky údajov|Zobraziť viac|Zobraziť menej)", re.I,
)


def slug_facts(supplier_url: str) -> List[tuple]:
    """Würth interné označenie v URL slugu (…/Nazov-OZNACENIE-M10X1/0500300002.sku/) → závit/materiál."""
    path = (supplier_url or "").split(".sku")[0]
    seg = path.rstrip("/").split("/")[-2] if path.count("/") >= 2 else ""
    up = seg.upper()
    out: List[tuple] = []
    m = re.search(r"\bM(\d{1,2}(?:[.,]\d)?)X(\d(?:[.,]\d{1,2})?)\b", up.replace("-", " "))
    if m:
        out.append(("Závit", f"M{m.group(1)}x{m.group(2)}"))
    if "PLAST" in up:
        out.append(("Materiál", "plast"))
    elif "OCEL" in up or "-OC-" in up:
        out.append(("Materiál", "oceľ"))
    elif "MOSAD" in up:
        out.append(("Materiál", "mosadz"))
    return out


def sale_unit_lines(pack_qty: Optional[int], per_piece: bool = False) -> List[str]:
    if per_piece:
        return ["<strong>Minimálne balenie:</strong> 1 ks – predáva sa po kusoch",
                "<strong>Cena:</strong> za 1 ks"]
    if pack_qty and pack_qty > 1:
        return [f"<strong>Minimálne balenie:</strong> {pack_qty} ks",
                f"<strong>Cena:</strong> za celé balenie ({pack_qty} ks)"]
    if pack_qty == 1:
        return ["<strong>Minimálne balenie:</strong> 1 ks", "<strong>Cena:</strong> za 1 ks"]
    return ["<strong>Minimálne balenie:</strong> 1 originálne balenie Würth (počet kusov v balení overujeme)",
            "<strong>Cena:</strong> za celé balenie Würth"]


def build_description_v3(
    name: str,
    raw_text: str,
    *,
    sku: str = "",
    ean: str = "",
    supplier_url: str = "",
    pack_qty: Optional[int] = None,
    per_piece: bool = False,
    oe_numbers: Optional[List[str]] = None,
    translations: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Elegantný SK popis: čo to je, použitie, parametre, balenie a predaj. Len údaje, ktoré máme."""
    esc = _html.escape
    translations = translations or {}
    lines = clean_description_lines(raw_text, name)
    blob = " \n ".join(lines)
    specs, rest = extract_specs(blob)
    for f in slug_facts(supplier_url):
        if f[0] not in [s[0] for s in specs]:
            specs.append(f)
    paras = to_paragraphs([x for x in rest.split("\n") if x.strip()])
    intro: List[str] = []
    feats: List[str] = []
    uses: List[str] = []
    dropped_en = 0
    title_l = re.sub(r"\s*\(\d{1,6}\s*ks\)", "", name).lower()
    for ptxt0 in paras:
        sents = re.split(r"(?<=[.!?])\s+", ptxt0)
        keep: List[str] = []
        for snt in sents:
            core = snt.strip(" .;,")
            if not core:
                continue
            if core in translations:
                tr_ = translations[core].strip()
                keep.append(tr_ if tr_.endswith((".", "!", "?")) else tr_ + "."); continue
            # zvyšky tabuľky príslušenstva („Popis: … Obj. č.“, kódy) — nie popis produktu
            if re.search(r"\bPopis:|\bObj\b|\bMnožstvo:|^\d{6,}\b|[A-Z]{4,}-[A-Z0-9.-]{4,}|"
                         r"\b[A-ZÁČĎÉÍĽŇÓÔŠŤÚÝŽ]{4,}\s+[A-ZÁČĎÉÍĽŇÓÔŠŤÚÝŽ]{4,}\s+[A-ZÁČĎÉÍĽŇÓÔŠŤÚÝŽ]{3,}", core):
                continue
            if core in ("Easy", "Easy-to-clean", "Charger with connecting cable for"):
                continue
            # krátke anglické zlomky zoznamu obsahu („Coarse filter for“)
            if (len(core.split()) <= 8 and EN_RE.search(core)
                    and not re.search(r"[áäčďéíľĺňóôŕšťúýž]", core.lower())):
                dropped_en += 1
                ENGLISH_SEEN.add(core)
                continue
            if is_english(core) or (len(EN_WORDS.findall(core)) >= 2 and not re.search(r"[áäčďéíľĺňóôŕšťúýž]", core.lower())):
                dropped_en += 1
                ENGLISH_SEEN.add(core)
                continue
            # zmiešané „Skladovacia teplota: 15-25°C Permissible storage period: 12 mesiacov“
            if re.search(r"\b(Permissible storage period|Frost-prone|Easy-to-clean)\b", core, re.I):
                dropped_en += 1
                ENGLISH_SEEN.add(core)
                continue
            keep.append(snt.strip())
        ptxt = " ".join(keep).strip(" .;,")
        if len(ptxt) < 4 or BOILER_RE.search(ptxt):
            continue
        if ptxt[0].islower() or ptxt.lower() in ("würth", "efekt", "pokyny") or ptxt.lower() in title_l:
            continue
        ptxt = re.sub(r"^Pokyny\s+", "", ptxt)
        ptxt = re.sub(r"\s+Pokyny\s+(?=[A-ZÁ-Ž])", ". ", ptxt)
        ptxt = re.sub(r"^Würth\s+(?=[A-ZÁ-Ž])", "", ptxt)
        if len(ptxt) < 4 or ptxt.lower() in ("zahrnuté v dodávke", "obsahuje", "obsah"):
            continue
        ptxt = re.sub(r"\.\.+", ".", ptxt)
        if ptxt in intro or ptxt in feats or ptxt in uses:
            continue
        if len(ptxt.split()) < 2:
            continue
        if USE_RE.match(ptxt):
            uses.append(ptxt)
        elif len(ptxt) >= 90 and len(intro) < 2:
            intro.append(ptxt)
        else:
            feats.append(ptxt)
    title = re.sub(r"\s*\(\d{1,6}\s*ks\)", "", name).strip()
    if sku:
        title = re.sub(rf"\s+{re.escape(sku)}\s*$", "", title).strip()
    parts = [f"<h2>{esc(title)}</h2>"]
    for p in intro:
        parts.append(f"<p>{esc(p if p.endswith(('.', '!')) else p + '.')}</p>")
    if feats:
        parts.append("<h3>Vlastnosti</h3><ul>" + "".join(f"<li>{esc(f)}</li>" for f in feats[:12]) + "</ul>")
    if uses:
        parts.append("<h3>Použitie</h3><ul>" + "".join(f"<li>{esc(u)}</li>" for u in uses[:6]) + "</ul>")
    if specs:
        parts.append("<h3>Parametre</h3><ul>" + "".join(
            f"<li><strong>{esc(k)}:</strong> {esc(v)}</li>" for k, v in specs) + "</ul>")
    sale = sale_unit_lines(pack_qty, per_piece)
    facts = sale + ([f"<strong>Katalógové číslo Würth:</strong> {esc(sku)}"] if sku else []) + (
        [f"<strong>EAN:</strong> {esc(ean)}"] if ean else [])
    parts.append("<h3>Balenie a predaj</h3><ul>" + "".join(f"<li>{x}</li>" for x in facts) + "</ul>")
    if oe_numbers:
        parts.append("<h3>Originálne čísla dielu (OE)</h3><p>" + esc(", ".join(oe_numbers)) + "</p>"
                     "<p>Pred objednaním over zhodu OE čísla s dielom na tvojom vozidle.</p>")
    lead = intro[0] if intro else (uses[0] if uses else "")
    if len(lead) > 200:
        cut_at = lead[:200].rfind(". ")
        lead = lead[:cut_at] if cut_at > 80 else lead[:197].rsplit(" ", 1)[0] + "…"
    if per_piece:
        sale_txt = "Predáva sa po kusoch, cena za 1 ks"
    elif pack_qty and pack_qty > 1:
        sale_txt = f"Minimálne balenie {pack_qty} ks, cena za celé balenie"
    elif pack_qty == 1:
        sale_txt = "Cena za 1 ks"
    else:
        sale_txt = "Cena za celé originálne balenie Würth"
    short = f"{title}" + (f" – {lead[0].lower() + lead[1:]}" if lead else "") + f". {sale_txt}."
    short = short.replace("..", ".").replace("….", "…")
    return {
        "description": "\n".join(parts),
        "short_description": f"<p>{esc(short)}</p>",
        "title": title,
        "specs": specs,
        "dropped_english": dropped_en,
        "has_text": bool(intro or feats or uses),
    }


def _data_json(name: str) -> dict:
    import json
    from pathlib import Path
    try:
        return json.loads((Path(__file__).resolve().parent.parent / "data" / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def known_pack_qty(sku: str) -> Optional[int]:
    """Balenie z Würth tabuliek príslušenstva (bot/data/wurth_pack_qty.json)."""
    v = _data_json("wurth_pack_qty.json").get((sku or "").replace(" ", ""))
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def translations_sk() -> Dict[str, str]:
    return _data_json("wurth_translations_sk.json")


def sale_title(title: str, pack_qty: Optional[int], per_piece: bool = False) -> str:
    t = re.sub(r"\s*[–-]\s*balenie\s+\d+\s*ks\s*$", "", (title or "").strip(), flags=re.I)
    t = re.sub(r"\s*\((?:balenie\s*)?\d{1,6}\s*ks\)", "", t).strip()
    if pack_qty and pack_qty > 1 and not per_piece:
        return f"{t} – balenie {pack_qty} ks"
    return t
