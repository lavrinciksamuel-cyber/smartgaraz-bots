#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zaradenie konceptov bez kategórie (dump „Auto chémia“ / Nezaradené) podľa NÁZVU — pravidlá, bez AI.

* Pravidlá = regexy na názov bez diakritiky (CZ aj SK tvary TecDoc názvov), poradie = priorita
  (špecifické pred všeobecnými). Cieľ = len EXISTUJÚCA kategória obchodu (nové sa nevytvárajú —
  nové kategórie schvaľuje Samuel v HQ).
* CZ → SK názov: slovník fráz (CZ_SK). Premenuje sa len keď po náhrade nezostane české slovo
  (promote_drafts.is_czech); inak názov ostane a promote ho nechá ako koncept.
* Stav (draft) sa nemení; zverejnenie robí promote_drafts.py podľa svojich pravidiel
  (OE číslo, cena ≥ nákup, sklad, …). Ceny sa nemenia. Do logu idú len počty.

  python bot/draft_classifier.py --dry-run            # report, čo by sa zaradilo
  python bot/draft_classifier.py --apply --max 2000   # zapíš kategórie (+SK názvy)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = logging.getLogger("draft_classifier")

ND = "Náhradné diely"
B, F, M, CH, N, R, S, V, E, K = (
    "Brzdy", "Filtre", "Motor & sanie", "Chladenie & kúrenie", "Náprava & podvozok", "Riadenie",
    "Spojka & prevodovka", "Výfuk & DPF diely", "Elektro & senzory", "Karoséria & svetlá")

# (regex na normalizovaný názov, cesta). Normalizácia: malé písmená, bez diakritiky.
ENGINE_INT = "Motor – piesty, ložiská a kľuka"
# Nové kategórie schválené Samuelom; vytvoria sa (len pri --apply), ak chýbajú. Iné sa nezakladajú.
APPROVED_NEW_CATS: List[Tuple[str, ...]] = [("Náhradné diely", "Motor & sanie", ENGINE_INT)]

