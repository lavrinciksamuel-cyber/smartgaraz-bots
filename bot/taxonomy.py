#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ŽELEZNÁ TAXONÓMIA SmartGaráž — jediný zdroj pravdy pre AI + WooCommerce sync.

Dve vrstvy (NEZAMIEŇAŤ):
1) KATEGÓRIE = čo to JE (olej / rameno nápravy / momentový kľúč)
2) FITMENT = na čo to SEDÍ (VW / Golf / 1.9 TDI / 2004) — pozri fitment.py

Pravidlá kategórií:
- Max 3 úrovne (Hlavná → Skupina → Typ)
- Žiadne značky áut / modelov v kategóriách (to je filter)
- Zákazník: číslo dielu ALEBO typ dielu → 2–3 kliky
- Vlastný logický strom (shop UX). LKQ / Szekop = inšpirácia pokrytia
  dielov pri importe, NIE kópia ich stromu / TecDoc ID.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Povolený strom (EXKLUZÍVNE) — škáluje na státisíce dielov + náradie
# ---------------------------------------------------------------------------
CATEGORY_TREE: Dict[str, List[str]] = {
    "Motorové oleje & Kvapaliny": [
        "Motorové oleje",
        "Prevodové oleje",
        "Brzdové kvapaliny",
        "Chladiace kvapaliny",
        "Posilňovač riadenia",
    ],
    "Autochémia & Aditíva": [
        "Aditíva do nafty & benzínu",
        "AdBlue & SCR",
        "Čističe palivového systému",
        "Čističe DPF / EGR",
        "Ochrana motora",
        "Starostlivosť o olejový systém",
        "Starostlivosť o chladiaci systém",
        "Tesnenie únikov",
        "Výplachy motora",
        "Výplachy prevodovky",
        "Klimatizácia",
    ],
    "Autokozmetika & Detail": [
        "Exteriér & umývanie",
        "Interiér",
        "Disky & pneumatiky",
        "Leštenie & vosky",
    ],
    "Dielenská chémia & Údržba": [
        "Čističe bŕzd & Odmasťovače",
        "Mazivá & Pasty",
        "Tesnenia & Lepidlá",
        "Odhrdzovače & Uvoľňovače",
        "Aerosóly & Spreje",
    ],
    # Peer Dielenskej — PU peny, hydroizol, stavebné lepidlá (nie autodielňa).
    "Stavebná chémia": [
        "PU peny & montáž",
        "Hydroizolácia & pásky",
        "Stavebné lepidlá & tmely",
    ],
    # --- AUTODIELY: typ dielu pre zákazníka (2–3 kliky). Fitment = filter, nie kategória.
    # Poradie L2 = typický nákupný tok (filtre/brzdy/žiarovky pred „hlbokým“ motorom).
    # LKQ/Szekop = inšpirácia pokrytia, NIE zrkadlo ich stromu.
    "Náhradné diely": [
        "Filtre",
        "Brzdy",
        "Svetlá & žiarovky",
        "Motor & sanie",
        "Chladenie & kúrenie",
        "Náprava & podvozok",
        "Riadenie",
        "Spojka & prevodovka",
        "Výfuk & DPF diely",
        "Elektro & senzory",
        "Karoséria & svetlá",
    ],
    # --- NÁRADIE: podľa typu práce ---
    "Náradie & Vybavenie": [
        "Diagnostika & meranie",
        "Kľúče & račne",
        "Momentové náradie",
        "Brzdy & podvozok – náradie",
        "Motor & olej – náradie",
        "Karoséria & čalúnenie – náradie",
        "Pneumatiky & disky – náradie",
        "Dielenské vybavenie",
        # Inšpekčné visačky, servisné nálepky, označenie dielne (Würth…)
        "Etikety & označenie",
        # PRO-TEC servisné adaptéry/hadice/kufríky ≠ náhradný diel na auto
        "Servisné adaptéry & Kity",
    ],
    "Výhodné sady & Akcie": [
        "Servisné sety",
        "Produkty v akcii",
    ],
}

# Pod-typy 3. úrovne (voliteľné listové kategórie)
# Formát: (parent_subcategory_name, [leaf, …])
PART_LEAVES: Dict[str, List[str]] = {
    "Motorové oleje": [
        "0W-8",
        "0W-16",
        "0W-20",
        "0W-30",
        "0W-40",
        "5W-20",
        "5W-30",
        "5W-40",
        "10W-30",
        "10W-40",
        "10W-60",
        "15W-40",
        "Ostatné viskozity",
    ],
    "Náprava & podvozok": [
        "Ramena & čapy",
        "Tlmiče & pružiny",
        "Silentbloky",
        "Ložiská kolies",
        "Stabilizátory",
        "Guľové čapy & spoje",
        "Spojové tyče",  # dial / track rod — SK názov pre shop
    ],
    "Brzdy": [
        "Brzdové doštičky",
        "Brzdové kotúče",
        "Bubnové brzdy & čeluste",
        "Brzdové hadice & valčeky",
        "Brzdové strmene",
        "ABS senzory & čidlá",
        "Ručná brzda & laná",
    ],
    "Filtre": [
        "Olejové filtre",
        "Vzduchové filtre",
        "Kabínové filtre",
        "Palivové filtre",
        "Filter sušiča klimatizácie",
        "Hydraulické / prevodové filtre",
    ],
    "Motor & sanie": [
        "Tesnenia motora & sania",
        "Rozvody & remene",
        "Klinové remene",
        "Turbá & sanie",
        "Palivové čerpadlá & vstreky",
        "Vodné pumpy",
        "Oleové pumpy & vypúšťacie skrutky",
        "Uloženie motora",
        "Sviečky & zapaľovanie",
        "EGR / škrtiaca klapka",
    ],
    "Chladenie & kúrenie": [
        "Chladiče motora",
        "Termostaty",
        "Ventilátory & spojky",
        "Hadice chladenia",
        "Expanzné nádoby",
        "Kúrenie & výparníky",
        "Diely klimatizácie",
    ],
    "Riadenie": [
        "Čerpadlá posilňovača",
        "Riadiace tyče & čapy",
        "Manžety riadenia",
        "Hrebene riadenia",
    ],
    "Spojka & prevodovka": [
        "Spojkové sady",
        "Prítlačné taniere & kotúče",
        "Vysúvacie ložiská",
        "Zotrvačníky",
        "Manžety poloosí",
        "Poloosi & kĺby",
    ],
    "Výfuk & DPF diely": [
        "Katalyzátory",
        "DPF / FAP filtre",
        "Lambda sondy",
        "Tlmiče výfuku",
        "Výfukové tesnenia & pružiny",
    ],
    "Elektro & senzory": [
        "Lambda & NOx senzory",
        "ABS / otáčkové senzory",
        "Teplotné & tlakové čidlá",
        "Štartéry & alternátory",
        "Autobatérie",
        "Relé & poistky",
        "Zapaľovacie cievky",
        "Riadiace jednotky",
        "Káble & konektory",
        # Wave 5 — imobil / keyless / spínacie skrinky (≠ náradie „kľúč“)
        # NEMAZAŤ pri iných taxonomy fixoch — merge, neprepisuj celý súbor.
        "Imobilizéry & centrálne zamykanie",
        "Štartovanie bez kľúča & spínacie skrinky",
    ],
    "Karoséria & svetlá": [
        "Odhlučnenie",
        "Spätné zrkadlá",
        "Stierače",
        "Nárazníky & držiaky",
        "Blatníky & mriežky",
        "Svetlomety & svetlá",
        "Autosklá",
        "Sťahovače okien",
        "Plynové vzpery",
        "Interiér & obloženie",
        "Zámky & kľúče",  # Wave 5 — NEMAZAŤ
    ],
    # Typ žiarovky = leaf (H4/H7…), rovnaký pattern ako Motorové oleje → 5W-30
    "Svetlá & žiarovky": [
        "H1",
        "H3",
        "H4",
        "H7",
        "H11",
        "HB3",
        "HB4",
        "W5W",
        "P21W",
        "LED žiarovky",
        "Xenón / výbojky",
        "Ostatné žiarovky",
    ],
}

# Starý chaos / legacy názvy → nový strom
LEGACY_CATEGORY_MAP: Dict[str, List[str]] = {
    "uncategorized": ["Náhradné diely", "Filtre"],
    "nezaradené": ["Náhradné diely", "Filtre"],
    "auto elektro": ["Náhradné diely", "Elektro & senzory"],
    "žiarovky a osvetlenie": ["Náhradné diely", "Svetlá & žiarovky"],
    "svetlá & žiarovky": ["Náhradné diely", "Svetlá & žiarovky"],
    "svetla & ziarovky": ["Náhradné diely", "Svetlá & žiarovky"],
    "svetlá a žiarovky": ["Náhradné diely", "Svetlá & žiarovky"],
    "autokozmetika a čističe": ["Autokozmetika & Detail", "Exteriér & umývanie"],
    "brzdy a montážne pasty": ["Dielenská chémia & Údržba", "Mazivá & Pasty"],
    "chladiaci systém": ["Motorové oleje & Kvapaliny", "Chladiace kvapaliny"],
    "mazivá a aerosóly": ["Dielenská chémia & Údržba", "Aerosóly & Spreje"],
    "mopar & pennzoil": ["Motorové oleje & Kvapaliny", "Motorové oleje"],
    "posilňovač riadenia a manuálna prevodovka": ["Motorové oleje & Kvapaliny", "Posilňovač riadenia"],
    "prevodovky a diferenciály": ["Motorové oleje & Kvapaliny", "Prevodové oleje"],
    "produkty v akcii": ["Výhodné sady & Akcie", "Produkty v akcii"],
    "zvýhodnené sady": ["Výhodné sady & Akcie", "Servisné sety"],
    "výhodné sady": ["Výhodné sady & Akcie", "Servisné sety"],
    # „Auto chémia“ = dump bucket (nie skutočná L1) — NEMAPUJ na Aditíva.
    "dielenská chémia": ["Dielenská chémia & Údržba", "Čističe bŕzd & Odmasťovače"],
    "dielenska chemia": ["Dielenská chémia & Údržba", "Čističe bŕzd & Odmasťovače"],
    "olejový systém": ["Autochémia & Aditíva", "Ochrana motora"],
    "palivový systém": ["Autochémia & Aditíva", "Čističe palivového systému"],
    # Premenované listy (shop UX) — staré Woo termy
    "tie rod / spojovacie tyče": ["Náhradné diely", "Náprava & podvozok", "Spojové tyče"],
    "klinové / poly-v remene": ["Náhradné diely", "Motor & sanie", "Klinové remene"],
    "klimatizácia (kompresor & kondenzátor)": ["Náhradné diely", "Chladenie & kúrenie", "Diely klimatizácie"],
    "riadiace jednotky & ecu": ["Náhradné diely", "Elektro & senzory", "Riadiace jednotky"],
    "káblové zväzky & konektory": ["Náhradné diely", "Elektro & senzory", "Káble & konektory"],
    "diesel": ["Autochémia & Aditíva", "Aditíva do nafty & benzínu"],
    "benzín": ["Autochémia & Aditíva", "Aditíva do nafty & benzínu"],
    # starý 2-úrovňový názov
    "náhradné diely & príslušenstvo": ["Náhradné diely", "Filtre"],
    "tesnenia motora & sania": ["Náhradné diely", "Motor & sanie", "Tesnenia motora & sania"],
    "chladenie": ["Náhradné diely", "Chladenie & kúrenie"],
    "filtre": ["Náhradné diely", "Filtre"],
    "servisné adaptéry & kity": ["Náradie & Vybavenie", "Servisné adaptéry & Kity"],
}

