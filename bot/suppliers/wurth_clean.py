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
from typing import Dict, List, Optional

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