RULES: List[Tuple[str, Optional[List[str]]]] = [
    # vedome bez kategórie (spojovací materiál, generické OE názvy, sudy)
    (r"^originalny diel \w+ nahradny diel|^originalni dil", None),
    (r"\b(skrutka|sroub|matica|matice|podlozka|spona|nit|klip)\b(?!.*(brzd|kol|vypust|odvzdus|ventil))", None),
    # svetlá pred zrkadlami (blikač v zrkadle)
    (r"blikac|smerov[ey] svet|odrazk|denne svetlo|denni svic|svetlomet|svetlomet|hlavn\w* svetl|zadn\w* svetl|"
     r"koncov\w* svetl|hmlov|mlhov|\bsvetlo\b|\bsvetla\b|osvetleni|osvetlenie|lampa", [ND, K, "Svetlomety & svetlá"]),
    (r"zrkadl|zrcatk|zpetn\w* zrc", [ND, K, "Spätné zrkadlá"]),
    (r"stierac|sterac|stieraci|wiper|aerotwin|\btwin \d+", [ND, K, "Stierače"]),
    (r"\b\d{2,3} ?ah\b|akumulator|autobateri|startovaci bateri|\bbateria\b|\bbaterie\b", [ND, E, "Autobatérie"]),
    # brzdy
    (r"(prislusenstv|sada prislus|montaz\w* dil|pruzin).{0,40}(celist|celust)", [ND, B, "Bubnové brzdy & čeluste"]),
    (r"(prislusenstv|sada prislus).{0,40}(desti|dosti)", [ND, B, "Brzdové doštičky"]),
    (r"cidlo opotreb|snimac opotreb|varovny kontakt|vystrazny kontakt|kontakt opotreb", [ND, B, "ABS senzory & čidlá"]),
    (r"brzdov\w* (desti|dosti)|\bdesticky\b|\bdosticky\b|sada brzdovych (desti|dosti)", [ND, B, "Brzdové doštičky"]),
    (r"brzdov\w* kotou|brzdov\w* kotuc|\bkotouc\w* brzd", [ND, B, "Brzdové kotúče"]),
    (r"strmen|trmen|odvzdusn\w* (sroub|ventil|skrutk)|odvetr\w* (sroub|ventil)|ventilacn\w* skrutk", [ND, B, "Brzdové strmene"]),
    (r"brzdov\w* hadic|hadick\w* brzd|brzdov\w* potrub|hlavn\w* brzdov\w* val|kolov\w* brzdov\w* val|"
     r"brzdov\w* valec|brzdov\w* valc|valec kolov\w* brzd|brzd\.? hadic|nadrz\w* brzd", [ND, B, "Brzdové hadice & valčeky"]),
    (r"celist|celust|brzdov\w* bub|\bbuben\b|bubnov", [ND, B, "Bubnové brzdy & čeluste"]),
    (r"lank\w*.{0,20}brzd|lano.{0,25}brzd|tazn\w* lano|brzdov\w* (lank|lano|kabel)|parkovac\w* brzd|"
     r"rucn\w* brzd|park\.? ?brzd|park s taznym", [ND, B, "Ručná brzda & laná"]),
    (r"\babs\b|snimac otac|snimac pocet otac|otackov\w* (snimac|senzor|cidl)|snimac, pocet", [ND, E, "ABS / otáčkové senzory"]),
    # filtre
    (r"olejov\w* filt|filtr oleje|filter oleja", [ND, F, "Olejové filtre"]),
    (r"kabinov|vnutorn\w* (vzduch|priestor)|vnitrni prostor|pylov|pelov|filtr, vzduch vnitr|filter, vnutorny", [ND, F, "Kabínové filtre"]),
    (r"vzduchov\w* filt|vzduchoveho filtr|filtr vzduchu|vlozka vzduch", [ND, F, "Vzduchové filtre"]),
    (r"palivov\w* filt|naftov\w* filt|filtr paliva|filter paliva", [ND, F, "Palivové filtre"]),
    (r"vysousec|vysusovac|susic|susicka klim", [ND, F, "Filter sušiča klimatizácie"]),
    (r"hydraul\w* filt|filtr\w*.{0,20}automat|prevodov\w* filt|filtr prevodov", [ND, F, "Hydraulické / prevodové filtre"]),
    # výfuk (pred tlmičmi podvozku)
    (r"katalyz", [ND, V, "Katalyzátory"]),
    (r"filtr pevnych|filter pevnych|\bdpf\b|\bfap\b|filtr sazi|filter sadzi|filter castic", [ND, V, "DPF / FAP filtre"]),
    (r"lambda", [ND, V, "Lambda sondy"]),
    (r"\bnox\b", [ND, E, "Lambda & NOx senzory"]),
    (r"(koncov|stredn|predn|zadn)\w* tlumic(?! perov)|tlumic vyfuk|tlmic vyfuk|vyfukov\w* tlmic|vyfukov\w* tlumic", [ND, V, "Tlmiče výfuku"]),
    (r"vyfuk", [ND, V, "Výfukové tesnenia & pružiny"]),
    # podvozok
    (r"lozisk\w* kol|lozisko kola|sada lozisk\w* kol|naboj kol|nabe kol|lozisk\w* naboj", [ND, N, "Ložiská kolies"]),
    (r"(spojovac\w* tyc|tyc\w*|tyck\w*|spojk\w*|drzak|ulozen\w*|pouzdr\w*|silentblok\w*|guma).{0,25}stabiliz", [ND, N, "Spojové tyče"]),
    (r"stabiliz", [ND, N, "Stabilizátory"]),
    (r"tlumic perov|tlmic perov|tlmic|tlumic|pruzin\w* (podvoz|napr)|vinut\w* pruz|pneumatick\w* pero|"
     r"vzduchov\w* pruz|ulozen\w* tlumic|doraz|prachovk\w* tlumic|horne ulozenie|lozisko ulozeni tlum|pruzina", [ND, N, "Tlmiče & pružiny"]),
    (r"(nosny|kloubov|gulov|zaves\w*) (klb|kloub|cep|cap)|kulov\w* cep|guľov", [ND, N, "Guľové čapy & spoje"]),
    (r"silentblok|uloženi ramen|ulozeni ramen|ulozenie ramen|pouzdro ramen|ulozeni, ridic|ulozenie, riad", [ND, N, "Silentbloky"]),
    (r"rameno|ramena|ridic\w* pak|riadiac\w* rameno|vzpera napr|priecne rameno|pricne rameno|nahon\w* rameno", [ND, N, "Ramena & čapy"]),
    # riadenie
    (r"axialn\w* (kloub|klb|cep)|hlav\w* (pricne|priecne|spojovac)|spojovac\w* tyc|tyc riad|koncovk\w* (spojov|tyce)|"
     r"riadiac\w* tyc", [ND, R, "Riadiace tyče & čapy"]),
    (r"(manzet|mech|prachovk)\w*.{0,20}(riad|rizen)", [ND, R, "Manžety riadenia"]),
    (r"hreben|prevodk\w* rizen|riadiac\w* (mechan|prevod)|rizeni$", [ND, R, "Hrebene riadenia"]),
    (r"cerpadl\w* posil|servocerpad|hydraul\w* cerpadl\w*.{0,15}(riz|riad)", [ND, R, "Čerpadlá posilňovača"]),
    # spojka / prevodovka
    (r"(manzet|mech|prachovk)\w*.{0,30}(hridel|poloos|klb|kloub)", [ND, S, "Manžety poloosí"]),
    (r"hnac\w* hridel|poloos|homokinet|kloub hnac|klb hnac|sada klubu|sada kloubu", [ND, S, "Poloosi & kĺby"]),
    (r"setrvacnik|zotrvacnik", [ND, S, "Zotrvačníky"]),
    (r"vypinac\w* lozisk|vysuvac\w* lozisk|centraln\w* vypinac|pracovn\w* valec\w* spojk|vypinaci valec", [ND, S, "Vysúvacie ložiská"]),
    (r"spojk\w* sad|sada spojk|spojka komplet", [ND, S, "Spojkové sady"]),
    (r"pritlacn|lamela spojk|spojkov\w* (kotou|lamel)|kotouc spojk", [ND, S, "Prítlačné taniere & kotúče"]),
    # motor
    (r"klinov\w* reme|drazkov\w* reme|ozuben\w* klinov|poly-?v|\b\d pk\b|\d+pk\d+", [ND, M, "Klinové remene"]),
    (r"rozvod|ozuben\w* reme|napinac\w* (rem|klad|jednot)|vratn\w* klad|vodic\w* klad|napinak|retaz rozvod|rozvodov", [ND, M, "Rozvody & remene"]),
    (r"vodn\w* (cerpadl|pump)", [ND, M, "Vodné pumpy"]),
    (r"olejov\w* (cerpadl|pump)|vypust\w* (sroub|skrut|zatk)|olejov\w* vypust", [ND, M, "Oleové pumpy & vypúšťacie skrutky"]),
    (r"(ulozen|zaves|drzak|silentblok|uchyc)\w* (motor|prevodov)", [ND, M, "Uloženie motora"]),
    (r"zapalovac\w* civk|zapalovac\w* ciev|zapalovac\w* modul", [ND, E, "Zapaľovacie cievky"]),
    (r"svick|svieck|zhavic", [ND, M, "Sviečky & zapaľovanie"]),
    (r"\begr\b|skrtic|skrtiac|recirkulac", [ND, M, "EGR / škrtiaca klapka"]),
    (r"turbo|dmychadl|sacie potrub|sani\b|sacieho|nasavan|plnic\w* (vzduch|potrub)", [ND, M, "Turbá & sanie"]),
    (r"vstrik|vstrek|trysk|palivov\w* (cerpadl|pump)|vysokotlak", [ND, M, "Palivové čerpadlá & vstreky"]),
    (r"tesnen|tesnic|gufer|hridelov\w* kruh|semerink|o-krouzek|krouzek tesn", [ND, M, "Tesnenia motora & sania"]),
    # chladenie
    (r"chladic,? chlazen|chladic motor|chladic chlad|radiator|chladic, chlad", [ND, CH, "Chladiče motora"]),
    (r"termostat", [ND, CH, "Termostaty"]),
    (r"odpor,? (vnitr|ventil)|predradn\w* odpor", [ND, CH, "Kúrenie & výparníky"]),
    (r"ventilator|vetrak|visko|spojka ventil", [ND, CH, "Ventilátory & spojky"]),
    (r"hadic\w* chlad|chladic\w* hadic|hadice chlad|hadica chlad", [ND, CH, "Hadice chladenia"]),
    (r"vyrovnav\w* nadr|expanzn\w* nadob|vyrovnavaci", [ND, CH, "Expanzné nádoby"]),
    (r"kompresor,? klim|kondenzator|klimatiz|expanzn\w* ventil", [ND, CH, "Diely klimatizácie"]),
    (r"vymenik tepla|vymennik tepla|vyparnik|topen|kurenia|ohrivac|odpor,? ventil|predradn\w* odpor|ventilator vnitr", [ND, CH, "Kúrenie & výparníky"]),
    # elektro
    (r"starter|spoustec|alternator|generator|regulator generat|volnobezn\w* (spojk|klad)\w* generat", [ND, E, "Štartéry & alternátory"]),
    (r"\brele\b|\brelé\b|pojistk|poistk", [ND, E, "Relé & poistky"]),
    (r"snimac teplot|cidlo teplot|cidlo, teplot|tlakov\w* (cidl|spinac|snimac)|snimac tlak|snimac plnic|cidlo tlak|snimac, tlak|snimac, teplot", [ND, E, "Teplotné & tlakové čidlá"]),
    (r"ridic\w* jednotk|riadiac\w* jednotk", [ND, E, "Riadiace jednotky"]),
    (r"kabel|konektor|svazek|zvazok|kabelaz|kabelov", [ND, E, "Káble & konektory"]),
    # karoséria
    (r"plynov\w* (vzper|pruz|pist)|elektrick\w* vzper|vzpera (dveri|kufr|kapot|batozin)|vzpera, (kufr|zavazad)|teleskopick\w* vzp", [ND, K, "Plynové vzpery"]),
    (r"naraznik", [ND, K, "Nárazníky & držiaky"]),
    (r"blatnik|mrizk|mriezk|podbeh|lem blatn", [ND, K, "Blatníky & mriežky"]),
    (r"zvedac\w* okn|stahovac\w* okn|mechanizm\w* okn|zdvihac\w* okn", [ND, K, "Sťahovače okien"]),
    (r"celn\w* sklo|autosklo|zadn\w* sklo|bocn\w* sklo", [ND, K, "Autosklá"]),
    (r"zamek|zamok|zamk|klic|kluc|centraln\w* zamyk", [ND, K, "Zámky & kľúče"]),
    # 2026-10-31 doplnok: najčastejšie nezaradené názvy (CZ/SK TecDoc tvary)
    (r"(agr|egr)[ -]?ventil|ventil,? (agr|egr)|potrubi,? (agr|egr)|chladic (agr|egr)", [ND, M, "EGR / škrtiaca klapka"]),
    (r"(hlavn\w*|pomocn\w*|vysuvac\w*|vypinac\w*) valec,? spojk|valec,? spojk|spojkov\w* valec", [ND, S, "Vysúvacie ložiská"]),
    (r"elektromotor\w*.{0,30}(okn|zvedac)|zvedac\w* okn|zdvihac\w* okn|spinac\w* (stahovan|zvedac)\w* okn", [ND, K, "Sťahovače okien"]),
    (r"elektromotor\w*.{0,30}(ventilator|chladic)|ventilator chladic", [ND, CH, "Ventilátory & spojky"]),
    (r"elektromotor\w*.{0,30}(sterac|stierac)|motor (sterac|stierac)", [ND, K, "Stierače"]),
    (r"olejov\w* van|vana olej|vypust\w* (sroub|skrutk)|olejov\w* (cerpadl|pump)", [ND, M, "Oleové pumpy & vypúšťacie skrutky"]),
    (r"chladic oleje|olejov\w* chladic|chladic,? motorov\w* olej", [ND, CH, "Chladiče motora"]),
    (r"(priruba|trubka|potrubi|vedeni|hadic\w*|spojk\w*) chladiv|chladici kapalin\w*.{0,20}(priruba|hadic|trubk)|priruba chlad", [ND, CH, "Hadice chladenia"]),
    (r"sac\w* potrub|sacie potrub|plnic\w* vzduch|turbodmychadl|turbodúchadl|\bturba\b|\bturbo\b|intercooler|nasavan\w* vzduch", [ND, M, "Turbá & sanie"]),
    (r"zdvihatk|vahadl|vackov\w* hridel|vackovy hridel|vedeni ventil|sacich ventil|vyfukov\w* ventil|sac\w* ventil|predstihov|nastavovac vack", [ND, M, "Rozvody & remene"]),
    (r"remenic|napinac\w* (reme|klin)|napinak|vratn\w* kladk|vodic\w* kladk|vodiac\w* kladk", [ND, M, "Klinové remene"]),
    (r"kryt hlavy valc|veko hlavy valc|tesn\w* (veka|krytu) hlavy", [ND, M, "Tesnenia motora & sania"]),
    (r"snimac otac|snimac polohy (vack|klik)|impulsn\w* snimac|senzor polohy (vack|klik)|generator impuls", [ND, E, "ABS / otáčkové senzory"]),
    (r"vstrikovac|vstrekovac|vstrikovaci trysk|vysokotlak\w* cerpadl|palivov\w* cerpadl|dopravni cerpadl", [ND, M, "Palivové čerpadlá & vstreky"]),
    (r"hnac\w* hridel|kloubov\w* hridel|kardan", [ND, S, "Poloosi & kĺby"]),
    (r"\bzamek\b|\bzamok\b|vlozka zamku|klika dveri|klucka dveri|oviadac zamku", [ND, K, "Zámky & kľúče"]),
    (r"mrizk|mriezk|blatnik|podbeh|ozdobn\w* (lista|mrizk)", [ND, K, "Blatníky & mriežky"]),
    (r"naraznik|narazn\w* (drziak|vyztuz)|vyztuha naraz", [ND, K, "Nárazníky & držiaky"]),
    # 2026-10-31 doplnok 2 (zhluky z 3 555 nezaradených konceptov)
    (r"odvetran\w* klikov|odvetr\w* kluk|odlucovac\w* olej|odlucovac oleja|ventil,? odvetr|hadic\w*,? (od)?vetrani klikov", [ND, M, "Tesnenia motora & sania"]),
    (r"(sroub|skrutk)\w*.{0,15}hlav\w* valc", [ND, M, "Tesnenia motora & sania"]),
    (r"lozisk\w*,? pruzn\w* vzper|horn\w* uloz\w*.{0,15}(tlmic|tlumic|vzper)|uloz\w* (tlmic|tlumic|pruzn\w* vzper)|domek (tlmic|tlumic)", [ND, N, "Tlmiče & pružiny"]),
    (r"(snimac|cidlo|senzor|teplot)\w*.{0,25}chlad\w* (kapalin|kvapalin)", [ND, E, "Teplotné & tlakové čidlá"]),
    (r"(vedeni|hadic|potrubi|trubk|priruba|spojk)\w*.{0,25}chlad\w* (kapalin|kvapalin)", [ND, CH, "Hadice chladenia"]),
    (r"(nadrz|nadob)\w*.{0,25}chlad\w* (kapalin|kvapalin)", [ND, CH, "Expanzné nádoby"]),
    (r"nemrzn\w* kapalin|nemrzn\w* kvapalin|antifreeze|chladic\w* kapalin\w*(?!.{0,10}(nadrz|hadic|vedeni|priruba|trubk))|chladiac\w* kvapalin", ["Motorové oleje & Kvapaliny", "Chladiace kvapaliny"]),
    (r"(nadob|nadrz|nadrzk)\w*.{0,20}brzdov\w* (kapalin|kvapalin)", [ND, B, "Brzdové hadice & valčeky"]),
    (r"brzdov\w* kapalin|brzdov\w* kvapalin|brake fluid|\bdot ?[45]\b", ["Motorové oleje & Kvapaliny", "Brzdové kvapaliny"]),
    (r"stahovan\w* okien|stahovani oken|zvedac\w* (zariz\w* )?oken|zvedaci zarizeni|okenn\w* (spinac|zvedac)|stahovac\w* mechanizm", [ND, K, "Sťahovače okien"]),
    (r"parkovac\w* senzor|parkovac\w* snimac|snimac,? parkovac|\bpdc\b", ["Autoelektronika & príslušenstvo", "Parkovacie senzory"]),
    (r"(uzaver|vicko|veko|zatka),? (plnic|olej)|plnic\w* hrdl\w* olej|olejov\w* merk|merk\w* (hladin\w* )?olej|odmerk\w* (hladin\w* )?olej|trychtyr\w* olej", [ND, M, "Oleové pumpy & vypúšťacie skrutky"]),
    (r"(uzavirac\w* )?vicko,? (chladic(?!\w* olej)|nadrz\w* chladic|vyrovnavac)|uzaver,? (chladic|nadrz\w* chladic)", [ND, CH, "Expanzné nádoby"]),
    (r"drzak,? chladic", [ND, CH, "Chladiče motora"]),
    (r"izolac\w* motorov\w* prostor|odhlucn|tlumic\w* hluku motor", [ND, K, "Odhlučnenie"]),
    (r"vysouvac\w* lozisk|vysuvac\w* lozisk|vypinac\w* lozisk", [ND, S, "Vysúvacie ložiská"]),
    (r"brzdov\w* (rurk|trubk)|brzdov\w* potrubi", [ND, B, "Brzdové hadice & valčeky"]),
    (r"(prachov\w*|kryc\w*|ochrann\w*) (stit|plech).{0,20}brzd|stit,? brzd", [ND, B, "Brzdové kotúče"]),
    (r"trubkov\w* modul|sac\w* (trubk|modul)", [ND, M, "Turbá & sanie"]),
    (r"uchyceni dver|zaves\w* dver|zaves dveri|rukovat\w* dver|klik\w* dver|madlo dver", [ND, K, "Zámky & kľúče"]),
    (r"filt\w* vzduch|vzduch\w* filt", [ND, F, "Vzduchové filtre"]),
    (r"filt\w* (oleja|oleje)|olejov\w* filt", [ND, F, "Olejové filtre"]),
    (r"filt\w* (paliva|nafty)|palivov\w* filt", [ND, F, "Palivové filtre"]),
    (r"filt\w* (interier|kabin|pylov)|kabinov\w* filt|pylov\w* filt", [ND, F, "Kabínové filtre"]),
    (r"izolac\w* hlukov|hlukov\w* izolac", [ND, K, "Odhlučnenie"]),
    (r"pruzinov\w* (misa|tanier)|miska pruzin|tanier pruzin", [ND, N, "Tlmiče & pružiny"]),
    (r"protimrazov\w* zatk|mrazov\w* zatk", [ND, M, "Tesnenia motora & sania"]),
    (r"regulator brzd\w* (sily|tlaku)", [ND, B, "Brzdové hadice & valčeky"]),
    # 2026-10-31 Samuel schválil novú kategóriu pre vnútro motora
    (r"(pist|piest)\w*.{0,25}brzd|brzd\w*.{0,25}(pist|piest)", [ND, B, "Brzdové strmene"]),
    (r"\bpist(?!ol)\w*|\bpiest(?!ol)\w*|ojnic|klikov\w* hridel|kluk\w* hriadel|hlavn\w* lozisk|lozisk\w*,? (klikov|kluk)|vlozk\w* valc|pistn\w* (krouzk|kruzk)", [ND, M, ENGINE_INT]),
    # lišty / klipy (do existujúcich kategórií)
    (r"vodic\w* list\w*|list\w*,? vodic|vodiaca lista|list\w* (retez|retaz)", [ND, M, "Rozvody & remene"]),
    (r"klip\w*.{0,25}(interier|podlah|oplast|obloz|calun|vypln)|(interier|podlah|oplast|obloz)\w*.{0,10}klip", [ND, K, "Interiér & obloženie"]),
    (r"(mask|mriez)\w*.{0,20}list|list\w*.{0,20}(mask|mriez)", [ND, K, "Blatníky & mriežky"]),
    (r"ozdobn\w*.{0,15}list|ochrann\w* list|list\w*,? (dver|blatnik|naraznik|prah|bocn|strech|okn)|bocn\w* list|\blist[ay]\b|\blista\b|\blistu\b|klip,? (ozdob|ochran)|lisovan\w* list", [ND, K, "Nárazníky & držiaky"]),
]
_RX = [(re.compile(rx), path) for rx, path in RULES]