# Keyword rules — PORADIE = priorita (prvý match vyhrá)
CATEGORY_KEYWORD_RULES: List[Tuple[List[str], List[str]]] = [
    # Sady
    # POZOR: „kit v“ / „kit i“ ako substring falšne trafí „kit Valeo/kit IV…“ —
    # preto len celé frázy so záverom (medzera/koniec), nie holé „kit v“.
    (["set na ", "set pre ", "sada na ", "sada pre ", "servisný set", "servisny set",
      "adapter kit", "servisný kit", "servisny kit", "combo set", "balíček", "balicek",
      "kit i ", "kit ii ", "kit iii ", "kit iv ", "kit v ", "kit vi ", "kit vii ",
      " kit i", " kit ii", " kit iii", " kit iv", " kit vi", " kit vii"],
     ["Výhodné sady & Akcie", "Servisné sety"]),
    # Poistky PRED „diagnostika“/„kufrík“ (skloňovanie: poistiek; sady v kufríku)
    (["poistka", "poistky", "poistiek", "flat fuse", "mini fuse", "maxi fuse", "ato fuse",
      "držiak poistky", "drziak poistky", "zásuvných poist", "zasuvnych poist",
      "resetovateľnej poist", "resetovatelnej poist"],
     ["Náhradné diely", "Elektro & senzory", "Relé & poistky"]),
    # Náradie (pred dielmi — „kľúč“ vs „kľúčový diel“)
    (["momentový", "momentovy", "torque wrench", "torque"], ["Náradie & Vybavenie", "Momentové náradie"]),
    (["diagnost", "obd-ii", "obd2", "multimeter", "diagnostika",
      "krížový laser", "krizovy laser", "cross laser", "laser sg", "laser cg",
      "endoskop", "endoscope", "boroskop", "boroscope",
      "testovacia kvapalina", "testovacia kapalina", "testovacia tekutina",
      "co 2 test", "co2 test", "testovanie úniku co", "testovanie uniku co",
      # Skúšobné boxy / testery prívesov (Würth Combi) ≠ Auto chémia
      "skúšobný box", "skusobny box", "skúšobná sada", "skusobna sada",
      "test box", "testing box", "trailer tester", "tester príves", "tester prives",
      "kontrola konektorov príves", "kontrola konektorov prives"],
     ["Náradie & Vybavenie", "Diagnostika & meranie"]),
    (["račne", "racne", "gola sada", "kľúčová sada", "klucova sada",
      "skrutkovač", "skrutkovac",
      "nástrčná hlavica", "nastrčna hlavica", "nástrcna hlavica", "socket bit", "socket ",
      "nástroje klann", "nastroje klann", "klann "],
     ["Náradie & Vybavenie", "Kľúče & račne"]),
    # Nie holé „pružin“ — matchuje aj „pružina“ (tlmič/airbag clock-spring).
    (["stahovák pružín", "stahovak pruzin", "spring compressor", "brzdový piest", "brzdovy piest", "piston wind back",
      "kliešte na ohýbanie", "klieste na ohybanie", "brzdových potrub", "brzdovych potrub",
      "odvzdušňovač bŕzd", "odvzdušňovače bŕzd", "odvzdusnovac brzd", "odvzdusnovace brzd",
      "brake pipe", "brake bleeder",
      "sada sťahovákov", "sada stahovakov", "sťahovák", "stahovak"],
     ["Náradie & Vybavenie", "Brzdy & podvozok – náradie"]),
    (["olejový kľúč", "olejovy kluc", "filter wrench", "oil filter tool",
      "kľúč na olejový filter", "kluc na olejovy filter",
      "kľúč na olejové filtre", "kluc na olejove filtre",
      "kľúč na olejovy filter", "oil filter wrench", "oil filter key",
      # „Kľúč na kryt olejového filtra“ ≠ hard-part kryt/puzdro
      "kľúč na kryt olej", "kluc na kryt olej", "kľúč na puzdro olej", "kluc na puzdro olej"],
     ["Náradie & Vybavenie", "Motor & olej – náradie"]),
    # Inšpekčné visačky / servisné nálepky — PRED generickým náradím aj chémiou
    # (inak Würth cheap import padne do starej „Auto chémia“ / nič).
    (["inšpekčná visačka", "inspekcna visacka", "inspektion", "inspection tag",
      "inspection label", "nálepka zákazníckeho", "nalepka zakaznickeho",
      "servisná nálepka", "servisna nalepka", "označenie servisu",
      "oznacenie servisu", "kunden-service", "kunden service",
      "identifikačná nálepka", "identifikacna nalepka", "nálepka na pneumatik",
      "nalepka na pneumatik", "nálepka na koles", "nalepka na koles",
      "označenie obrys", "oznacenie obrys", "contour mark", "reflective tape",
      "termopapier", "thermo papier", "tachograf", "tachograph", "digitalský tachograf",
      "digitálne tachograf", "digitalne tachograf"],
     ["Náradie & Vybavenie", "Etikety & označenie"]),
    # Mazacie armatúry / kužeľové krúžky / potrubné spojky (centrálne mazanie)
    (["kužeľový krúžok", "kuzelovy kruzok", "dvojitý kužeľ", "dvojity kuzel",
      "cone ring", "potrubná spojka", "potrubna spojka", "pipe coupling",
      "centrálne mazacie", "centralne mazacie", "mazacie zariaden",
      "spojovací diel", "spojovaci diel", "prevlečná skrut", "prevlecna skrut"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # Generické dielenské (karoséria/laser/skrutkovač majú vlastné pravidlá)
    (["zdvihák", "zdvihak", "hever", "dielenský stôl",
      "vzduchový kompresor", "vzduchovy kompresor", "air compressor",
      "dielenský kompresor", "dielensky kompresor", "kompresor 230", "kompresor 400v",
      "pracovný stôl", "pracovny stol", "kladivo", "vrták", "vrtak",
      "čepeľ", "cepel", "pištoľ na kartuše", "pistol na kartuse",
      "mikrokefka", "elektróda", "elektroda",
      "dávkovač lepidla", "davkovac lepidla", "hadicová sonda", "hadicova sonda",
      "pvc hadica", "dávkovaciu striekačku", "davkovaciu striekacku",
      "ručnú dávkovaciu", "rucnu davkovaciu", "držiak pištole", "drziak pistole",
      "kolienko pre", "bezpečnostný kábel", "bezpecnostny kabel",
      "striekacia hlavica", "striekacia dýza", "striekacia dyza",
      "hlavica priamej", "hlavica priamej trysky", "stupňovitý vrták", "stupnovity vrtak",
      "door gap", "gripper", "refillo",
      # Mazacie LISY a príslušenstvo = náradie (NIE „Mazivá & Pasty“ / chémia)
      "mazací lis", "mazaci lis", "pákový mazací", "pakovy mazaci",
      "prívodný diel", "privodny diel", "kovový prívod", "kovovy privod",
      "dvojpáková pištoľ na mazivo", "dvojpakova pistol na mazivo",
      "pištoľ na mazivo", "pistol na mazivo", "grease gun",
      "chránič podlahy", "chranic podlahy", "floor protector",
      "dávkovač na tmel", "davkovac na tmel", "vaku 30",
      "pomocné štartovacie", "pomocne startovacie", "štartovacie zariadenie",
      "startovacie zariadenie", "jump starter",
      "náhradná hadica", "nahradna hadica", "hydraulické ručné", "hydraulicke rucne"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # Prívesové elektro adaptéry/zásuvky PRED generickým „adaptér“ (náradie)
    (["pólový", "polovy", "pólová", "polova", "kolíkový", "kolikovy", "kolíková", "kolikova",
      "13-pól", "13-pol", "7-pól", "7-pol", "15-pól", "15-pol", "15-kolík", "15-kolik",
      "zásuvka abs", "zasuvka abs", "zásuvka 15", "zasuvka 15", "zásuvka 7", "zasuvka 7",
      "opravná zásuvka", "opravna zasuvka", "krátky adaptér", "kratky adapter",
      "špirálový kábel", "spiralovy kabel"],
     ["Náhradné diely", "Elektro & senzory"]),
    # PRO-TEC/X-Chemie servisné adaptéry, hadice, kufríky = náradie (nie diel)
    (["adaptér", "adapter", "adaptérový", "adapterovy", "adaptérová",
      "hadica orion", "orion super", "orion hd", "hlavica mercury",
      "kufrík", "kufrik", "lpg system", "multi adaptér", "multi adapter",
      "servisné adaptéry", "servisne adaptery"],
     ["Náradie & Vybavenie", "Servisné adaptéry & Kity"]),
    # Dielňové STROJE / zariadenia (PRED ATF/prevodovkou — inak
    # „Zariadenie na … prevodovky ATF“ spadne do Prevodové oleje;
    # zachytené: PRO-TEC ATF Evolution NextGen #1339, XCHEMIE Xcleaner #1382).
    (["zariadenie na výmenu", "zariadenie na preplach", "zariadenie na čistenie",
      "zariadenie na cistenie", "flush machine", "exchange unit", "exchange machine",
      "service unit", "atf evolution", "atf nextgen", "evolution nextgen",
      "injector cleaning machine", "cleaning station"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # --- FILTRE (PRED olejmi/chémiou/kozmetikou — inak prevodov/interiér/palivov ukradnú match) ---
    (["hydraulický filter", "hydraulicky filter", "hydraulic filter",
      "hydraulický filtr", "hydraulicky filtr",  # CZ „filtr“
      "filter hydraulický", "filter hydraulicky", "filtr hydraulický", "filtr hydraulicky",
      "transmission filter", "filter automatickej prevodovky", "filter prevodovky",
      "filtr automatickej prevodovky", "filtr prevodovky",
      "atf filter", "prevodový filter", "prevodovy filter", "prevodový filtr", "prevodovy filtr",
      "gearbox filter", "oil strainer", "sitko oleja prevod", "filter do automatickej",
      "filtr do automatickej",
      # B2B skratky: „FILTR VW AUTOMAT. PREVOD“
      "automat. prevod", "filtr automat", "filter automat"],
     ["Náhradné diely", "Filtre", "Hydraulické / prevodové filtre"]),
    (["cabin air filter", "cabin filter", "cabin air",
      "kabínový filter", "kabinovy filter", "kabínový", "kabinovy", "kabínov", "kabinov",
      "filter vnútorného priestoru", "filter vnutorneho priestoru",
      "vnútorného priestoru", "vnutorneho priestoru",
      "vzduch v interiéri", "vzduch v interieri", "filter, vzduch v interiéri",
      "peľový filter", "pelovy filter", "pollen filter", "pollen",
      "filtre d'habitacle", "habitacle"],
     ["Náhradné diely", "Filtre", "Kabínové filtre"]),
    (["oilfilter", "oil filter", "olejový filter", "olejovy filter", "olejov filter",
      "olejový filtr", "olejovy filtr",  # CZ „filtr“
      "filter olejový", "filter olejovy", "filtr olejový", "filtr olejovy",
      "filter oleja", "filtr oleja",
      "filter olejový", "filter olejovy", "filtr olejový", "filtr olejovy",
      "puzdro olejového filtra", "puzdro olejoveho filtra",
      "kryt olejového filtra", "kryt olejoveho filtra",
      "nadstavec filtra", "nadstavec olejového", "nadstavec olejoveho",
      "oil filter housing", "oil filter cover"],
     ["Náhradné diely", "Filtre", "Olejové filtre"]),
    (["fuel filter", "palivový filter", "palivovy filter", "diesel filter"],
     ["Náhradné diely", "Filtre", "Palivové filtre"]),
    # cabin už vyššie — „air filter“ nesmie ukradnúť cabin air filter
    # CZ/SK slovosled: „Filter vzduchový“ (LKQ) aj „Vzduchový filter“
    (["air filter", "vzduchový filter", "vzduchovy filter", "luftfilter",
      "filter vzduchový", "filter vzduchovy", "filtr vzduchový", "filtr vzduchovy"],
     ["Náhradné diely", "Filtre", "Vzduchové filtre"]),
    (["filter sušiča", "filter susica", "receiver drier", "receiver dryer", "ac drier",
      "sušič klimatizácie", "susic klimatizacie"],
     ["Náhradné diely", "Filtre", "Filter sušiča klimatizácie"]),
    # Poistky PRED sadami/náradím („súprava … poistky“).
    (["poistka", "poistky", "fuse box", "blade fuse", "ato fuse", "mini fuse", "resetovateľnej poistky", "resetovatelnej poistky"],
     ["Náhradné diely", "Elektro & senzory", "Relé & poistky"]),
    # Stierače PRED kozmetikou „skla/okná“ (inak Würth stierač → Exteriér umývanie).
    (["stierač", "stierac", "wiper blade", "wiper ", "stěrač", "sterac", "stierače",
      "aerotwin", "silencio", "twin truck", "flat blade"],
     ["Náhradné diely", "Karoséria & svetlá", "Stierače"]),
    # Oleje & kvapaliny
    (["brake fluid", "brzdová kvapalina", "brzdova kvapalina", "dot 3", "dot 4", "dot 5.1", "dot 5"], ["Motorové oleje & Kvapaliny", "Brzdové kvapaliny"]),
    # Aditíva/ošetrenie chladiaceho systému (flush/conditioner/stop-leak/emulzifikátor) ≠ samotná chladiaca kvapalina
    (["radiator flush", "radiator conditioner", "radiator oil cleaner", "radiator oil emulsifier", "radiator stop", "rad charger", "cooling system flush", "cooling system conditioner",
      "čistič olejového chladiča", "cistic olejoveho chladica", "čistič olejových kanálov chladiča", "cistic olejovych kanalov chladica",
      "kondicionér chladiča", "kondicioner chladica", "emulgátor olejov do chladiča", "emulgator olejov do chladica",
      "olej do chladiča", "olej z chladiča", "olejové usadeniny z chladiča", "olejové nečistoty z chladiaceho",
      "čistič radiátora a oleja", "cistic radiatora a oleja",
      "výplach chladiča", "vyplach chladica", "výplach chladiace", "vyplach chladiace",
      "prísada proti úniku chladiacej", "prisada proti uniku chladiacej"],
     ["Autochémia & Aditíva", "Starostlivosť o chladiaci systém"]),
    (["coolant", "antifreeze", "chladiac", "g11", "g12", "g13"], ["Motorové oleje & Kvapaliny", "Chladiace kvapaliny"]),
    # Oil booster / ochrana oleja PRED „posilňovač riadenia“ (SK „posilňovač … oleja“ ≠ PSF)
    (["posilňovač motorového oleja", "posilnovac motoroveho oleja", "posilňovač oleja", "posilnovac oleja",
      "oil booster", "oil enhancer", "engine oil enhancer", "oil system care", "oil system guard"],
     ["Autochémia & Aditíva", "Starostlivosť o olejový systém"]),
    # Reťazová / záhradná píla ≠ PSF (zachytené cheap: STARLINE GARDEN CHAINSAW → Posilňovač)
    (["chainsaw", "chain saw", "reťazová píla", "retazova pila", "garden oil", "bar oil",
      "pilový olej", "pilovy olej", "olej na reťaz", "olej na retaz",
      # len OLEJ 2T — nie „čistič … 2-taktných motorov“
      "2t olej", "olej 2t", "2-t ", "2t ", "2-taktný olej", "2-taktny olej",
      "dvojtaktný olej", "dvojtaktny olej", "two stroke oil", "2-stroke oil",
      "garden 2t", "2t syntet", "2t mineral"],
     ["Motorové oleje & Kvapaliny", "Motorové oleje", "Ostatné viskozity"]),
    (["power steering", "posilňovač riadenia", "posilnovac riadenia", "posilňovača riadenia",
      "psf", "chf 11", "hydraulic control", "hydraulic fluid", "steering fluid", "electric steering",
      "hydraulický olej", "hydraulicky olej", "hydraulic oil", "iso vg",
      "hydraulická kontrol", "hydraulicka kontrol", "kontrolná kvapalina", "kontrolna kvapalina"],
     ["Motorové oleje & Kvapaliny", "Posilňovač riadenia"]),
    # Čističe/kondicionéry PREVODKY — len chémia (flush/čistič), NIE hard-part filtre.
    # Bare „automatickej prevodovky“ kradlo FEBI hydraulický filter → Výplachy/oleje.
    (["transmission flush", "transmission conditioner",
      "prevodovky flush", "gearbox flush", "gearbox conditioner", "atf flush", "atf conditioner",
      "čistič automatickej", "cistic automatickej", "kondicionér do automatickej",
      "kondicioner do automatickej", "kondicionér automatickej", "kondicioner automatickej",
      "automatic transmission flush", "auto transmission flush",
      "automatic transmission conditioner", "auto transmission conditioner",
      "výplach pre automatick", "vyplach pre automatick",
      "výplach automatickej prevod", "vyplach automatickej prevod",
      "výplach prevodovky", "vyplach prevodovky", "transmission cleaner"],
     ["Autochémia & Aditíva", "Výplachy prevodovky"]),
    # Žeraviace/sviečky PRED ATF (BorgWarner/BERU inak → Prevodové oleje).
    # (ignition coil / zapaľovacia cievka → Elektro, nie sviečky)
    (["žeraviac", "zeraviac", "žeravia", "zeravia", "glow plug", "heater plug", "spark plug", "sviečka", "sviecka", "žiarovacia"],
     ["Náhradné diely", "Motor & sanie", "Sviečky & zapaľovanie"]),
    (["ignition coil", "zapaľovacia cievka", "zapalovacia cievka", "cievka zapaľ", "cievka zapal"],
     ["Náhradné diely", "Elektro & senzory", "Zapaľovacie cievky"]),
    # Čističe senzorov PRED hard-part „sensor/snímač“ (AIRFLOW SENSOR CLEANER ≠ diel).
    (["sensor cleaner", "airflow sensor cleaner", "maf cleaner", "map cleaner",
      "čistič senzor", "cistic senzor", "čistič snímač", "cistic snimac",
      "čistič mass air", "cistic mass air"],
     ["Autochémia & Aditíva", "Čističe palivového systému"]),
    # Snímač opotrebenia doštičiek PRED generickým „snímač“ (≠ teplotné čidlo).
    (["opotrebenia bŕzd", "opotrebenia brzd", "snímač opotrebenia", "snimac opotrebenia",
      "indikátor opotrebovania", "indikator opotrebovania",
      "indikátor opotrebenia", "indikator opotrebenia",
      "opotrebovania obloženia", "opotrebovania oblozenia",
      "opotrebovanie obloženia", "opotrebovanie oblozenia",
      "výstražný kontakt", "vystrazny kontakt", "warnkontakt",
      "brake wear", "wear indicator", "pad wear", "wear sensor"],
     ["Náhradné diely", "Brzdy", "ABS senzory & čidlá"]),
    # Snímače PRED ATF/„prevodov“ — „Snímač otáčok, automatická prevodovka“ ≠ olej.
    # Nie holé EN „sensor“ (kradne *SENSOR CLEANER* / chémiu).
    (["snímač", "snimac", "otáčok", "otacok", "rpm sensor", "speed sensor",
      "transmission sensor", "gearbox sensor", "crankshaft sensor", "camshaft sensor"],
     ["Náhradné diely", "Elektro & senzory", "Teplotné & tlakové čidlá"]),
    # Poistky/držiaky PRED generickým elektro (MIDI/MAXI/ATO ≠ mazivo).
    (["poistka", "poistky", "držiak poistky", "drziak poistky", "fuse holder", "fuse ",
      " g poistka", "keramické bezpečnostné po", "keramicke bezpecnostne po"],
     ["Náhradné diely", "Elektro & senzory", "Relé & poistky"]),
    # Elektro konektory/káble PRED chémiou (description často spomína EGR/DPF).
    (["konektor", "connector", "koncovkový", "koncovkovy", "mate-n-lok", "jazýček", "jazycek",
      "izolačná priechodka", "izolacna priechodka", "káblový konektor", "kablovy konektor",
      "kábl. priech", "kabl. priech", "káblová priechod", "kablova priechod",
      "cable gland", "rozvodnú skriň", "rozvodnu skrin",
      "špirálový kábel", "spiralovy kabel", "wiring harness", "kabeláž", "kabelaz", "zásuvk", "zasuvk",
      "abs/ebs", "abs ebs", "opravná spojka", "opravna spojka", "opravný kábel", "opravny kabel"],
     ["Náhradné diely", "Elektro & senzory"]),
    # Airbag clock-spring PRED náradie „pružin*“.
    (["airbag", "clock spring", "clockspring", "vinutá pružina", "vinuta pruzina", "vinutá prużina"],
     ["Náhradné diely", "Elektro & senzory"]),
    # Wave 5 — imobilizér / keyless / kľúče PRED náradie „kľúč “. NEMAZAŤ.
    (["imobilizér", "imobilizer", "immobiliser", "immobilizer", "imobiliz",
      "centrálne zamykanie", "centralne zamykanie", "central locking",
      "centrálny zámok", "centralny zamok", "central lock",
      "keyless entry", "alarm a zamykanie",
      "diaľkové ovládanie kľúč", "dialkove ovladanie kluc", "remote key",
      "transpondér", "transponder", "rfid kľúč", "rfid kluc",
      "mikroakumulátor", "mikroakumulator",
      "kľúča zapaľovania", "kluca zapalovania", "batéria 3.0v", "bateria 3.0v",
      "key fob battery", "key battery"],
     ["Náhradné diely", "Elektro & senzory", "Imobilizéry & centrálne zamykanie"]),
    (["spínacia skrinka", "spinacia skrinka", "spínač skrinky", "spinac skrinky",
      "spínače skrinky", "spinace skrinky", "ignition switch", "ignition lock",
      "spínač zapaľovania", "spinac zapalovania", "zámok zapaľovania", "zamok zapalovania",
      "keyless go", "keyless start", "štartovanie bez kľúča", "startovanie bez kluca",
      "start/stop button", "tlačidlo štart", "tlacidlo start",
      "zündschloss", "zündschalter"],
     ["Náhradné diely", "Elektro & senzory", "Štartovanie bez kľúča & spínacie skrinky"]),
    (["zámok dverí", "zamok dveri", "zámky dverí", "zamky dveri", "door lock",
      "zámok zadných", "zamok zadnych", "zámok predných", "zamok prednych",
      "zadný zámok", "zadny zamok", "predný zámok", "predny zamok",
      "zámok riadenia", "zamok riadenia", "steering lock", "zámok volantu", "zamok volantu",
      "zámky a kliky", "zamky a kliky", "door handle", "kľučka dverí", "klucka dveri",
      "kľúčenka", "klucenka", "auto kľúč", "auto kluc", "car key", "vehicle key",
      "náhradný kľúč", "nahradny kluc", "kľúč od auta", "kluc od auta",
      "ovládač kľúča", "ovladac kluca", "key fob", "key blade"],
     ["Náhradné diely", "Karoséria & svetlá", "Zámky & kľúče"]),
    (["odhlučn", "odhlucn", "protihlukov", "tlmenie hluku", "zvukov izol", "zvukovú izol", "zvukovu izol",
      "taviteľná vrstva", "tavitelna vrstva", "sound deaden", "antidrön", "antidron", "butylová podložka", "butylova podlozka"],
     ["Náhradné diely", "Karoséria & svetlá", "Odhlučnenie"]),
    (["atf", "gear oil", "prevodov", "transmisn", "75w", "70w", "80w90", "80w-90", "80w 90",
      "85w90", "85w-90", "75w90", "75w-90", "75w80", "75w-80",
      "cvtf", "diferenci", "transfer case", "axle oil", "gl-4", "gl-5", "limited slip",
      "sp-iv", "asrc", "nsg370", "nv245", "nv246", "nv146", "nv3500", "borgwarner 44", "44-40", "44-44"],
     ["Motorové oleje & Kvapaliny", "Prevodové oleje"]),
    # Motorové oleje podľa viskozity (PRED generickým olej pravidlom)
    (["0w-20", "0w20", "0w 20", "sae 0w-20", "sae 0w20"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "0W-20"]),
    (["0w-30", "0w30", "0w 30", "sae 0w-30", "sae 0w30"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "0W-30"]),
    (["0w-40", "0w40", "0w 40", "sae 0w-40", "sae 0w40"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "0W-40"]),
    (["5w-20", "5w20", "5w 20", "sae 5w-20", "sae 5w20"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "5W-20"]),
    (["5w-30", "5w30", "5w 30", "sae 5w-30", "sae 5w30"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "5W-30"]),
    (["5w-40", "5w40", "5w 40", "sae 5w-40", "sae 5w40"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "5W-40"]),
    (["10w-30", "10w30", "10w 30", "sae 10w-30"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "10W-30"]),
    (["10w-40", "10w40", "10w 40", "sae 10w-40"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "10W-40"]),
    (["10w-60", "10w60", "10w 60", "sae 10w-60"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "10W-60"]),
    (["15w-40", "15w40", "15w 40", "sae 15w-40"], ["Motorové oleje & Kvapaliny", "Motorové oleje", "15W-40"]),
    # Monograde SAE (bez W) + menej časté — leaf „Ostatné viskozity“
    (["sae 30", "sae30", "sae 40", "sae40", "sae 50", "sae50",
      "motor oil sae", "engine oil sae", "monograde"],
     ["Motorové oleje & Kvapaliny", "Motorové oleje", "Ostatné viskozity"]),
    # Plastické mazivá PRED catch-all značiek (Castrol grease ≠ motorový olej)
    (["plastické mazivo", "plasticke mazivo", "vysokoteplotné plastické", "vysokoteplotne plasticke",
      "vazelín", "vazelin", "grease cartridge", "nlgi", "bearing grease",
      "hi-tech extreme", "hi tech extreme"],
     ["Dielenská chémia & Údržba", "Mazivá & Pasty"]),
    (["engine oil", "motorový olej", "motorovy olej", "motor oil", "motorenöl", "motorenol",
      "maxpro", "pennzoil", "platinum", "rotella", "mobil 1", "amalie",
      "helix", "magnatec", "edge ", "quartz", "castrol", "motul", "liqui moly",
      "valvoline", "fuchs", "elf ", "total energies", "sae 10w", "sae 5w", "sae 0w"],
     ["Motorové oleje & Kvapaliny", "Motorové oleje"]),
    # Hard-part DPF/FAP PRED autochémou (filter diel ≠ čistič)
    (["dpf filter", "fap filter", "filter pevných", "filter pevnych",
      "diesel particulate filter", "filtr pevných častíc", "filtr pevnych castic"],
     ["Náhradné diely", "Výfuk & DPF diely", "DPF / FAP filtre"]),
    # Autochémia — DPF/EGR čističe (holé „dpf“ až PO hard-part filter)
    (["dpf clean", "dpf cleaner", "čistič dpf", "cistic dpf", "dpf čistič", "dpf cistic",
      "egr clean", "egr cleaner", "čistič egr", "cistic egr", "egr valve clean",
      "dpfcc", "dpf power", "dpf super", "oxicat", "eolys", "fap fluid", "cerine",
      "diesel particulate clean", "particulate filter clean",
      "čistič filtra pevných", "cistic filtra pevnych",
      "čistič filtra pevnych", "cistic filtra pevných",
      "dpf", "fap"],  # až po „dpf filter“ / „fap filter“
     ["Autochémia & Aditíva", "Čističe DPF / EGR"]),
    (["oil system cleaner", "výplach motora", "vyplach motora", "engine flush", "engine clean",
      "čistič motora", "cistic motora", "čistič motorov", "cistic motorov",
      "čistenie motora", "cistenie motora",
      "čistič vnútornej časti motora", "cistic vnutorn", "metabond cl ", "metabond cl-",
      "metabond cl(", " čistič vnútornej",
      "čistič olejového systému", "cistic olejoveho systemu", " flush"],
     ["Autochémia & Aditíva", "Výplachy motora"]),
    # HVAC hard-part PRED autochémiou „Klimatizácia“ — inak „klimatiz/interiér“ ukradne diely.
    (["interiérový ventilátor", "interierovy ventilator",
      "ventilátor kúren", "ventilator kuren", "ventilátor topen", "ventilator topen",
      "ventilátor klimatiz", "ventilator klimatiz",
      "blower motor", "cabin blower", "heater blower", "heater fan",
      "výmenník tepla", "vymenik tepla", "výmenník vykurov", "vymenik vykurov",
      "heat exchanger", "heater matrix",
      "vnútorný ventilátor", "vnutotny ventilator", "vnutorny ventilator",
      "vykurovacie teleso", "vložka kúrenia", "vlozka kurenia",
      "radiátor kúrenia", "radiator kurenia", "vložka elektrického kúrenia",
      "vzduchová tryska"],
     ["Náhradné diely", "Chladenie & kúrenie", "Kúrenie & výparníky"]),
    (["heater core", "výparník", "vyparnik", "evaporator core", "ac evaporator"],
     ["Náhradné diely", "Chladenie & kúrenie", "Kúrenie & výparníky"]),
    # Autochémia klímy — čističe/tesnenie/výkon (NIE holé „klimatiz“ / hard-part).
    (["air condition foam", "air conditioner foam", "air codition",  # frequent supplier typo
      "airflow", "ac clean", "ac leak", "a/c clean", "a/c foam",
      "čistič klimatiz", "cistic klimatiz", "coolius", "evapomat",
      "na klimatizáci", "na klimatizacie", "sprej na klimatiz",
      "dezinfekčný sprej na klimatiz", "dezinfekcny sprej na klimatiz",
      "čistiace zariadenie na klimatiz", "cistiace zariadenie na klimatiz",
      "utesnenie únikov klimatiz", "utesnenie unikov klimatiz",
      "utesnenie únikov", "utesnenie unikov",
      "detekciu netesností v klimatiz", "detekciu netesnosti v klimatiz",
      "netesností v klimatiz", "netesnosti v klimatiz",
      "zvýšenie výkonu klimatiz", "zvysenie výkonu klimatiz", "zvysenie vykonu klimatiz",
      "conditioner foam cleaner"],
     ["Autochémia & Aditíva", "Klimatizácia"]),
    # Len CHÉMIA / čističe — hard-part vstreky/čerpadlá majú leaf pod Motor & sanie.
    (["čistič paliv", "cistic paliv", "fuel system cleaner", "fuel injector cleaner",
      "injector cleaner", "čistič vstrekov", "cistic vstrekov", "čistič vstrekovač",
      "cistic vstrekovač", "guard fill",
      "čistič škrtiacej", "cistic skrtiacej", "throttle body cleaner",
      "čistič škrtiacej klapky", "cistic skrtiacej klapky",
      "čistič systému dieselového", "cistic systemu dieseloveho",
      "kompletný čistič systému diesel", "kompletny cistic systemu diesel"],
     ["Autochémia & Aditíva", "Čističe palivového systému"]),
    (["megasel", "megalene", "petrol detox", "diesel detox", "octane", "detox", "oktán"], ["Autochémia & Aditíva", "Aditíva do nafty & benzínu"]),
    # Údržbové oleje v spreji PRED generickým „maziv“ (HHS/Multi aj v aerosól bloku nižšie)
    (["olej na údržbu", "olej na udrzbu", "multi plus", "multi cobra",
      "maintenance oil", "penetrating oil spray", "hhs"],
     ["Dielenská chémia & Údržba", "Aerosóly & Spreje"]),
    # Mazivá PRED ochranou motora (HI-TECH EXTREME = vazelína!)
    # Plastické mazivá / vazelína PRED catch-all „castrol/mobil“ → motorový olej
    (["hi-tech extreme", "hi tech extreme", "plastické mazivo", "plasticke mazivo",
      "vysokoteplotné plastické", "vysokoteplotne plasticke",
      "vazelín", "vazelin", "grease cartridge", "nlgi", "bearing grease", "wheel bearing grease"],
     ["Dielenská chémia & Údržba", "Mazivá & Pasty"]),
    # Nie holé „keramick“ — trafí „keramické poistky“ (elektro).
    (["anti seize", "anti-seize", "grease", "maziv", "copper paste",
      "keramická pasta", "keramicka pasta", "ceramic paste", "adhesive grease"],
     ["Dielenská chémia & Údržba", "Mazivá & Pasty"]),
    # Stop-leak ≠ ochrana motora; Bike-Line oil guard ≠ Metabond SPIRIT
    (["gearbox stop leak", "engine stop leak", "stop leak", "tesnenie úniku", "tesnenie uniku"], ["Autochémia & Aditíva", "Tesnenie únikov"]),
    (["oil system guard", "oil system care", "bike-line", "bike line", "oil enhancer", "engine oil enhancer", "oil booster additive"], ["Autochémia & Aditíva", "Starostlivosť o olejový systém"]),
    # „metabond cl“ je vo Výplachy (vyššie) — tu NIE, lebo substring „metabond clear“
    # Stierače PRED glass cosmetics (Stierač čelného skla ≠ rain repellent).
    (["stierač", "stierac", "wiper blade", "wiper ", "stierače", "stierace"],
     ["Náhradné diely", "Karoséria & svetlá", "Stierače"]),
    # Hard-part sklá / sťahovače / vzpery (PRED kozmetikou skiel)
    (["čelné sklo", "celne sklo", "predné sklo", "predne sklo", "zadné sklo", "zadne sklo",
      "bočné sklo", "bocne sklo", "windscreen", "windshield", "door glass",
      "autosklo ", "auto sklo"],
     ["Náhradné diely", "Karoséria & svetlá", "Autosklá"]),
    (["window regulator", "window lift", "sťahovač okien", "stahovac okien",
      "sťahovanie okien", "stahovanie okien", "mechanizmus okna", "power window motor"],
     ["Náhradné diely", "Karoséria & svetlá", "Sťahovače okien"]),
    (["plynová vzpera", "plynova vzpera", "gas strut", "gas spring", "gas damper",
      "vzpera kapoty", "vzpera kufra", "boot strut", "bonnet strut"],
     ["Náhradné diely", "Karoséria & svetlá", "Plynové vzpery"]),
    (["výplň dverí", "vypln dveri", "door panel", "obloženie interiér", "oblozenie interier",
      "lakťová opierka", "laktova opierka", "slnečná clona", "slnecna clona",
      "interior trim", "dash trim"],
     ["Náhradné diely", "Karoséria & svetlá", "Interiér & obloženie"]),
    # Metabond CLEAR = kozmetika. NIE holé „čelného skla“ — to kradlo stierače.
    (["metabond clear", "ochrana čelného", "ochrana celneho",
      "ochrana čelného skla", "ochrana celneho skla",
      "rain repellent", "glass protect"],
     ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    (["ceratec", "engine protect", "nano engine", "ochrana motora", "spirit", "oil booster",
      "metabond truck", "metabond old", "metabond pro", "metabond universal", "metabond h plus",
      "metabond eco", "metabond new", "metabond gasoline", "metabond diesel",
      " h plus", "zušľachťovač hydraul", "zuslachtovac hydraul",
      "protect & seal", "stop smoke",
      "keramická ochrana motora", "keramicka ochrana motora", "ceramic engine",
      "hydraulická zdvihátka", "hydraulicka zdvihatka", "hydraulic tappet", "zdvihátka"],
     ["Autochémia & Aditíva", "Ochrana motora"]),
    # AdBlue ≠ nafta (MUSÍ byť pred diesel/nafta pravidlom)
    (["adblue", "ad blue", "ad-blue", "scr fluid", "scr systém", "scr system"], ["Autochémia & Aditíva", "AdBlue & SCR"]),
    (["diesel conditioner", "diesel system", "diesel anti", "diesel applicator", "cetane", "anti-gel", "anti gel", "petrol ", "benzín", "benzin"], ["Autochémia & Aditíva", "Aditíva do nafty & benzínu"]),
    # Dielenská (+ Würth Intershop) — antikoróz/zinc + rost off
    (["rust", "hrdz", "odhrdzova", "uvolňova", "uvolnova", "penetrant", "rust remover", "rost off",
      "antikoróz", "antikoroz", "zinc 300", "zinkový sprej", "zinkovy sprej", "cold zinc",
      "náter podvoz", "nater podvoz", "ochrana podvoz", "underbody", "under body",
      "ochranný náter podvoz", "metalyt"],
     ["Dielenská chémia & Údržba", "Odhrdzovače & Uvoľňovače"]),
    (["brake cleaner", "čistič bŕzd", "cistic brzd", "odmasť", "odmast", "degreaser", "brake clean",
      "čistič brzdového systému", "cistic brzdoveho systemu", "brake system cleaner",
      "umývací prostriedok na motory", "umyvaci prostriedok na motory", "engine wash", "engine cleaner spray",
      "montážny čistič", "montazny cistic", "parts cleaner", "brake and parts",
      "čistič pu", "cistic pu", "pu peny", "pu foam cleaner",
      "čistič kovov", "cistic kovov", "metal cleaner", "cleaner 7063",
      "odvápňovač", "odvapnovac", "oživenie kovov", "ozivenie kovov", "metal polish cleaner"],
     ["Dielenská chémia & Údržba", "Čističe bŕzd & Odmasťovače"]),
    # Detektory / značkovače / ochrana zvárania (PRED generickým catch-all)
    (["detektor netesnost", "leak detector", "cestný značkovač", "cestny znackovac", "road marker",
      "elektronická ochrana pri zváraní", "elektronicka ochrana pri zvarani", "welding protector"],
     ["Dielenská chémia & Údržba", "Aerosóly & Spreje"]),
    (["epoxidový základ", "epoxidovy zaklad", "epoxy primer", "1k epoxid", "základný náter", "zakladny nater"],
     ["Dielenská chémia & Údržba", "Odhrdzovače & Uvoľňovače"]),
    # Silikónový sprej / PTFE = aerosól PRED generickým silikón/lepidlo
    (["silicone spray", "silikónový sprej", "silikonovy sprej", "silicone lubricant",
      "ptfe spray", "dry lube", "kontaktný sprej", "kontaktny sprej", "contact spray",
      "lube spray", "power lube", "start spray", "štartovací sprej", "startovaci sprej",
      "hhs ", "multi plus", "multi cobra"],
     ["Dielenská chémia & Údržba", "Aerosóly & Spreje"]),
    # Pištoľ/dýza na kartuše / penovú pištoľ = náradie PRED penou/lepidlom
    (["aplikačná pištoľ", "aplikacna pistol", "aplikačná dýza", "aplikacna dyza",
      "striekacia hlavica", "striekacia dýza", "striekacia dyza", "refillo",
      "door gap", "gripper", "mazací lis", "mazaci lis",
      "dávkovac", "davkovac", "hadica s adaptérom", "hadica s adapterom",
      "penová pištoľ", "penova pistol", "foam gun", "purlogic", "pu gun"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # Stavebná chémia = TOP-LEVEL peer Dielenskej (nie pod ňou).
    (["expanzná pena", "expanzna pena", "expanznú pen", "expanding foam",
      "pištoľová pena", "pistolova pena", "montážna pena", "montazna pena",
      "montážnu pen", "pu pena", "pur pena", "polyuretánová pena", "polyuretanova pena",
      "construction foam", "spray foam", "pena na zárubne", "pena na zarubne",
      "zárubňov", "zarubnov"],
     ["Stavebná chémia", "PU peny & montáž"]),
    (["imelast", "hydroizolač", "hydroizolac", "hydroizolačná membrán", "hydroizolacna membran",
      "flaušová páska", "flausova paska", "páska na škáry", "paska na skary", "páska na skary",
      "skleneného vlákna", "skleneneho vlakna", "glass fibre tape", "glass fiber tape", "joint tape"],
     ["Stavebná chémia", "Hydroizolácia & pásky"]),
    (["glej", "wood glue", "studený glej", "studeny glej",
      "maliarsky akryl", "akrylátový tmel", "akrylatovy tmel"],
     ["Stavebná chémia", "Stavebné lepidlá & tmely"]),
    # Kovový tesniaci krúžok = diel (PRED generickým „tesniac“ → lepidlá)
    (["tesniaci krúžok", "tesniaci kruzok", "medený tesniaci", "medeny tesniaci",
      "copper washer", "sealing ring", "crush washer"],
     ["Náhradné diely", "Motor & sanie", "Tesnenia motora & sania"]),
    # Výfukové tmely/pasty = dielenská chémia (nie Autodiely výfuk)
    (["výfukový tmel", "vyfukovy tmel", "exhaust putty", "montážna pasta na výfuk", "montazna pasta na vyfuk",
      "pasta na výfuk", "pasta na vyfuk",
      "aktivátor", "aktivator", "adhézny prostriedok", "adhezny prostriedok", "contact clean"],
     ["Dielenská chémia & Údržba", "Tesnenia & Lepidlá"]),
    # Kartuša s lepidlom / RTV / spájka / tekutý kov — auto/dielenské tesnenia
    (["silicone", "silikón", "lepidlo", "tesniac", "gasket maker", "replast",
      "rtv ", "threadlocker", "zaisťovač skrutiek", "zaistovac skrutiek", "anaerob",
      "lepiaca páska", "lepiaca paska", "tkaninová páska", "tkaninova paska", "cloth tape",
      "uv lepiaca", "koncovej objímky", "koncovej objimky", "duct tape",
      "kartuša s lepid", "kartusa s lepid", "kartuša s prísad", "silikón super",
      "mäkkú spájku", "makku spajku", "náhrada za mäkkú", "nahrada za makku",
      "tekutý kov", "tekuty kov", "kovový tmel", "kovovy tmel", "liquid metal"],
     ["Dielenská chémia & Údržba", "Tesnenia & Lepidlá"]),
    (["aerosol", "sprej", "spray"], ["Dielenská chémia & Údržba", "Aerosóly & Spreje"]),
    # „mazací“ substring kradne mazací lis — len mazivo/tuk/klzné
    (["mazivo", "tuk ", "lubesnake", "klzné", "klzne", "klzný", "klzny"],
     ["Dielenská chémia & Údržba", "Mazivá & Pasty"]),
    (["montážna pasta", "montazna pasta", "medený sprej", "medeny sprej", "cu 800",
      "pasta pre hydraulické kladiv", "pasta pre hydraulicke kladiv", "hydraulic hammer paste"],
     ["Dielenská chémia & Údržba", "Mazivá & Pasty"]),
    # Predlžovacie trubice / aplikátory sprejov = náradie (nie samotný aerosól)
    (["predlžovacia trubic", "predlzovacia trubic", "extension tube", "aplikačná trubic", "aplikacna trubic"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # UV lampa na lepenie/opravu skiel = náradie (nie autokozmetika / nie žiarovka)
    (["uv led lampa", "uv lampa", "bezšnúrová uv", "bezsnurova uv",
      "oprava čelných skiel", "oprava celnych skiel", "opravu čelných skiel", "opravu celnych skiel"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # Würth náradie / karoséria / sklá
    (["karosár", "karosar", "vyrezávan", "vyrezavan", "čelné skl", "celne skl", "opravy skiel",
      "kladivo na povrch", "ťažná svorka", "tazna svorka", "brúsik", "brusik",
      "lemovačka", "lemovacka", "lemovač", "lemovac"],
     ["Náradie & Vybavenie", "Karoséria & čalúnenie – náradie"]),
    # Držiak EČV / ŠPZ (+ Würth Circo / základová doska)
    (["evidenčné číslo", "evidencne cislo", "držiak na evidenč", "drziak na evidenc",
      "držiak ŠPZ", "drziak SPZ", "držiak ečv", "drziak ecv", "držiak ečv",
      "ečv circo", "ecv circo", "základová doska pre držiak", "zakladova doska pre drziak",
      "license plate holder", "number plate holder"],
     ["Náhradné diely", "Karoséria & svetlá"]),
    # Würth trysky / dýzy na tmel (nie ostrekovač)
    (["tryska s plochou štrbin", "tryska s plochou strbin", "plochou štrbinou",
      "plochou strbinou", "striekací tmel na švy", "striekaci tmel na svy",
      "seam sealer nozzle", "flat slot nozzle"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    # Káblové svorky / cable clamps (nie batériová pólová svorka)
    (["svorka na káble", "svorka na kable", "káblová svorka", "kablova svorka",
      "svorky na káble", "svorky na kable", "cable clamp", "cable clip",
      "wire clamp", "wire clip"],
     ["Náhradné diely", "Elektro & senzory"]),
    # Zástrčkové vložky / pin kontakty
    (["zástrčková vložka", "zastrckova vlozka", "zástrčkovú vlož", "pin contact",
      "kontaktná vložka", "kontaktna vlozka"],
     ["Náhradné diely", "Elektro & senzory"]),
    (["skrutkovač", "skrutkovac", "bit ", "bity ", "račne", "racne", "kľúč ", "kluc "],
     ["Náradie & Vybavenie", "Kľúče & račne"]),
    (["laser ", "krížový laser", "krizovy laser", "merací", "meraci"],
     ["Náradie & Vybavenie", "Diagnostika & meranie"]),
    # Autokozmetika — čističe/pásky PRED generickým „tesniac/páska“ v lepidlách
    (["wax", "vosk", "polish", "lešti", "leštid", "ceramic coating", "cutting compound", "top coat", "anti-hologram", "anti hologram"], ["Autokozmetika & Detail", "Leštenie & vosky"]),
    (["šampón", "sampon", "shampoo", "predumyt", "pre wash", "umývan", "umyvan", "foam", "snow foam", " wash", "crazy wash", "moto cleaner", "špongia", "spongia", "mikrovlákn", "utierka", "towel",
      "ochranná fólia", "ochranna folia", "maskovacia fólia", "maskovacia folia", "mask light",
      "maskovacia páska", "maskovacia paska", "krycia páska", "krycia paska", "krycia maskov",
      "soft mask", "baliaca páska", "baliaca paska",
      "odstraňovač asfalt", "odstranovac asfalt", "tar remover", "živice", "zivice", "asfaltu",
      "čistič skla", "cistic skla", "čistič čeln", "cistic celn", "screenwash", "screen wash",
      "čistič karosér", "cistic karoser", "čistič vozidl", "cistic vozidl",
      "čistiaci prostriedok na vozidl", "cistiaci prostriedok na vozidl",
      "active clean", "fi-cleaner", "liquid green", "viacúčelový čistiaci", "viacucelovy cistiaci",
      "odstraňovač škvŕn", "odstranovac skvrn", "odstraňovač olejových škv", "odstranovac olejovych skv",
      "stain remover",
      "čistiaci prostriedok na karosér", "cistiaci prostriedok na karoser",
      "haftclean", "aktívny čistič povrch", "aktivny cistic povrch",
      "suché čistenie", "suche cistenie"],
     ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    # Interiér kozmetika — NIE kabínový filter / interiérový ventilátor (hard-part vyššie).
    # Bez holého „interiér/interior“ — ukradlo by to diely (ventilátor, panel…).
    (["cockpit", "interiérový čistič", "interierovy cistic", "interior cleaner",
      "čistič interiér", "cistic interier", "interior detail", "interior care",
      "osviežovač vzduchu", "osviezovac vzduchu", "osviežovač interiér", "osviezovac interier",
      # Len interiérový oživovač — „… gumy Exteriér“ ostáva v Exteriér/Leštenie.
      "oživovač plastu a gumy - interiér", "ozivovac plastu a gumy - interier",
      "oživovač plastu a gumy – interiér", "ozivovac plastu a gumy – interier",
      "koža", "koza", "kože", "koze", "kožen", "kozen", "textil", "leather",
      "starostlivosť o kožu", "starostlivost o kozu", "čistič na kožu", "cistic na kozu",
      "prostriedok na kožu", "prostriedok na kozu", "čistiaci prostriedok na kožu",
      "cistiaci prostriedok na kozu", "alcantaru", "alcantara",
      "autoparfum", "parfum", "vôňa", "vona", "dressing", "car perfume", "perfume",
      "cockpit spray", "dashboard"],
     ["Autokozmetika & Detail", "Interiér"]),
    # Stierače PRED glass/čelné sklo kozmetikou (a pred „umývan“ šumom)
    (["wiper", "stierač", "stierac", "stierače", "stierace", "lišta stierača", "lista stieraca",
      "rameno stierača", "rameno stieraca"],
     ["Náhradné diely", "Karoséria & svetlá", "Stierače"]),
    # Kozmetika diskov — NIE hard-part ventily/matice (tie sú v Autodiely nižšie)
    (["wheel cleaner", "čistič disk", "cistic disk", "rim cleaner", "tire shine",
      "tyre shine", "hard wheel", "black wheel",
      "čistič na ráfik", "cistic na rafik", "čistič ráfik", "cistic rafik",
      "pena na ošetrovanie", "pena na osetrovanie",
      "ošetrovanie pneu", "osetrovanie pneu", "tyre foam", "tire foam"],
     ["Autokozmetika & Detail", "Disky & pneumatiky"]),
    (["glass cleaner", "glass a-f", "okná", "okna", "tar remover", "insect", "anty insect", "moscerini"], ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    (["aditiv", "aditív"], ["Autochémia & Aditíva", "Aditíva do nafty & benzínu"]),
    # NIE holé „gear“ v MAXGEAR — len s medzerami / easy gear / gear oil.
    (["easy gear", "gear oil", "gearbox oil", " gear "], ["Motorové oleje & Kvapaliny", "Prevodové oleje"]),
    # Autobatérie (PRED catch-all; „MAXGEAR batéria“ ≠ prevodovka)
    (["štartovacia batéria", "startovacia bateria", "autobatéria", "autobateria",
      "car battery", "starter battery", "akumulátor 12v", "akumulator 12v",
      "akumulátor 12 v", "akumulator 12 v", "batéria 12v", "bateria 12v",
      "agm batéri", "agm bateri", "efb batéri", "efb bateri"],
     ["Náhradné diely", "Elektro & senzory", "Štartéry & alternátory"]),
    (["clean motor"], ["Autochémia & Aditíva", "Výplachy motora"]),
    # Autodiely — konkrétny typ (LKQ / Szekop / EF — pred generickými catch-all)
    (["control arm", "wishbone", "rameno nápravy", "rameno napravy", "rameno ", "čap ramena", "cap ramena", "ball joint", "guľový čap", "gulovy cap"], ["Náhradné diely", "Náprava & podvozok", "Ramena & čapy"]),
    # Airbag clock-spring pred generickou „pružina“ (inak spadne do podvozku).
    (["airbag", "clock spring", "clockspring", "vinutá pružina", "vinuta pruzina", "vinutá prużina"], ["Náhradné diely", "Elektro & senzory"]),
    (["tlmič", "tlmic", "tlumič", "tlumic", "tlumičov", "tlumicov",
      "shock absorber", "shock ", "pružina", "pruzina", "pružín", "pruzin",
      "coil spring", "strut ",
      "doraz odpruž", "doraz odpruz", "zátka, odpruž", "zatka, odpruz", "zátka odpruž",
      "zátka, odpruženie", "zatka, odpruzenie",
      "podložka zadnej pružiny", "podlozka zadnej pruziny", "podložka pružiny",
      "bump stop", "spring seat",
      "odpruženie", "odpruzenie", "odpružení", "odpruzeni", "odpruženia",
      "pérování", "perovani", "pérovani",
      "ložisko pružnej vzpery", "lozisko pruznej vzpery",
      "pružné ložisko vzpery", "pruzne lozisko vzpery",
      "ložisko vzpery", "lozisko vzpery",
      "sada odpružen", "sada odpruzen", "pro-street-s", "pro street s",
      "nastaviteľný podvozok", "nastavitelny podvozok", "coilover"],
     ["Náhradné diely", "Náprava & podvozok", "Tlmiče & pružiny"]),
    (["otoč nápravy", "otoc napravy", "zavesenia kolies", "knuckle", "steering knuckle",
      "tehlice kolesa", "tehlica kolesa", "wheel carrier"],
     ["Náhradné diely", "Náprava & podvozok", "Ramena & čapy"]),
    (["silentblok", "bushing", "bush "], ["Náhradné diely", "Náprava & podvozok", "Silentbloky"]),
    (["wheel bearing", "ložisko kolesa", "lozisko kolesa",
      "ložiska kolesa", "loziska kolesa", "ložiská kolesa", "loziska kolesa",
      "ložiskom", "loziskom", "skriňa ložisk", "skrina lozisk",
      "náboj kolesa", "naboj kolesa", "hub bearing", "wheel set"],
     ["Náhradné diely", "Náprava & podvozok", "Ložiská kolies"]),
    (["stabilizer", "stabilizátor", "stabilizator", "sway bar", "antiroll"], ["Náhradné diely", "Náprava & podvozok", "Stabilizátory"]),
    (["tie rod", "track rod", "spojovacia tyč", "spojovacia tyc", "riadiaca tyč"], ["Náhradné diely", "Náprava & podvozok", "Spojové tyče"]),
    (["brake pad", "brake pads", "brake lining",
      "brzdové doštičky", "brzdove dosticky",
      "brzdových doštičiek", "brzdovych dostickiek",
      "doštičky", "dosticky", "doštičiek", "dostickiek",
      "sada brzdových doštičiek", "sada brzdovych dostickiek",
      "brzdových platnič", "brzdovych platnic", "platničiek", "platniciek",
      "platničky", "platnicky"],
     ["Náhradné diely", "Brzdy", "Brzdové doštičky"]),
    # Príslušenstvo kotúčov PRED generickým „kotúč“ / chémiou (montážne sady).
    (["príslušenstva kotúčových bŕzd", "prislusenstva kotucovych brzd",
      "príslušenstvo kotúčových bŕzd", "prislusenstvo kotucovych brzd",
      "príslušenstvo bŕzd", "prislusenstvo brzd",
      "brake accessory", "brake accessories", "disc brake accessory",
      "súprava príslušenstva kotúč", "suprava prislusenstva kotuc",
      "súprava skrutiek pre kotúč", "suprava skrutiek pre kotuc",
      "skrutiek pre kotúčové", "skrutiek pre kotucove"],
     ["Náhradné diely", "Brzdy", "Brzdové doštičky"]),
    (["brake disc", "brzdový kotúč", "brzdovy kotuc", "brzdový kotouč", "brzdovy kotouc",
      "brake rotor", "kotouč"],
     ["Náhradné diely", "Brzdy", "Brzdové kotúče"]),
    (["brake caliper", "brzdový strmeň", "brzdovy strmen",
      "brzdového strmeňa", "brzdoveho strmena",
      "vodiaci puzd", "vodiacich puzd", "piest brzdového", "piest brzdoveho",
      "príslušenstva k brzdovému strmeň", "prislusenstva k brzdovemu strmen",
      "príslušenstvo k brzdovému strmeň", "prislusenstvo k brzdovemu strmen"],
     ["Náhradné diely", "Brzdy", "Brzdové strmene"]),
    (["brake hose", "brzdová hadica", "brzdova hadica", "wheel cylinder", "brzdový valček",
      "vsuvka brzdového", "vsuvka brzdoveho", "brzdového vedenia", "brzdoveho vedenia",
      "brake line fitting"],
     ["Náhradné diely", "Brzdy", "Brzdové hadice & valčeky"]),
    # Bubnové PRED generickým kotúč / dust shield
    (["drum brake", "bubnová brzda", "bubnova brzda", "bubnové brzdy", "bubnove brzdy",
      "brake shoe", "brzdové čeľuste", "brzdove celuste", "brzdová čeľusť", "brzdova celust",
      "brake drum", "brzdový bubon", "brzdovy bubon"],
     ["Náhradné diely", "Brzdy", "Bubnové brzdy & čeluste"]),
    (["krycia doska zadnej", "dust shield", "brake shield",
      "kotúčovej brzdy", "kotucovej brzdy", "kotúčové a bubnové", "kotucove a bubnove"],
     ["Náhradné diely", "Brzdy", "Brzdové kotúče"]),
    (["abs sensor", "abs senzor", "wheel speed sensor", "snímač abs", "snimac abs",
      "opotrebenia bŕzd", "opotrebenia brzd", "brake wear", "wear indicator"],
     ["Náhradné diely", "Brzdy", "ABS senzory & čidlá"]),
    (["handbrake", "ručná brzda", "rucna brzda", "parking brake cable"], ["Náhradné diely", "Brzdy", "Ručná brzda & laná"]),
    (["timing belt", "rozvodový remeň", "rozvodovy remen", "timing chain", "rozvodová reťaz",
      "rozvodová sada", "rozvodova sada", "timing kit", "tensioner", "napínač rozvod"],
     ["Náhradné diely", "Motor & sanie", "Rozvody & remene"]),
    (["poly-v", "poly v ", "serpentine", "klinový remeň", "klinovy remen",
      "drážkový remeň", "drazkovy remen", "v-ribbed", "v ribbed", "ribbed belt",
      "klinový a drážkový", "klinovy a drazkovy"],
     ["Náhradné diely", "Motor & sanie", "Klinové remene"]),
    (["turbocharger", "turbo ", "turbodúchad", "intercooler", "plnič",
      "chladič vzduchu", "chladic vzduchu"],
     ["Náhradné diely", "Motor & sanie", "Turbá & sanie"]),
    # Hard-part palivo PRED autochémiou (čističe majú vlastné keywords vyššie)
    (["fuel pump", "palivové čerpadlo", "palivove cerpadlo", "palivového čerpadla",
      "vstrekovacie čerpadlo", "vstrekovacie cerpadlo", "injection pump",
      "vstrekovací ventil", "vstrekovaci ventil", "vstrekovač", "vstrekovac",
      "injector nozzle", "fuel injector", "common rail", "high pressure pump",
      "vysokotlakové čerpadlo", "vysokotlakove cerpadlo"],
     ["Náhradné diely", "Motor & sanie", "Palivové čerpadlá & vstreky"]),
    (["engine mount", "motor mount", "uloženie motora", "ulozenie motora",
      "silentblok motora", "zavesenie motora", "engine support"],
     ["Náhradné diely", "Motor & sanie", "Uloženie motora"]),
    # Uloženie / držiak chladiča (nie „uloženie motora“)
    (["uloženie, chladič", "ulozenie, chladic", "uloženie chladiča", "ulozenie chladica",
      "držiak chladiča", "drziak chladica", "radiator mount", "radiator bracket",
      "mounting radiator", "uchycení chladiče", "uchyteni chladice"],
     ["Náhradné diely", "Chladenie & kúrenie", "Chladiče motora"]),
    # Držiak / lôžko zadného svetla
    (["oporné lôžko", "oporne lozko", "oporné ložisko svetla", "uchytenie zadného svetla",
      "uchytenie zadneho svetla", "uchytenie svetla", "tail light bracket", "lamp bracket",
      "držiak zadného svetla", "drziak zadneho svetla"],
     ["Náhradné diely", "Karoséria & svetlá", "Svetlomety & svetlá"]),
    (["water pump", "vodná pumpa", "vodna pumpa", "coolant pump",
      "vodné čerpadlo", "vodne cerpadlo", "elektrické vodné čerpadlo"],
     ["Náhradné diely", "Motor & sanie", "Vodné pumpy"]),
    # Vypúšťacie skrutky PRED generickým „tesnenie“ / chémiou
    (["vypúšťacia skrutka", "vypustacia skrutka", "oil drain plug", "oil sump plug",
      "vypúšťacia skrut", "vypustacia skrut"],
     ["Náhradné diely", "Motor & sanie", "Oleové pumpy & vypúšťacie skrutky"]),
    (["spark plug", "sviečka", "sviecka", "glow plug", "žeraviac", "zeraviac", "žeravia", "zeravia", "žiarovacia", "heater plug"],
     ["Náhradné diely", "Motor & sanie", "Sviečky & zapaľovanie"]),
    (["throttle body", "škrtiaca", "skrtiaca", "egr valve", "egr ventil"], ["Náhradné diely", "Motor & sanie", "EGR / škrtiaca klapka"]),
    # NIE holé „tesnenie“ — substring trafí aj „utesnenie“ (AC stop-leak chémia).
    (["gasket", "tesniaci krúžok", "tesniaci kruzok", "medený tesniaci", "medeny tesniaci",
      "manifold gasket", "intake gasket", "nasávania", "nasavania", "head gasket",
      "tesnenie hlavy", "tesnenie motora", "tesnenie sania", "tesnenie vačkov",
      "tesnenie ", "oil pan gasket"],
     ["Náhradné diely", "Motor & sanie", "Tesnenia motora & sania"]),
    (["radiator", "chladič motora", "chladic motora", "engine cooler",
      "uloženie, chladič", "ulozenie, chladic", "uloženie chladič", "ulozenie chladic",
      "držiak chladič", "drziak chladic", "mounting radiator", "radiator mount"],
     ["Náhradné diely", "Chladenie & kúrenie", "Chladiče motora"]),
    (["oporné lôžko náprav", "oporne lozko naprav", "oporné ložisko náprav",
      "oporne lozisko naprav", "silentblok oporn", "support bearing mount"],
     ["Náhradné diely", "Náprava & podvozok", "Silentbloky"]),
    (["thermostat", "termostat"], ["Náhradné diely", "Chladenie & kúrenie", "Termostaty"]),
    (["cooling fan", "ventilátor chladen", "ventilator chladen", "fan clutch",
      "ventilátor chladiča", "ventilator chladica"],
     ["Náhradné diely", "Chladenie & kúrenie", "Ventilátory & spojky"]),
    (["coolant hose", "hadica chlad", "radiator hose"], ["Náhradné diely", "Chladenie & kúrenie", "Hadice chladenia"]),
    # Len nádoba chladenia — NIE „expanzná pena“ (tá je vyššie v Tesnenia).
    (["expansion tank", "coolant reservoir",
      "expanzná nádob", "expanzna nadob", "expanzné nádob", "expanzne nadob",
      "vyrovnávacia nádob", "vyrovnavacia nadob", "compensating tank"],
     ["Náhradné diely", "Chladenie & kúrenie", "Expanzné nádoby"]),
    # Duplikát HVAC keywords (ak chem/kozmetika pravidlá niekde vyššie zlyhajú).
    (["heater core", "výparník", "vyparnik", "kúrenie", "kuranie",
      "interiérový ventilátor", "interierovy ventilator",
      "ventilátor kúren", "ventilator kuren"],
     ["Náhradné diely", "Chladenie & kúrenie", "Kúrenie & výparníky"]),
    # Hard-part klíma ≠ autochémia „čistič klimatiz“
    (["ac compressor", "a/c compressor", "kompresor klimatiz", "kompresoru klimatiz",
      "kompletné kompresory", "kompletne kompresory",
      "kondenzátor klimatiz", "kondenzator klimatiz", "ac condenser", "a/c condenser",
      "chladič klimatiz", "chladic klimatiz", "dryer receiver", "vysúšač klimatiz",
      "vysusac klimatiz"],
     ["Náhradné diely", "Chladenie & kúrenie", "Diely klimatizácie"]),
    (["oil cooler", "chladič oleja", "chladic oleja", "oilcooler",
      "olejový chladič", "olejovy chladic", "olejového chladiča"],
     ["Náhradné diely", "Chladenie & kúrenie"]),
    (["power steering pump", "čerpadlo posilňovača", "cerpadlo posilnovaca"], ["Náhradné diely", "Riadenie", "Čerpadlá posilňovača"]),
    (["steering rack", "hrebeň riadenia", "hreben riadenia"], ["Náhradné diely", "Riadenie", "Hrebene riadenia"]),
    (["steering boot", "manžeta riadenia", "manzeta riadenia"], ["Náhradné diely", "Riadenie", "Manžety riadenia"]),
    (["clutch kit", "spojková sada", "spojkova sada", "clutch set"], ["Náhradné diely", "Spojka & prevodovka", "Spojkové sady"]),
    (["clutch disc", "spojkový kotúč", "pressure plate", "prítlačný"], ["Náhradné diely", "Spojka & prevodovka", "Prítlačné taniere & kotúče"]),
    (["release bearing", "vysúvacie ložisko", "throwout"], ["Náhradné diely", "Spojka & prevodovka", "Vysúvacie ložiská"]),
    (["flywheel", "zotrvačník", "zotrvacnik", "dual mass"], ["Náhradné diely", "Spojka & prevodovka", "Zotrvačníky"]),
    (["cv boot", "manžeta poloosy", "manzeta poloosy", "drive shaft boot",
      "manžety náprav", "manzety naprav", "manžeta náprav", "manzeta naprav",
      "univ. manžet", "univ. manzet"],
     ["Náhradné diely", "Spojka & prevodovka", "Manžety poloosí"]),
    (["drive shaft", "poloos", "cv joint", "homokinetický"], ["Náhradné diely", "Spojka & prevodovka", "Poloosi & kĺby"]),
    (["catalytic", "katalyzátor", "katalyzator"], ["Náhradné diely", "Výfuk & DPF diely", "Katalyzátory"]),
    (["diesel particulate", "dpf filter", "fap filter", "filter pevných"], ["Náhradné diely", "Výfuk & DPF diely", "DPF / FAP filtre"]),
    (["lambda", "oxygen sensor", "o2 sensor", "lambda sonda"], ["Náhradné diely", "Výfuk & DPF diely", "Lambda sondy"]),
    (["muffler", "tlmič výfuku", "tlmic vyfuku", "exhaust silencer",
      "súprava na opravu výfuku", "suprava na opravu vyfuku", "oprava výfuku", "oprava vyfuku"],
     ["Náhradné diely", "Výfuk & DPF diely", "Tlmiče výfuku"]),
    # Kolesá / pneumatiky hard-parts PRED kozmetikou diskov
    (["skrutka kolesa", "matica kolesa", "wheel bolt", "wheel nut", "lug nut",
      "ventil pneumatik", "ventil pre automobilové", "ventil pre automobilove",
      "tire valve", "tyre valve"],
     ["Náhradné diely", "Náprava & podvozok", "Ložiská kolies"]),
    # Štartovacie káble / autokáble / akumulátor prísl. (nie „štartovací sprej“)
    (["štartovací kábel", "startovaci kabel", "jump lead", "jumper cable", "booster cable",
      "autokábel", "autokabel", "flry", "pólová svorka", "polova svorka",
      "battery terminal", "battery clamp"],
     ["Náhradné diely", "Elektro & senzory"]),
    (["starter ", "štartér", "starter motor"], ["Náhradné diely", "Elektro & senzory", "Štartéry & alternátory"]),
    (["alternator", "alternátor", "dynamo"], ["Náhradné diely", "Elektro & senzory", "Štartéry & alternátory"]),
    # 12V autobatéria ≠ key-fob 3.0V (to je vyššie pri imobil)
    (["autobater", "autobatér", "auto batér", "auto bater", "car battery",
      "akumulátor 12", "akumulator 12",
      "start-stop battery", "agm battery", "efb battery", "baterie 12v", "batéria 12v", "bateria 12v",
      "olovený akumulátor", "oloveny akumulator",
      # SK/CZ B2B: „Štartovacia batéria“, „startovací baterie“, AGM/EFB Start-Stop
      "štartovacia batér", "startovacia bater", "startovací bater", "startovaci bater",
      "štartovací akumul", "startovaci akumul",
      "start-stop", "start stop", "agm", "efb",
      "batéria agm", "bateria agm", "batéria efb", "bateria efb"],
     ["Náhradné diely", "Elektro & senzory", "Autobatérie"]),
    (["riadiaca jednotka", "riadiace jednotky", "riadiacej jednotky", "engine control unit",
      "ecu ", "ecm ", "tcu ", "bcm ", "abs control unit", "riadiaci modul"],
     ["Náhradné diely", "Elektro & senzory", "Riadiace jednotky"]),
    (["kabelový zväzok", "kabelovy zvazok", "káblový zväzok", "kablovy zvazok",
      "wiring harness", "wire harness", "kabeláž motora", "kabelaz motora"],
     ["Náhradné diely", "Elektro & senzory", "Káble & konektory"]),
    # „cievka“ samotné je šum — len ignition / zapaľovacia
    (["ignition coil", "zapaľovacia cievka", "zapalovacia cievka", "cievka zapaľ", "cievka zapal"],
     ["Náhradné diely", "Elektro & senzory", "Zapaľovacie cievky"]),
    (["nox sensor", "tlakové čidlo", "teplotné čidlo", "pressure sensor", "temp sensor",
      "snímač impulz", "snimac impulz", "snímač polohy", "snimac polohy",
      "kľukového", "klukoveho", "vačkového", "vackoveho", "crankshaft sensor", "camshaft sensor"],
     ["Náhradné diely", "Elektro & senzory", "Teplotné & tlakové čidlá"]),
    # Čistič / leštenie svetlometov — autokozmetika (nie leaf žiaroviek)
    (["čistenie svetlomet", "cistenie svetlomet", "headlight restor", "headlight clean",
      "leštenie svetlomet", "lestenie svetlomet", "renovácia svetlomet", "renovacia svetlomet"],
     ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    # Kompletné svetlomety / hmlovky / aktuátory PRED catch-all žiarovkami
    # (H7 v názve kompletu ≠ leaf žiarovky — match_bulb_type to musí pustiť).
    (["svetlomet", "světlomet", "svetlometu", "světlometu",
      "headlight assembly", "headlamp assembly", "head lamp unit", "lamp unit",
      "hlavný svetlomet", "hlavny svetlomet", "predný svetlomet", "predny svetlomet",
      "zadný svetlomet", "zadny svetlomet",
      "kompletné svetlo", "kompletne svetlo",
      "motorček ovládania svetlomet", "motorcek ovladania svetlomet",
      "motorčekček ovládania svetlomet", "motorcekcek ovladania svetlomet",
      "motorček xenon", "motorcek xenon", "motorček svetlomet", "motorcek svetlomet",
      "senzor, kontrola sklonu svetlomet", "senzor kontrola sklonu svetlomet",
      "ovládanie sklonu svetlomet", "ovladanie sklonu svetlomet",
      "sklonu svetlomet", "headlight level", "headlamp level",
      "mlhový světlomet", "mlhovy svetomet", "mlhový svetlomet", "mlhovy svetlomet",
      "hmlovka", "hmlové svetlo", "hmlove svetlo", "fog light", "fog lamp",
      "brzdové svetlo", "brzdove svetlo", "zadné svetlo", "zadne svetlo",
      "uchytenie zadného svetla", "uchytenie zadneho svetla",
      "uchytenie svetla", "držiak svetla", "drziak svetla",
      "tail lamp", "rear lamp", "stop light",
      "motorček ostrekovač", "motorcek ostrekovač", "motorček ostrekovačov",
      "tryska ostrekovača svetiel", "tryska ostrekovača", "headlight washer",
      "čerpadlo ostrekovača", "cerpadlo ostrekovača", "nádrž ostrekovača", "nadrz ostrekovača",
      "washer pump", "washer reservoir",
      "regulace sklonu světlomet", "regulace sklonu svetlomet",
      "sklonu světlomet", "sklonu svetlomet"],
     ["Náhradné diely", "Karoséria & svetlá", "Svetlomety & svetlá"]),
    # Ambient LED / LED pásy PRED catch-all žiarovkami (a PRED „lepidlo“ v description = páska na spodku)
    (["ambientné svetlá", "ambientne svetla", "ambientné osvetlenie", "ambientne osvetlenie",
      "ambientné ladiace", "ambientne ladiace", "led ambient", "ambient led",
      "ledint", "led pás", "led pas", "led pásik", "led pasik", "led pásy", "led pasy",
      "flexibilné led", "flexibilne led", "led tuning", "tuning kit osram"],
     ["Náhradné diely", "Svetlá & žiarovky", "LED žiarovky"]),
    # Konkrétne typy — match_bulb_type() má prioritu; tieto = fallback / catch-all
    # (nie „svetlomet“ samotné — to chytí aj čističe / leštidlá)
    (["headlight bulb", "tail light", "žiarovka", "ziarovka", "bulb ",
      "led žiarov", "led ziarov", "led bulb",
      "halogénová žiarov", "halogenova ziarov", "halogén", "halogen", "osvetlenie vozidl",
      # Würth obrys/komorové/výbojky (názvy bez „žiarovka“)
      "komorové svetlo", "komorove svetlo", "komorového svetla", "komoroveho svetla",
      "kluster", "obrysové svetlo", "obrysove svetlo",
      "pracovné svetlo", "pracovne svetlo", "svetlo, 12", "svetlo, 24",
      "výbojka", "vybojka", "plynová výbojka", "plynova vybojka"],
     ["Náhradné diely", "Svetlá & žiarovky", "Ostatné žiarovky"]),
    (["relé ", "rele ", "relay"],
     ["Náhradné diely", "Elektro & senzory", "Relé & poistky"]),
    (["side mirror", "spätné zrkadlo", "spatne zrkadlo"], ["Náhradné diely", "Karoséria & svetlá", "Spätné zrkadlá"]),
    (["bumper", "nárazník", "naraznik"], ["Náhradné diely", "Karoséria & svetlá", "Nárazníky & držiaky"]),
    # Würth / dielňa — spojky, antény, nabíjačky (nie Auto chémia dump).
    (["spájkovacia spojka", "spajkovacia spojka", "heat shrink splice", "zmrštiteľná teplom",
      "odrušovací pás", "odrusovaci pas", "zaslepovacia priechodka", "cable grommet",
      "príslušenstvo pre autoantén", "prislusenstvo pre autoanten", "autoantén",
      "uzemňovací nit", "uzemnovaci nit", "uzemňovac", "ground strap", "earth strap",
      "plochým zasúvacím jazýčkom", "plochym zasuvacim jazyckom",
      "plochý zásuvný kontakt", "plochy zasuvny kontakt",
      "plochý spojovací kontakt", "plochy spojovaci kontakt",
      "plochá spojka", "plocha spojka", "zásuvný kontakt", "zasuvny kontakt",
      "spojovací kontakt", "spojovaci kontakt",
      "izolačná trubička", "izolacna trubicka", "heat shrink tube",
      "ploché zasúvacie puzdrá", "ploche zasuvacie puzdra",
      "ploché zásuvné objímky", "ploche zasuvne objimky",
      "kryt zástrčky", "kryt zazstrcky", "kryt zastrcky",
      "zasúvacie puzdro", "zasuvacie puzdro"],
     ["Náhradné diely", "Elektro & senzory"]),
    (["nabíjačka pomocného", "nabijacka pomocneho", "pomocného štartovacieho",
      "pomocné štartovacie", "pomocne startovacie", "jump starter", "booster pack",
      "náhradný akumulátor pre pomocné", "nahradny akumulator pre pomocne",
      "čistiaca pištoľ na stlačený", "cistiaca pistol na stlaceny",
      "magnetický držiak smartfón", "magneticky drziak smartfon",
      "kontrolné zrkadlo", "kontrolne zrkadlo", "inspection mirror",
      "plošinu pre batériu", "plosinú pre bateriu", "plosinú pre batériu",
      "sada príslušenstva 27 kusov pre plošinu"],
     ["Náradie & Vybavenie", "Dielenské vybavenie"]),
    (["ochranná sada", "ochranna sada", "boot kit", "dust cover kit"],
     ["Náhradné diely", "Náprava & podvozok", "Tlmiče & pružiny"]),
    # VAG originál bez typového názvu — aspoň správne L1/L2.
    (["díl vag lišta", "dil vag lista", "vag lišta", "vag lista",
      "súprava montážnych dielov", "suprava montaznych dielov", "montážnych dielov"],
     ["Náhradné diely", "Karoséria & svetlá"]),
    (["díl vag veko", "dil vag veko", "vag veko", "oil filter cover"],
     ["Náhradné diely", "Filtre", "Olejové filtre"]),
    (["parkovacia tabuľ", "parkovacia tabul", "parking disc", "parkovacia známka", "parkovacia znamka",
      "nočná parkovacia", "nocna parkovacia"],
     ["Náhradné diely", "Karoséria & svetlá"]),
    # (duplicitné filter rules vyššie v bloku FILTRE — tu len catch-all slug)
    (["filtre"], ["Náhradné diely", "Filtre"]),
    (["alkaline", "alkalick", "zásady", "zasady", "acid (", "acid nix", "kyseliny", "solvent", "foamer",
      "super pro+", "activ x", "softwash", "mitt", "bubblewash", "alu xclean", "vivid black", "apc -", "bio power"],
     ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    # „plastic“ ako substring trafí viskoplastické (glej) — len celé frázy
    (["plastic care", "plastic polish", "plastic cleaner", "očistič plastov", "ocistic plastov",
      "oživovač plastov", "ozivovac plastov",
      "oživovač plastu a gumy exteriér", "ozivovac plastu a gumy exterier",
      "oživovač plastu a gumy - exteriér", "ozivovac plastu a gumy - exterier",
      "rubber refresher", "nanotechnológ", "autosklo",
      "floor strong"],
     ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    (["nover glass", "glass ", "kiribati", "aruba", "laguna", "proelite"], ["Autokozmetika & Detail", "Exteriér & umývanie"]),
    (["akcia", "zľava", "zlava", "sale", "promo"], ["Výhodné sady & Akcie", "Produkty v akcii"]),
]


# Hard junk — vyradiť z katalogu (draft), nie radiť do adaptérov
JUNK_NAME_HINTS = (
    "káva", "kava", "coffee", "test pro-tec", "test produkt", "test ",
)

def format_category_tree() -> str:
    lines = []
    for main, subs in CATEGORY_TREE.items():
        if subs:
            lines.append(f"- {main} -> [ {' | '.join(subs)} ]")
        else:
            lines.append(f"- {main}")
        for sub in subs:
            leaves = PART_LEAVES.get(sub) or []
            for leaf in leaves:
                lines.append(f"    - {main} > {sub} > {leaf}")
    return "\n".join(lines)


def _all_paths_index() -> List[List[str]]:
    paths: List[List[str]] = []
    for main, subs in CATEGORY_TREE.items():
        if not subs:
            paths.append([main])
            continue
        for sub in subs:
            leaves = PART_LEAVES.get(sub) or []
            if leaves:
                for leaf in leaves:
                    paths.append([main, sub, leaf])
            else:
                paths.append([main, sub])
    return paths


def sanitize_category_path(path: List[str]) -> List[str]:
    cleaned = [str(p).strip() for p in path if p and str(p).lower() not in ("null", "none", "")]
    if not cleaned:
        return ["Náhradné diely", "Filtre"]

    # Exact path match (2 alebo 3 úrovne)
    for known in _all_paths_index():
        if len(cleaned) >= len(known) and [c.lower() for c in cleaned[: len(known)]] == [k.lower() for k in known]:
            return known
        if len(cleaned) == len(known) and all(a.lower() == b.lower() for a, b in zip(cleaned, known)):
            return known

    main = cleaned[0]
    sub = cleaned[1] if len(cleaned) > 1 else None
    leaf = cleaned[2] if len(cleaned) > 2 else None

    if main in CATEGORY_TREE:
        allowed = CATEGORY_TREE[main]
        if not allowed:
            return [main]
        # case-insensitive sub
        resolved_sub = None
        if sub:
            for s in allowed:
                if s.lower() == sub.lower():
                    resolved_sub = s
                    break
        if not resolved_sub:
            resolved_sub = allowed[0]
        leaves = PART_LEAVES.get(resolved_sub) or []
        if leaves:
            if leaf:
                for lf in leaves:
                    if lf.lower() == leaf.lower():
                        return [main, resolved_sub, lf]
            # Neznámy leaf → ostaň na L2 (neforce-uj prvú viskozitu / diel)
            return [main, resolved_sub]
        return [main, resolved_sub]

    # Match podľa leaf / sub názvu
    needle = (leaf or sub or main).lower()
    for known in _all_paths_index():
        if known[-1].lower() == needle or needle in known[-1].lower():
            return known

    return ["Náhradné diely", "Filtre"]


def match_oil_viscosity(raw_data: dict) -> Optional[List[str]]:
    """Detekcia SAE viskozity v názve/SKU → Motorové oleje > 0W-20 …"""
    import re

    text = " ".join([
        str(raw_data.get("name") or ""),
        str(raw_data.get("sku") or ""),
        str(raw_data.get("brand") or ""),
    ]).lower()
    # Záhradné / reťazové / 2T oleje NESMÚ skončiť v auto leaf (5W-30, 10W-40…).
    # (Keyword rules by inak nikdy nevyhrali — viskozita má prioritu.)
    if any(
        x in text
        for x in (
            "garden", "chainsaw", "chain saw", "bar oil",
            "reťazový olej", "retazovy olej", "olej na reťaz", "olej na retaz",
            "pilový olej", "pilovy olej", "motorovej píly", "motorovej pily",
            "2t olej", "olej 2t", "2-taktný olej", "2-taktny olej",
            "dvojtaktný olej", "dvojtaktny olej", "two stroke oil", "2-stroke oil",
            "garden 2t", "2t syntet", "2t mineral",
        )
    ):
        return ["Motorové oleje & Kvapaliny", "Motorové oleje", "Ostatné viskozity"]
    # Preskoč prevodové / ATF / hypotetika „75W“
    if any(x in text for x in ("atf", "gear oil", "prevodov", "75w", "80w", "85w", "90w", "gl-4", "gl-5")):
        return None

    # Bežné SAE + menej časté (0W-16, 5W-50, …) → leaf alebo „Ostatné viskozity“
    m = re.search(
        r"\b(0w[-\s]?(?:8|12|16|20|30|40)|5w[-\s]?(?:20|30|40|50)|"
        r"10w[-\s]?(?:30|40|50|60)|15w[-\s]?(?:40|50)|20w[-\s]?(?:50|60))\b",
        text,
    )
    if m:
        raw = re.sub(r"[-\s]", "", m.group(1).upper())  # 0W20
        # 0W20 → 0W-20
        norm = re.sub(r"^(\d+)W(\d+)$", r"\1W-\2", raw)
        leaves = PART_LEAVES.get("Motorové oleje") or []
        if norm not in leaves:
            return ["Motorové oleje & Kvapaliny", "Motorové oleje", "Ostatné viskozity"]
        return ["Motorové oleje & Kvapaliny", "Motorové oleje", norm]

    # Monograde SAE 30/40/50 (bez W) — len pri olejovej kontexte
    if re.search(r"\b(?:sae|motor\s*oil|engine\s*oil|motorov)\b", text) and re.search(
        r"\b(?:sae\s*)?(?:30|40|50)\b", text
    ):
        return ["Motorové oleje & Kvapaliny", "Motorové oleje", "Ostatné viskozity"]
    return None


def extract_bulb_type_token(text: str) -> Optional[str]:
    """Vytiahni ECE typ žiarovky z názvu / URL / popisu (H7, H4, PX22D→H11…)."""
    import re

    t = (text or "").lower()
    # URL slugy Würth: …-H7-12V… / …-ZIAROVKA-H4-P43T…
    # Poradie: H11 pred H1, HB* pred H3/H4
    patterns = (
        (r"(?:^|[^a-z0-9])h\s*11(?:[^a-z0-9]|$)", "H11"),
        (r"(?:^|[^a-z0-9])px\s*22\s*d(?:[^a-z0-9]|$)", "H11"),  # pätica H11
        (r"(?:^|[^a-z0-9])hb\s*4(?:[^a-z0-9]|$)|\b9006\b", "HB4"),
        (r"(?:^|[^a-z0-9])hb\s*3(?:[^a-z0-9]|$)|\b9005\b", "HB3"),
        (r"(?:^|[^a-z0-9])h\s*7(?:[^a-z0-9]|$)", "H7"),
        (r"(?:^|[^a-z0-9])h\s*4(?:[^a-z0-9]|$)", "H4"),
        (r"(?:^|[^a-z0-9])h\s*3(?:[^a-z0-9]|$)", "H3"),
        (r"(?:^|[^a-z0-9])h\s*1(?:[^a-z0-9]|$)", "H1"),
        (r"(?:^|[^a-z0-9])w\s*5\s*w(?:[^a-z0-9]|$)", "W5W"),
        # P21W + dvojvláknová P21/5W (brzda/koncové)
        (r"(?:^|[^a-z0-9])p\s*21\s*/\s*5\s*w(?:[^a-z0-9]|$)", "P21W"),
        (r"(?:^|[^a-z0-9])py?\s*21\s*w(?:[^a-z0-9]|$)", "P21W"),
    )
    for pat, leaf in patterns:
        if re.search(pat, t):
            return leaf
    return None


def match_bulb_type(raw_data: dict) -> Optional[List[str]]:
    """Detekcia typu žiarovky (H4/H7…) → Svetlá & žiarovky > H7.

    Rovnaký pattern ako match_oil_viscosity: typ = leaf pod spoločným L2.
    Würth často nemá typ v short name — berieme aj supplier_url (slug …-H7-…).
    """
    import re

    text = " ".join([
        str(raw_data.get("name") or ""),
        str(raw_data.get("sku") or ""),
        str(raw_data.get("brand") or ""),
        str(raw_data.get("supplier_url") or ""),
        str(raw_data.get("url") or ""),
    ]).lower()

    # Žeraviace / zapaľovacie sviečky ≠ žiarovky (substring „žiarov“)
    if any(
        x in text
        for x in (
            "žeraviac", "zeraviac", "žeravia", "zeravia",
            "glow plug", "heater plug", "spark plug",
            "žiarovacia", "ziarovacia", "sviečka", "sviecka",
            "cievka zapaľ", "cievka zapal",
        )
    ):
        return None

    # Čističe / leštenie / UV lampy na lepenie skiel ≠ typ žiarovky do auta
    if any(
        x in text
        for x in (
            "čistenie svetlomet", "cistenie svetlomet", "lešten", "lesten",
            "headlight restor", "headlight clean",
            "uv led lampa", "uv lampa", "oprava čeln", "oprava celn",
            "opravu čeln", "opravu celn",
            "bezšnúrová uv", "bezsnurova uv",
        )
    ):
        return None

    # Reflexná páska „označenie obrysov“ ≠ žiarovka (substring „obrysov“)
    if any(
        x in text
        for x in (
            "označenie obrys", "oznacenie obrys", "contour mark", "reflective tape",
            "diamond grade", "3m diamond",
        )
    ):
        return None

    bulb_word = any(
        x in text
        for x in (
            "žiarovka", "ziarovka", "žiarovky", "ziarovky",
            "bulb",
            "halogénová žiarov", "halogenova ziarov", "halogénovú žiarov",
            "led žiar", "led ziar", "led bulb", "headlight bulb",
            # Würth SEO slug
            "ziarovka-", "-ziarovka", "halogenova-ziarovka", "xenonova-halogenova",
            "výbojka", "vybojka", "plynová výboj", "plynova vyboj",
        )
    )

    # Kompletné svetlomety / aktuátory ≠ leaf H7 (aj keď názov obsahuje H7/D3S)
    # napr. „H7+H9 svetlomet“, „D3S+H7+PW24W svetlomet“, „Motorček XENON svetlometu“
    assy = any(
        x in text
        for x in (
            "svetlomet", "světlomet", "svetlometu", "světlometu",
            "headlight assembly", "headlamp assembly", "head lamp unit",
            "lamp unit", "hlavný svetlomet", "hlavny svetlomet",
            "kompletné svetlo", "kompletne svetlo",
            "motorček", "motorcek", "headlight level", "headlamp level",
        )
    )
    if assy and not bulb_word:
        return None

    bulb_ctx = bulb_word or any(
        x in text
        for x in (
            "halogén", "halogen",
            "tail light", "osvetlen", "komorov",
            "obrysové svetlo", "obrysove svetlo",
            "pracovné svetlo", "pracovne svetlo", "kluster",
            "led lamp", "výbojk", "vybojk",
            "xenon", "xenón",
            "svetlo, 12", "svetlo, 24",
        )
    )

    base = ["Náhradné diely", "Svetlá & žiarovky"]
    leaves = PART_LEAVES.get("Svetlá & žiarovky") or []

    # Pravá xenón výbojka (D1S–D4S) PRED H7 tokenom — „D2S“ ≠ halogen leaf.
    if re.search(r"(?:^|[^a-z0-9])d[1-4]\s*s(?:[^a-z0-9]|$)", text) or any(
        x in text for x in ("výbojka", "vybojka", "plynová výboj", "plynova vyboj")
    ):
        return base + ["Xenón / výbojky"]

    ledish = bool(re.search(r"\bled\b|ledriving|legal\s*led|legáln\w*\s*led", text))
    halogen_word = "halogen" in text or "halogén" in text
    token = extract_bulb_type_token(text)
    # LED retrofit diaľkových/potkávačiek (H7 LED…) → LED leaf; W5W/P21W LED ostáva typ.
    # ECE token stačí — názov nemusí obsahovať slovo „žiarovka“ (OSRAM … legal LED).
    _led_headlight = {"H1", "H3", "H4", "H7", "H11", "HB3", "HB4"}
    if ledish and not halogen_word and token in _led_headlight:
        return base + ["LED žiarovky"]

    if token:
        if token in leaves:
            return base + [token]
        return base + ["Ostatné žiarovky"]

    if bulb_ctx and ledish and not halogen_word:
        return base + ["LED žiarovky"]

    # Xenónový efekt na halogéne / komorové svetlá bez ECE typu → catch-all
    if bulb_ctx:
        return base + ["Ostatné žiarovky"]

    return None


def _strip_category_echo(text: str) -> str:
    """Odstráň AI/template echo kategórie z popisu — inak description zacyklí
    zlú leaf kategóriu (náter podvozka → „prevodové oleje“ v short_desc/tabuľke)."""
    import re

    t = text or ""
    t = re.sub(
        r"zaraden[ée]\s+v\s+kateg[oó]rii\s+[^.<]{0,100}\.?",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    # Breadcrumb v HTML: „Motorové oleje &amp; Kvapaliny › Prevodové oleje“
    t = re.sub(
        r"(?:motorov[eé]\s+olej[^<]{0,40}|autoch[eé]mia[^<]{0,40}|dielensk[^<]{0,40})"
        r"(?:&amp;|&)[^<]{0,40}(?:›|>)[^<]{0,60}",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    return t


def match_keyword_category(raw_data: dict) -> Optional[List[str]]:
    """Match na name/sku/brand NAJPRV — description často obsahuje šum."""
    # Viskozita oleja má prioritu pred generickým „engine oil“
    vis = match_oil_viscosity(raw_data)
    if vis:
        return sanitize_category_path(vis)

    # Typ žiarovky (H4/H7…) — pred generickým „žiarovka / halogen“
    bulb = match_bulb_type(raw_data)
    if bulb:
        return sanitize_category_path(bulb)

    primary = " ".join([
        str(raw_data.get("name") or ""),
        str(raw_data.get("sku") or ""),
        str(raw_data.get("brand") or ""),
    ]).lower()
    secondary = _strip_category_echo(" ".join([
        str(raw_data.get("short_description") or ""),
        str(raw_data.get("description") or ""),
    ]).lower())

    # Tieto L1 nikdy z description — AI copy / breadcrumbs / „egr“ v texte falšujú match
    skip_secondary_mains = {
        "náradie & vybavenie",
        "motorové oleje & kvapaliny",
        "náhradné diely",
    }
    # Krátke / nebezpečné keywordy — len z názvu (description ich má ako šum:
    # olej ESP spomína „pevných častíc“, PSF „rim“, …)
    secondary_blocklist = {
        "egr", "dpf", "fap", "dpfcc", "oxicat", "eolys", "cerine",
        "pevných častíc", "pevnych castic", "filtra pevných", "filtra pevnych",
        "diesel particulate", "rim", "disk", "pneu", "tire", "tyre",
        "benzín", "benzin", "petrol ", "expanzn", "nasávania", "nasavania",
        "tesniac", "gasket", "tesnenie", "plastic", "adblue", "scr fluid",
        # LED pásy majú „lepidlo na spodnej strane“ v popise ≠ Tesnenia & Lepidlá
        "lepidlo", "lepiaca", "lepiac",
    }

    for keywords, path in CATEGORY_KEYWORD_RULES:
        if any(k in primary for k in keywords):
            return sanitize_category_path(list(path))
    for keywords, path in CATEGORY_KEYWORD_RULES:
        if path and path[0].lower() in skip_secondary_mains:
            continue
        # Hard-part filtre len z názvu/SKU — AI copy často spomína „olejový filter“
        if path and any(str(p).lower() == "filtre" for p in path):
            continue
        hits = [k for k in keywords if k in secondary and k not in secondary_blocklist]
        if hits:
            return sanitize_category_path(list(path))
    return None


def map_legacy_category(name: str) -> Optional[List[str]]:
    key = (name or "").strip().lower()
    path = LEGACY_CATEGORY_MAP.get(key)
    return sanitize_category_path(path) if path else None


def all_allowed_names() -> set:
    names = set(CATEGORY_TREE.keys())
    for subs in CATEGORY_TREE.values():
        names.update(subs)
    for leaves in PART_LEAVES.values():
        names.update(leaves)
    return names


def category_nav_meta() -> List[Dict[str, str]]:
    """Meta pre UI karty kategórií (slug hint + short label)."""
    return [
        {"name": "Motorové oleje & Kvapaliny", "label": "Oleje & kvapaliny", "icon": "oil", "blurb": "0W20, ATF, DOT, G12"},
        {"name": "Autochémia & Aditíva", "label": "Autochémia", "icon": "flask", "blurb": "DPF, aditíva, ochrana"},
        {"name": "Autokozmetika & Detail", "label": "Autokozmetika", "icon": "sparkle", "blurb": "Umývanie, vosky, interiér"},
        {"name": "Dielenská chémia & Údržba", "label": "Dielenská chémia", "icon": "wrench-chem", "blurb": "Mazivá, čističe, spreje"},
        {"name": "Stavebná chémia", "label": "Stavebná chémia", "icon": "chem", "blurb": "PU peny, hydroizol, tmely"},
        {"name": "Náhradné diely", "label": "Autodiely", "icon": "parts", "blurb": "Filtre, brzdy, H4/H7…"},
        {"name": "Náradie & Vybavenie", "label": "Náradie", "icon": "tools", "blurb": "Podľa typu práce"},
        {"name": "Výhodné sady & Akcie", "label": "Sady & akcie", "icon": "tag", "blurb": "Sety a výhodné balenia"},
    ]


# --- 2026-10-13: karoséria/dvere + CZ názvy filtrov z LKQ/Szekop (pred všeobecnými pravidlami) ---
_KAROSERIA = ["Náhradné diely", "Karoséria & svetlá"]
CATEGORY_KEYWORD_RULES[:0] = [
    (["filtr, vzduch v interiéru", "filtr, vzduch v interieru", "vzduch v interiéru", "vzduch v interieru"],
     ["Náhradné diely", "Filtre", "Kabínové filtre"]),
    (["vzduchový filtr", "vzduchovy filtr"], ["Náhradné diely", "Filtre", "Vzduchové filtre"]),
    (["palivový filtr", "palivovy filtr"], ["Náhradné diely", "Filtre", "Palivové filtre"]),
    (["brzdový kotouč", "brzdovy kotouc"], ["Náhradné diely", "Brzdy", "Brzdové kotúče"]),
    (["generátor", "generator", "alternátor", "alternator"],
     ["Náhradné diely", "Elektro & senzory", "Štartéry & alternátory"]),
    (["tryska ostrekovača", "tryska ostrekovaca", "ostrekovač", "ostrekovac", "stieracie tesnenie"],
     ["Náhradné diely", "Karoséria & svetlá", "Stierače"]),
    (["zámok", "zamok", "zámku", "zamku", "kľučka", "klucka", "kľučk", "kluck", "dveřní madlo", "madlo",
      "rukoväť výklopných", "rukovat vyklopnych", "ovládanie dverí", "ovladanie dveri"],
     ["Náhradné diely", "Karoséria & svetlá", "Zámky & kľúče"]),
    (["tesnenie dverí", "tesnenie dveri", "tesnenie predných", "tesnenie zadných", "tesnenie prednych",
      "tesnenie zadnych", "tesnenie skla", "tesnenie okolo", "tesnenia okna", "upchávka tesnenia",
      "záves dverí", "zaves dveri", "záves veka", "zaves veka", "pánt", "pant zadn",
      "doraz kapoty"],
     _KAROSERIA),
]
