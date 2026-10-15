# -*- coding: utf-8 -*-
"""Publish pravidlo: hlavný autodiel bez OE čísla ostáva koncept.

Výnimky (môžu ísť von bez OE): drobné komponenty, ktoré výrobca auta
samostatne nepredáva (sady na opravu, piesty, vodiace puzdrá, spony,
tesnenia, konektory…), chémia/oleje, náradie, žiarovky, tuning sady.
"""
from __future__ import annotations

import re

MAJOR = re.compile(
    r"altern[aá]tor|gener[aá]tor|[šs]tart[eé]r\b|kondenz[aá]tor|chladi[čc]|radi[aá]tor|[čc]erpadl|kompresor|"
    r"turbo|vstrekova[čc]|kot[uú][čc]|kotou[čc]|do[šs]ti[čc]k|obložen|strme[ňn]|tlmi[čc]|tlumi[čc]|"
    r"filt(er|r)|sn[ií]ma[čc]|senzor|sonda|lo[žz]isk|rameno|[čc]ap\b|spojk|rozvod|termostat|svetlomet|"
    r"zrkadl|n[aá]boj|pol[oa]os|cievk|katalyz|\begr\b|\bdpf\b|uloženie|stabiliz|riadeni|ventil[aá]tor|servomotor",
    re.I,
)
SMALL = re.compile(
    r"opravn|na opravu|vodiac|puzd|piest|pr[ií]slu[šs]enst|mont[aá][žz]|upevnen|výstražn|kontakt|súprava ložiska brzd|tesnen|kr[uú][žz]ok|konektor|"
    r"z[aá]suvk|z[aá]str[čc]k|k[aá]bel|k[aá]bl|priechodk|vsuvk|[žz]iarov|poistk|rel[eé]|spon|klip|skrutk|"
    r"matic|podlo[žz]k|krytk|ochrann[ýy] plech|kľúč|n[aá]radi|n[aá]stroj|adapt[eé]r|olej\b|kvapalin|sprej|"
    r"čisti|cleaner|výplach|flush|batéri|akumul|nabíja|kliešt|zariaden|\(\d+[,.]?\d*\s*(ml|l)\)|\d+w-?\d+",
    re.I,
)
TUNING = re.compile(r"\bEIBACH\b|K&(amp;)?N\b|\bBILSTEIN\s+B(8|1[2-6])\b|\bB(8|1[2-6])\b.*\bBILSTEIN\b|Pro-Kit|Pro-Street", re.I)
NON_PART_SUPPLIERS = {"wurth", "jlm", "metabond", "pro-tec"}


def needs_oe(name: str, meta: dict) -> bool:
    src = str((meta or {}).get("_supplier_source") or "").lower()
    if src in NON_PART_SUPPLIERS:
        return False
    n = name or ""
    if SMALL.search(n) or TUNING.search(n):
        return False
    return bool(MAJOR.search(n))


def has_oe_numbers(meta: dict, sku: str = "") -> bool:
    v = str((meta or {}).get("_sg_oe_numbers") or "").strip()
    return bool([x for x in v.split("|") if len(x.strip()) >= 5])