# CZ → SK frázy (TecDoc generické názvy). Dlhšie frázy prvé. Bez diakritických variantov sa matchuje case-insensitive.
CZ_SK = [
    ("Sada příslušenství, brzdové čelisti", "Sada príslušenstva, brzdové čeľuste"),
    ("Sada prislusenstvi, brzdove celisti", "Sada príslušenstva, brzdové čeľuste"),
    ("Sada příslušenství, obložení kotoučové brzdy", "Sada príslušenstva, brzdové doštičky"),
    ("Sada příslušenství, brzdový třmen", "Sada príslušenstva, brzdový strmeň"),
    ("Sada montážních dílu čelistí", "Montážna sada brzdových čeľustí"),
    ("Sada montážních dílů čelistí", "Montážna sada brzdových čeľustí"),
    ("Sada vodicích pouzder, brzdový třmen", "Sada vodiacich puzdier, brzdový strmeň"),
    ("Měch, vodítko brzdového třmenu", "Manžeta, vedenie brzdového strmeňa"),
    ("Odvětrávací šroub/ventil, brzdový třmen", "Odvzdušňovacia skrutka/ventil, brzdový strmeň"),
    ("Vodicí čep, brzdový třmen", "Vodiaci čap, brzdový strmeň"),
    ("Nastavení-sada, brzdový třmen", "Opravná sada, brzdový strmeň"),
    ("Opravná sada, brzdový třmen", "Opravná sada, brzdový strmeň"),
    ("Brzdový třmen", "Brzdový strmeň"),
    ("Brzdové destičky sada", "Sada brzdových doštičiek"),
    ("Sada brzdových destiček, kotoučová brzda", "Sada brzdových doštičiek, kotúčová brzda"),
    ("Brzdový kotouč", "Brzdový kotúč"),
    ("Sada brzdových čelistí", "Sada brzdových čeľustí"),
    ("Brzdový buben", "Brzdový bubon"),
    ("Brzdová hadice", "Brzdová hadica"),
    ("Brzdový váleček kola", "Brzdový valček kolesa"),
    ("Hlavní brzdový válec", "Hlavný brzdový valec"),
    ("Výstražný kontakt, opotřebení obložení", "Výstražný kontakt opotrebenia doštičiek"),
    ("Čidlo opotřebení", "Snímač opotrebenia"),
    ("Tažné lanko, parkovací brzda", "Lanko ručnej brzdy"),
    ("Snímač, počet otáček kol", "Snímač otáčok kolesa"),
    ("Snímač, počet otáček", "Snímač otáčok"),
    ("Chladič, chlazení motoru", "Chladič motora"),
    ("Termostat, chladivo", "Termostat chladiacej kvapaliny"),
    ("Hadice chladiče", "Hadica chladiča"),
    ("Vyrovnávací nádrž, chladicí kapalina", "Vyrovnávacia nádržka chladiacej kvapaliny"),
    ("Vodní čerpadlo", "Vodné čerpadlo"),
    ("Ozubený klínový řemen", "Drážkový klinový remeň"),
    ("ozubený klínový řemen", "drážkový klinový remeň"),
    ("Klínový řemen", "Klinový remeň"),
    ("Ozubený řemen", "Ozubený remeň"),
    ("Sada ozubeného řemene", "Sada rozvodového remeňa"),
    ("Napínací kladka, ozubený řemen", "Napínacia kladka rozvodového remeňa"),
    ("Vratná/vodicí kladka, ozubený řemen", "Vratná/vodiaca kladka rozvodového remeňa"),
    ("Napínák, žebrovaný klínový řemen", "Napínač drážkového remeňa"),
    ("Zavěšení motoru", "Uloženie motora"),
    ("Uložení motoru", "Uloženie motora"),
    ("Uložení, řídicí mechanismus", "Uloženie riadiaceho ramena"),
    ("Uložení, řídící mechanismus", "Uloženie riadiaceho ramena"),
    ("Řídicí páka, zavěšení kol", "Rameno zavesenia kolesa"),
    ("Rameno, zavěšení kol", "Rameno zavesenia kolesa"),
    ("Tyč/vzpěra, stabilizátor", "Tyčka stabilizátora"),
    ("Tyč/Vzpěra, stabilizátor", "Tyčka stabilizátora"),
    ("Uložení, stabilizátor", "Uloženie stabilizátora"),
    ("Ložisko, stabilizátor", "Uloženie stabilizátora"),
    ("Axiální kloub, příčné táhlo řízení", "Axiálny kĺb riadenia"),
    ("Hlava příčného táhla řízení", "Koncovka tyče riadenia"),
    ("Příčné táhlo řízení", "Tyč riadenia"),
    ("Sada měchů, řízení", "Sada manžiet riadenia"),
    ("Podpora-/ Kloub", "Guľový čap"),
    ("Podpora-/ kloub", "Guľový čap"),
    ("Tlumič pérování", "Tlmič pruženia"),
    ("Pružina podvozku", "Pružina podvozku"),
    ("Ložisko pružné vzpěry", "Ložisko uloženia tlmiča"),
    ("Ložisko kola", "Ložisko kolesa"),
    ("Sada ložisek kol", "Sada ložísk kolesa"),
    ("Sada ložisek kola", "Sada ložísk kolesa"),
    ("Náboj kola", "Náboj kolesa"),
    ("Kloubová hlava, hnací hřídel", "Kĺb hnacieho hriadeľa"),
    ("Sada kloubů, hnací hřídel", "Sada kĺbov hnacieho hriadeľa"),
    ("Sada měchů, hnací hřídel", "Sada manžiet hnacieho hriadeľa"),
    ("Hnací hřídel", "Hnací hriadeľ"),
    ("Spojková sada", "Spojková sada"),
    ("Setrvačník", "Zotrvačník"),
    ("Vypínací ložisko", "Vysúvacie ložisko"),
    ("Olejový filtr", "Olejový filter"),
    ("Vzduchový filtr", "Vzduchový filter"),
    ("Palivový filtr", "Palivový filter"),
    ("Filtr, vzduch v interiéru", "Kabínový filter"),
    ("Filtr, vzduch v interiéru", "Kabínový filter"),
    ("Zapalovací svíčka", "Zapaľovacia sviečka"),
    ("Žhavicí svíčka", "Žeraviaca sviečka"),
    ("Zapalovací cívka", "Zapaľovacia cievka"),
    ("Lambda sonda", "Lambda sonda"),
    ("Startér", "Štartér"),
    ("Generátor", "Alternátor"),
    ("Snímač, teplota chladicí kapaliny", "Snímač teploty chladiacej kvapaliny"),
    ("Snímač, tlak v sacím potrubí", "Snímač tlaku v sacom potrubí"),
    ("Pneumatická pružina, zavazadlový/nákladní prostor", "Plynová vzpera kufra"),
    ("Plynová vzpěra, kufr/nákladový prostor", "Plynová vzpera kufra"),
    ("Těsnění, hlava válce", "Tesnenie hlavy valcov"),
    ("Těsnění, kryt hlavy válce", "Tesnenie veka ventilov"),
    ("Těsnicí kroužek, vypouštěcí šroub oleje", "Tesniaci krúžok vypúšťacej skrutky"),
    ("Těsnění", "Tesnenie"),
    ("Vstřikovací ventil", "Vstrekovací ventil"),
    ("Turbodmychadlo", "Turbodúchadlo"),
    ("Kompresor, klimatizace", "Kompresor klimatizácie"),
    ("Kondenzátor, klimatizace", "Kondenzátor klimatizácie"),
    ("Výměník tepla, vytápění vnitřního prostoru", "Výmenník tepla kúrenia"),
    ("Spínač, světla zpátečky", "Spínač cúvacích svetiel"),
    ("Zpětné zrcátko", "Spätné zrkadlo"),
    ("Sklo zrcátka", "Sklo spätného zrkadla"),
    ("Lišta stěrače", "Stierač"),
    ("Stírací lišta", "Stierač"),
    ("Hlavní světlomet", "Hlavný svetlomet"),
    ("Koncové světlo", "Zadné svetlo"),
    ("Mlhový světlomet", "Hmlový svetlomet"),
    ("Směrové světlo", "Smerové svetlo"),
    ("Chladič plnicího vzduchu", "Chladič plniaceho vzduchu"),
    ("Ventilátor, chlazení motoru", "Ventilátor chladenia motora"),
    ("Odpor, vnitřní tlakový ventilátor", "Predradný odpor ventilátora kúrenia"),
    ("sada", "sada"),
]
_CZ_SK = [(re.compile(re.escape(a), re.I), b) for a, b in sorted(CZ_SK, key=lambda x: -len(x[0])) if a.lower() != b.lower()]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def classify(name: str) -> Optional[List[str]]:
    n = norm(name)
    for rx, path in _RX:
        if rx.search(n):
            return path
    return None


