# -*- coding: utf-8 -*-
"""CZ/TecDoc dodávateľské názvy dielov → bežné slovenské pojmy shopu.

Používa ai_engine.normalize_sk_product_name (import boty) aj jednorazové
premenovanie publikovaných produktov. Poradie: frázy pred slovami.
"""
from __future__ import annotations

import re

_PHRASES = [
    (r"\bOrigináln[íi] d[íi]l\b", "Originálny diel"),
    (r"\bHydraulický filtr, automatická převodovka\b", "Hydraulický filter automatickej prevodovky"),
    (r"\bFiltr, vzduch v interiéru\b", "Kabínový filter"),
    (r"\bSnímač, počet otáček kol\b", "Snímač otáčok kolesa"),
    (r"\bOchranný plech proti rozstřikování, brzdový kotouč\b", "Ochranný plech brzdového kotúča"),
    (r"\bSvorkovnice včetně vedení a pinů\b", "Svorkovnica s vedením a pinmi"),
    (r"\bSenzor,? regulace sklonu světlometů\b", "Senzor regulácie sklonu svetlometov"),
    (r"\bSada příslušenství k brzdovému třmenu\b", "Sada príslušenstva brzdového strmeňa"),
    (r"\bPřídavné vodní čerpadlo, elektrické\b", "Prídavné elektrické vodné čerpadlo"),
    (r"\bSada ložisek kol\b", "Sada ložísk kolesa"),
    (r"\bLožisko pružné vzpěry\b", "Ložisko vzpery tlmiča"),
    (r"\bTryska ostřikovače vyhřívaná\b", "Vyhrievaná tryska ostrekovača"),
    (r"\bTryska ostřikovače\b", "Tryska ostrekovača"),
    (r"\bSada pro opravu kabelů, alternátor\b", "Sada na opravu káblov alternátora"),
    (r"\bStop olejovému kouři\b", "Stop olejovému dymu"),
    (r"\bPřísada pro hydraulická zdvihátka\b", "Prísada pre hydraulické zdvíhadlá"),
    (r"\bTlumič pérování\b", "Tlmič pérovania"),
    (r"\bMotor stieračů\b", "Motor stieračov"),
    (r"\bOE FILTR VW AUTOMAT\. PREVOD\.\s*B6\s*2\b", "VW Filter automatickej prevodovky"),
    (r"\bpalivovy filtr\b", "palivový filter"),
    (r"\bKabinový filtr\b", "Kabínový filter"),
    (r"\bmlhový\b", "hmlový"),
    (r"\bradiátor topení\b", "radiátor kúrenia"),
    (r"\bstartovací baterie\b", "štartovacia batéria"),
    (r"\bSada odpružení\b", "Sada odpruženia"),
    (r"\bVystrazny kontakt, opotrebeni oblozeni\b", "Výstražný kontakt opotrebenia obloženia"),
    (r"\bPripojka - set, bocni obrysove svetlo\b", "Prípojka – sada, bočné obrysové svetlo"),
    (r"\bOlejový chladič motorového oleja\b", "Chladič motorového oleja"),
]
_WORDS = [
    (r"\bgener[aá]tor\b", "alternátor"),
    (r"\bfiltr\b", "filter"),
    (r"\bkotouč\b", "kotúč"),
    (r"\btlumičov\b", "tlmičov"),
    (r"\bsvětlomet\b", "svetlomet"),
]
_TRAIL = re.compile(r"\s+(ORIGINÁL|ORIG\.)\s*$")


def _case(canon: str):
    def f(m: re.Match) -> str:
        o = m.group(0)
        if o.isupper() and len(o) > 3:
            return canon.upper()
        if o[:1].isupper():
            return canon[:1].upper() + canon[1:]
        return canon
    return f


_RULES = [(re.compile(p, re.I), r) for p, r in _PHRASES] + [(re.compile(p, re.I), _case(r)) for p, r in _WORDS]


def normalize_part_name(name: str) -> str:
    if not name:
        return name
    out = name
    for rx, rep in _RULES:
        out = rx.sub(rep, out)
    out = _TRAIL.sub("", out)
    # "AS-PL alternátor" → "AS-PL Alternátor" (typ dielu s veľkým písmenom za značkou)
    out = re.sub(r"(?<=\s)alternátor\b", "Alternátor", out) if re.match(r"^\S+\s+alternátor\b", out) else out
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out