CZ_SK_WORDS = [('př', 'pr'), ('měnič', 'menič'), ('seřízení', 'nastavenia'), ('střední', 'stredný'), ('talíř', 'tanier'), ('odvzdušnění', 'odvzdušnenia'), ('podběh', 'podbeh'), ('závěs', 'záves'), ('dveřní', 'dverový'), ('stavěcí', 'nastavovací'), ('světlometů', 'svetlometov'), ('předního', 'predného'), ('řetez', 'reťaz'), ('sazových', 'sadzových'), ('otáček', 'otáčok'), ('vypouštěcí', 'vypúšťací'), ('přepínací', 'prepínací'), ('válců', 'valcov'), ('podpěra', 'podpera'), ('větráku', 'ventilátora'), ('volnoběžný', 'voľnobežný'), ('převod', 'prevod'), ('třmen', 'strmeň'), ('třmenu', 'strmeňa'), ('kotoučové', 'kotúčovej'), ('kotoučová', 'kotúčová'), ('doběh', 'dobeh'), ('příslušenství', 'príslušenstvo'), ('těsnicích', 'tesniacich'), ('ohřívače', 'ohrievača'), ('předehřívání', 'predhrievania'), ('předehřívač', 'predhrievač'), ('stěna', 'stena'), ('přichýtka', 'príchytka'), ('příchytka', 'príchytka'), ('příchytek', 'príchytiek'), ('světel', 'svetiel'), ('servořízení', 'posilňovača riadenia'), ('střed', 'stred'), ('zpětného', 'spätného'), ('zpětný', 'spätný'), ('směs', 'zmes'), ('skříňka', 'skrinka'), ('zařízení', 'zariadenie'), ('tělo', 'teleso'), ('příčného', 'priečneho'), ('drátěný', 'drôtený'), ('utěrka', 'utierka'), ('svěrka', 'svorka'), ('svěrných', 'svorných'), ('vstřikovač', 'vstrekovač'), ('sběrné', 'zberné'), ('sběrného', 'zberného'), ('směrové', 'smerové'), ('úchytů', 'úchytov'), ('vnitřního', 'vnútorného'), ('odvětrávání', 'odvetrávania'), ('odvětrávací', 'odvetrávací'), ('napětí', 'napätia'), ('zavěšení', 'zavesenia'), ('světlem', 'svetlom'), ('změny', 'zmeny'), ('plněná', 'plnená'), ('ostřikovací', 'ostrekovací'), ('ostříkovače', 'ostrekovača'), ('vzpěra', 'vzpera'), ('rozvodů', 'rozvodov'), ('blatníků', 'blatníkov'), ('předních', 'predných'), ('před', 'pred'), ('zpětné', 'spätné'), ('věnec', 'veniec'), ('přípojné', 'prípojné'), ('přídržných', 'prídržných'), ('klipsů', 'klipsov'), ('lemů', 'lemov'), ('volnoběžkou', 'voľnobežkou'), ('usměrňovací', 'usmerňovací'), ('roznětnice', 'rozvádzač'), ('předlohový', 'predlohový'), ('pinů', 'pinov'), ('chlazení', 'chladenie'), ('chlazeni', 'chladenie'), ('zapalování', 'zapaľovanie'), ('vstřikování', 'vstrekovania'), ('topení', 'kúrenia'), ('klikové', 'kľukovej'), ('kliková', 'kľuková'), ('klikový', 'kľukový'), ('rozvodového', 'rozvodovej'), ('chladiče', 'chladiča'), ('ojniční', 'ojničné'), ('vačkové', 'vačkového'), ('hřídele vačkové', 'vačkového hriadeľa'), ('motoru', 'motora'), ('hřídele', 'hriadeľa'), ('hřídel', 'hriadeľ'), ('hřídelů', 'hriadeľov'), ('řetězu', 'reťaze'), ('řetězů', 'reťazí'), ('řetěz', 'reťaz'), ('skříně', 'skrine'), ('skříň', 'skriňa'), ('skříní', 'skríň'), ('příruba', 'príruba'), ('řízení', 'riadenia'), ('větrák', 'ventilátor'), ('těsnicí', 'tesniaci'), ('těsnící', 'tesniaci'), ('těsnění', 'tesnenie'), ('zadní', 'zadný'), ('přední', 'predný'), ('horní', 'horný'), ('dolní', 'dolný'), ('levý', 'ľavý'), ('levé', 'ľavé'), ('levá', 'ľavá'), ('světlo', 'svetlo'), ('světla', 'svetlá'), ('světlometu', 'svetlometu'), ('světlomet', 'svetlomet'), ('plynů', 'plynov'), ('řídicí', 'riadiaca'), ('řídící', 'riadiaca'), ('impulsů', 'impulzov'), ('mřížka', 'mriežka'), ('řemenů', 'remeňov'), ('řemene', 'remeňa'), ('řemenice', 'remenica'), ('řemen', 'remeň'), ('vodní', 'vodné'), ('dveře', 'dvere'), ('dveří', 'dverí'), ('tlumič', 'tlmič'), ('tlumiče', 'tlmiče'), ('tlumičů', 'tlmičov'), ('vnější', 'vonkajší'), ('vnitřní', 'vnútorný'), ('stieračů', 'stieračov'), ('kotouče', 'kotúče'), ('kotouč', 'kotúč'), ('odvětrání', 'odvetranie'), ('osvětlení', 'osvetlenie'), ('předstihová', 'predstihová'), ('kabelů', 'káblov'), ('větrací', 'vetrací'), ('kroužků', 'krúžkov'), ('kroužek', 'krúžok'), ('čistění', 'čistenia'), ('čištění', 'čistenia'), ('vytápění', 'kúrenia'), ('výměník', 'výmenník'), ('klínových', 'klinových'), ('klínový', 'klinový'), ('měch', 'mech'), ('dřík', 'driek'), ('přítlačný', 'prítlačný'), ('přídavné', 'prídavné'), ('měrka', 'mierka'), ('trychtýř', 'lievik'), ('nosičů', 'nosičov'), ('uzávěr', 'uzáver'), ('ostřikovače', 'ostrekovača'), ('ostřikovačů', 'ostrekovačov'), ('šroubů', 'skrutiek'), ('šroub', 'skrutka'), ('odměrka', 'odmerka'), ('vyhřívané', 'vyhrievané'), ('přívod', 'prívod'), ('převodovka', 'prevodovka'), ('převodovky', 'prevodovky'), ('svíčka', 'sviečka'), ('svíčky', 'sviečky'), ('plnění', 'plnenia'), ('kola', 'kolesa'), ('filtr', 'filter'), ('čidlo', 'snímač'), ('větrání', 'vetrania'), ('držák', 'držiak'), ('ventilů', 'ventilov'), ('víko', 'veko'), ('víčko', 'viečko'), ('pístní', 'piestne'), ('píst', 'piest'), ('válce', 'valca'), ('válec', 'valec'), ('hlavní', 'hlavný'), ('zapalovací', 'zapaľovacia'), ('žhavicí', 'žeraviaca'), ('částic', 'častíc'), ('sazový', 'sadzový'), ('sazí', 'sadzí'), ('destičky', 'doštičky')]
_CZ_SK_W = [(re.compile(r"(?<!\w)" + re.escape(a) + r"(?!\w)", re.I), b) for a, b in CZ_SK_WORDS]


def _case(m, sk):
    w = m.group(0)
    return sk[:1].upper() + sk[1:] if w[:1].isupper() else sk


def to_sk(name: str) -> str:
    out = name
    for rx, sk in _CZ_SK:
        out = rx.sub(sk, out)
    for rx, sk in _CZ_SK_W:  # jednotlivé slová (hranice slova), zachová veľké písmeno
        out = rx.sub(lambda m, sk=sk: _case(m, sk), out)
    return out


def _wc():
    url = (os.environ.get("WC_URL") or "").rstrip("/")
    ck = os.environ.get("WC_CONSUMER_KEY") or ""
    cs = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not (url and ck and cs):
        raise SystemExit("Chýba WC_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET")
    return url, (ck, cs)


def category_index(session, url, auth, create: bool = False) -> Dict[Tuple[str, ...], int]:
    """Cesta (názvy od koreňa) → term id, len existujúce kategórie."""
    rows, page = [], 1
    while True:
        r = session.get(f"{url}/wp-json/wc/v3/products/categories", auth=auth, timeout=60,
                        params={"per_page": 100, "page": page, "_fields": "id,name,parent"})
        r.raise_for_status()
        batch = r.json() or []
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    by_id = {c["id"]: c for c in rows}

    def path(c):
        out, seen = [], set()
        while c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c["name"].replace("&amp;", "&").strip())
            c = by_id.get(c.get("parent") or 0)
        return tuple(reversed(out))

    idx = {path(c): c["id"] for c in rows}
    if create:
        for want in APPROVED_NEW_CATS:
            parent = idx.get(tuple(want[:-1]))
            if want in idx or not parent:
                continue
            r = session.post(f"{url}/wp-json/wc/v3/products/categories", auth=auth, timeout=60,
                             json={"name": want[-1], "parent": parent})
            if r.ok:
                idx[want] = r.json()["id"]
                LOG.info("vytvorená schválená kategória %s (id %s)", " > ".join(want), idx[want])
            else:
                LOG.warning("kategóriu %s sa nepodarilo vytvoriť: HTTP %s", want[-1], r.status_code)
    return idx


def iter_published_dump(url, auth, session):
    """Zverejnené produkty v generických kategóriách (Auto chémia, Nezaradené…)."""
    from promote_drafts import GENERIC_CATS
    from shop_guard import wait_if_slow

    r = session.get(f"{url}/wp-json/wc/v3/products/categories", auth=auth, params={"per_page": 100, "search": ""}, timeout=120)
    ids = []
    page = 1
    while True:
        r = session.get(f"{url}/wp-json/wc/v3/products/categories", auth=auth,
                        params={"per_page": 100, "page": page, "_fields": "id,name"}, timeout=120)
        rows = r.json() if r.ok else []
        ids += [c["id"] for c in rows if (c.get("name") or "").replace("&amp;", "&").strip().lower() in GENERIC_CATS]
        if len(rows) < 100:
            break
        page += 1
    for cid in ids:
        page = 1
        while True:
            wait_if_slow()
            r = session.get(f"{url}/wp-json/wc/v3/products", auth=auth, timeout=120,
                            params={"status": "publish", "category": cid, "per_page": 100, "page": page,
                                    "_fields": "id,name,sku,status,price,regular_price,categories,stock_status,meta_data"})
            rows = r.json() if r.ok else []
            for p in rows:
                yield p
            if len(rows) < 100:
                break
            page += 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max", type=int, default=0, help="max zaradených za beh (0 = bez limitu)")
    ap.add_argument("--from-file", default="", help="JSONL konceptov (offline)")
    ap.add_argument("--out", default="")
    ap.add_argument("--published", action="store_true",
                    help="zverejnené produkty v dump kategóriách (Auto chémia/Nezaradené) – len kategória, názov sa nemení")
    a = ap.parse_args()
    apply = a.apply and not a.dry_run

    from promote_drafts import GENERIC_CATS, evaluate, is_czech, iter_drafts, sk_name
    import requests
    import shop_guard  # noqa: F401
    from shop_guard import wait_if_slow

    session = requests.Session()
    cat_ids: Dict[Tuple[str, ...], int] = {}
    if a.from_file:
        drafts = (json.loads(l) for l in open(a.from_file, encoding="utf-8") if l.strip())
        url = auth = None
    else:
        url, auth = _wc()
        cat_ids = category_index(session, url, auth, create=apply)
        drafts = iter_published_dump(url, auth, session) if a.published else iter_drafts(url, auth, session, 0.5, {"published": 0})

    st = Counter()
    by_path: Counter = Counter()
    by_src: Counter = Counter()
    pending: List[dict] = []

    def flush():
        if not pending:
            return
        wait_if_slow()
        try:
            r = session.post(f"{url}/wp-json/wc/v3/products/batch", auth=auth, json={"update": pending}, timeout=180)
            if r.ok:
                st["written"] += len(pending)
            else:
                st["write_failed"] += len(pending)
        except Exception:
            st["write_failed"] += len(pending)
        pending.clear()
        time.sleep(1)

    for p in drafts:
        st["drafts"] += 1
        cats = [(c.get("name") or "").replace("&amp;", "&").strip() for c in (p.get("categories") or [])]
        specific = [c for c in cats if c.lower() not in GENERIC_CATS and not c.lower().startswith("[orphan]")]
        meta = {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}
        src = str(meta.get("_supplier_source") or "?").lower()
        name = (p.get("name") or "").strip()
        upd: Dict = {}
        # SK názov (zverejnené názvy nemeníme)
        if is_czech(name) and not a.published:
            fixed = to_sk(sk_name(name))
            if fixed != name and not is_czech(fixed):
                upd["name"] = fixed
                st["sk_renamed"] += 1
            else:
                st["czech_left"] += 1
        if not specific:
            st["no_category"] += 1
            path = classify(upd.get("name") or name) or classify(name)
            if path:
                tid = cat_ids.get(tuple(path)) if cat_ids else -1
                if tid:
                    upd["categories"] = [{"id": tid}] if tid > 0 else []
                    st["classified"] += 1
                    by_path[" > ".join(path[1:])] += 1
                    by_src[src] += 1
                    cats = [path[-1]]
                else:
                    st["path_missing_in_shop"] += 1
            else:
                st["no_rule"] += 1
        if upd:
            # Odhad: prejde po zaradení pravidlami promote_drafts?
            q = dict(p)
            if "name" in upd:
                q["name"] = upd["name"]
            if "categories" in upd:
                q["categories"] = [{"name": c} for c in cats]
            reasons, _ = evaluate(q)
            if not reasons:
                st["would_promote"] += 1
            if apply and url:
                pending.append({"id": p["id"], **upd})
                if len(pending) >= 50:
                    flush()
        if a.max and st["classified"] >= a.max:
            break
    if apply and url:
        flush()
    res = {"applied": apply, **st, "classified_by_supplier": dict(by_src), "top_paths": dict(by_path.most_common(25))}
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SG_RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
