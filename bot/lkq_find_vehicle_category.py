#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nájdi LKQ Catalog URL pre Make → Model → Motor → kategória dielov.

Príklad:
  python bot/lkq_find_vehicle_category.py \\
    --make skoda --model 'superb iii' --engine '2.0 TDI' --kw 150hp --category filtre

Výstup: jedna Catalog URL (slug + path IDs) vhodná pre
  import_pipeline.py --supplier lkq --category-url …
"""

from __future__ import annotations

import argparse
import os
try:  # loginy z HQ (hq.smartgaraz.sk) majú prednosť pred secrets
    from suppliers.hq_credentials import export_logins as _hq_export
    _hq_export("lkq")
except Exception:
    pass

import sys
import time
from typing import Any, Callable, List, Optional, Tuple, TypeVar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from suppliers.lkq import LkqSupplier  # noqa: E402

BASE = "https://www.lkq.sk"
NAHRADNE = "/Catalog/osobny-automobil-nahradne-diely/39849642;39850140"
HTTP_TIMEOUT = int(os.environ.get("LKQ_HTTP_TIMEOUT", "60"))
T = TypeVar("T")


def _retry(fn: Callable[[], T], attempts: int = 4, label: str = "lkq") -> T:
    last: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — sieťové výpadky LKQ
            last = exc
            # 4xx (404 neexistujúca vetva) sa opakovaním neopraví → hneď ďalej
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code is not None and 400 <= int(code) < 500 and int(code) != 429:
                raise
            wait = 2 ** i
            print(f"# retry {label} {i+1}/{attempts} after {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    assert last is not None
    raise last


def _flatten_links(obj: Any, out: Optional[List[Tuple[str, str]]] = None) -> List[Tuple[str, str]]:
    out = out if out is not None else []
    if isinstance(obj, list):
        for item in obj:
            _flatten_links(item, out)
    elif isinstance(obj, dict):
        text = (obj.get("Text") or "").strip()
        link = obj.get("Link") or ""
        if text and link:
            out.append((text, link))
        for val in obj.values():
            if isinstance(val, (list, dict)):
                _flatten_links(val, out)
    return out


def _last_id(link: str) -> str:
    return link.rstrip("/").split("/")[-1].split(";")[-1]


def _tokens_match(blob: str, needle: str) -> bool:
    """Každý token z needle musí sedieť (vii ≠ viii; 8v→8v1; kotuce≈kotucova)."""
    import re
    import unicodedata

    def _fold(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return s.lower()

    b = _fold(blob)
    aliases = {
        "kotuce": "kotuc",
        "kotuc": "kotuc",
        "brzdy": "brzd",
        "brzd": "brzd",
        "dosticky": "dostic",
        "filtre": "filtr",
        # LKQ leaf: …-stierace / „Stierače“ (nie „stieracie lišty“)
        "stieracie": "stierac",
        "stierace": "stierac",
        "stierac": "stierac",
        "zapalovanie": "zapalovanie",
        "zapaľovanie": "zapalovanie",
        "tlmice": "tlmic",
        "tlmiče": "tlmic",
        "tlmic": "tlmic",
        "loziska": "lozisk",
        "ložiská": "lozisk",
        "lozisk": "lozisk",
        "snimace": "snimac",
        "snímače": "snimac",
        "snimac": "snimac",
        "remene": "remen",
        "remen": "remen",
        # Imobilizér / kľúče / keyless (LKQ nested under elektro / dvere)
        "imobilizer": "imobil",
        "imobilizér": "imobil",
        "immobiliser": "imobil",
        "immobilizer": "imobil",
        "imobil": "imobil",
        "keyless": "keyless",
        "zamykanie": "zamykan",
        "centralne": "central",
        "centrálne": "central",
        "central": "central",
        "alarm": "alarm",
        "zamky": "zamk",
        "zámky": "zamk",
        "zamok": "zamk",
        "zámok": "zamk",
        "kluky": "klik",
        "kliky": "klik",
        "kluce": "kluc",
        "kľúče": "kluc",
        "kluc": "kluc",
        "kľúč": "kluc",
        "skrinky": "skrink",
        "skrinka": "skrink",
        "spinace": "spinac",
        "spínače": "spinac",
        "spinac": "spinac",
        "spínač": "spinac",
        "riadiacej": "riadiac",
        "riadiace": "riadiac",
        "jednotky": "jednotk",
    }
    folded_needle = _fold(needle)
    # hyphenované frázy (c-class, a-class) — vyžaduj „c-class“ alebo „c class“
    for phrase in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", folded_needle):
        spaced = phrase.replace("-", " ")
        if phrase not in b and spaced not in b:
            return False
    for tok in re.findall(r"[a-z0-9]+", folded_needle):
        stem = aliases.get(tok, tok)
        if re.fullmatch(r"[ivxlcdm]+", tok):
            if not re.search(rf"(?<![ivxlcdm]){re.escape(tok)}(?![ivxlcdm])", b):
                return False
        elif re.fullmatch(r"[a-z0-9]{2,4}", tok) and re.search(r"\d", tok):
            # platform kód: 8v → 8v1/8vk; 5f → 5f1
            if not re.search(rf"(?<![a-z0-9]){re.escape(tok)}[a-z0-9]*", b):
                return False
        elif len(tok) == 1:
            # jedno-písmenové (BMW „3“, MB „c“ v c-class) — už pokryté frázou / digit token
            if tok.isdigit():
                if not re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", b):
                    return False
            continue
        elif stem != tok:
            if stem not in b:
                return False
        elif not re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", b):
            return False
    return True


def _generation_recency(text: str) -> int:
    """Vyššie = novšie generácia ([08/14-] MM/YY alebo 2014)."""
    import re

    years: List[int] = []
    for mm, yy in re.findall(r"(\d{2})/(\d{2})", text):
        y = int(yy)
        years.append(1900 + y if y >= 70 else 2000 + y)
    for y in re.findall(r"(?:19|20)\d{2}", text):
        years.append(int(y))
    return max(years) if years else 0


def _rank(links: List[Tuple[str, str]], needle: str, extra: str = "") -> List[Tuple[str, str]]:
    """Zoradené kandidáty (novšia generácia prvá)."""
    e = extra.lower().strip()
    scored: List[Tuple[int, int, int, str, str]] = []
    for text, link in links:
        blob = f"{text} {link}".lower()
        if not _tokens_match(blob, needle):
            continue
        if e and e not in blob:
            continue
        scored.append((-_generation_recency(text), len(text), text.lower().count(" "), text, link))
    scored.sort()
    return [(t, l) for _, _, _, t, l in scored]


def _pick(links: List[Tuple[str, str]], needle: str, extra: str = "") -> Tuple[str, str]:
    ranked = _rank(links, needle, extra)
    if not ranked:
        raise SystemExit(f"Nenašiel som match pre {needle!r} extra={extra!r}")
    return ranked[0]


# Rodičovské vetvy, kde LKQ typicky schová imobil/kľúče/keyless (nie top-level).
_NEST_PARENT_HINTS = (
    "elektro",
    "ostatn",
    "dver",
    "zapal",
    "zapaľ",
    "karoser",
    "zamk",
    "alarm",
    "zamykan",
    "imobil",
    "keyless",
    "spinac",
    "skrink",
    "riadiac",
)


def _parent_priority(text: str, link: str) -> int:
    """Nižšie = skôr prehľadávať (relevantné vetvy pred celým stromom)."""
    blob = f"{text} {link}".lower()
    for i, hint in enumerate(_NEST_PARENT_HINTS):
        if hint in blob:
            return i
    return 100


def _find_category_deep(
    cat_links: List[Tuple[str, str]],
    needle: str,
    *,
    open_cat: Callable[[str], None],
    categories: Callable[[str], List[Tuple[str, str]]],
    max_depth: int = 4,
) -> Tuple[str, str]:
    """Top-level match; ak nie, BFS do vnorených Categories (imobil/zámky/…)."""
    ranked = _rank(cat_links, needle)
    if ranked:
        return ranked[0]

    from collections import deque

    # najprv sľubné vetvy (elektro / dvere / zapaľovanie)
    seeds = sorted(cat_links, key=lambda tl: _parent_priority(tl[0], tl[1]))
    queue: deque[Tuple[str, str, int]] = deque((t, l, 0) for t, l in seeds)
    seen: set[str] = set()
    budget = int(__import__("os").environ.get("SG_LKQ_BFS_MAX", "30"))
    while queue and budget > 0:
        budget -= 1
        _text, link, depth = queue.popleft()
        lid = _last_id(link)
        if not lid or lid in seen or depth >= max_depth:
            continue
        seen.add(lid)
        try:
            open_cat(link)
            children = categories(lid)
        except Exception as exc:  # noqa: BLE001 — leaf / prázdna vetva
            print(f"# nest skip depth={depth}: {exc}", file=sys.stderr)
            continue
        if not children:
            continue
        ranked = _rank(children, needle)
        if ranked:
            return ranked[0]
        # deti: znova preferuj hinty
        kids = sorted(children, key=lambda tl: _parent_priority(tl[0], tl[1]))
        for ctext, clink in kids:
            queue.append((ctext, clink, depth + 1))
    raise SystemExit(f"Nenašiel som (ani nested) match pre {needle!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--make", required=True, help="napr. skoda / volkswagen")
    ap.add_argument("--model", required=True, help="napr. 'superb iii' / 'fabia iii'")
    ap.add_argument("--engine", required=True, help="napr. '2.0 TDI' / '1.0 TSI'")
    ap.add_argument("--kw", default="", help="doplnkový filter motora (150hp, 70kw, …)")
    ap.add_argument(
        "--category",
        default="filtre",
        help="leaf kategória (filtre, kotucova-brzda, …) — match v Text/Link",
    )
    ap.add_argument(
        "--exclude",
        default=(
            "4x4,combi,alltrack,cabrio,sportback,variant,e-tech,"
            "limuzína,limuzina,coupé,coupe,praktik,van,dodávka,dodavka,pick-up,pickup"
        ),
        help="čiarkou oddelené exclude v Text/Link (ak needle sám nežiada variant)",
    )
    args = ap.parse_args()

    user = os.environ.get("LKQ_USER") or os.environ.get("LKQ_USERNAME")
    password = os.environ.get("LKQ_PASSWORD")
    if not user or not password:
        raise SystemExit("Chýba LKQ_USER / LKQ_PASSWORD")

    supplier = LkqSupplier(credentials={"user": user, "password": password})
    if not supplier.ensure_login():
        raise SystemExit("LKQ login zlyhal")

    session = supplier.http.session
    # LKQ Garage Catalogs/* bez Referer na rodičovskú stránku vráti [] (silent fail).
    referer = BASE + NAHRADNE

    def _headers() -> dict:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": referer,
            "Origin": BASE,
        }

    def open_cat(link: str) -> None:
        nonlocal referer
        url = BASE + link if link.startswith("/") else link

        def _get() -> None:
            session.get(url, timeout=HTTP_TIMEOUT, headers={"Referer": referer}).raise_for_status()

        _retry(_get, label=f"GET {url[-48:]}")
        referer = url

    def catalogs(last: str) -> List[Tuple[str, str]]:
        def _post() -> List[Tuple[str, str]]:
            resp = session.post(
                f"{BASE}/Catalog/Catalogs/{last}", json={}, headers=_headers(), timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            links = _flatten_links(resp.json())
            if not links:
                raise RuntimeError(f"Catalogs/{last} empty (check Referer/session)")
            return links

        return _retry(_post, label=f"Catalogs/{last}")

    def motors(last: str, *, require: bool = True) -> List[Tuple[str, str]]:
        def _post() -> List[Tuple[str, str]]:
            resp = session.post(
                f"{BASE}/Catalog/Motors/{last}", json={}, headers=_headers(), timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            links = _flatten_links(resp.json())
            if require and not links:
                raise RuntimeError(f"Motors/{last} empty (check Referer/session)")
            return links

        return _retry(_post, label=f"Motors/{last}")

    def categories(last: str, *, require: bool = True) -> List[Tuple[str, str]]:
        def _post() -> List[Tuple[str, str]]:
            resp = session.post(
                f"{BASE}/Catalog/Categories/{last}",
                json={},
                headers=_headers(),
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            links = _flatten_links(resp.json())
            if require and not links:
                raise RuntimeError(f"Categories/{last} empty (check Referer/session)")
            return links

        return _retry(_post, label=f"Categories/{last}")

    excludes = [x.strip().lower() for x in (args.exclude or "").split(",") if x.strip()]
    # ak používateľ explicitne žiada combi/alltrack v --model, nevylučuj
    model_l = args.model.lower()
    excludes = [ex for ex in excludes if ex not in model_l]

    open_cat(NAHRADNE)
    make_text, make_link = _pick(catalogs("39850140"), args.make)
    open_cat(make_link)
    model_links = catalogs(_last_id(make_link))
    # prefer non-combi/alltrack unless model string asks for it
    filtered_models = []
    for text, link in model_links:
        blob = f"{text} {link}".lower()
        if any(ex in blob for ex in excludes):
            continue
        filtered_models.append((text, link))
    if filtered_models:
        model_links = filtered_models
    # skúšaj kandidátov (Praktik/van často bez motorov v Garage)
    model_candidates = _rank(model_links, args.model)
    if not model_candidates:
        raise SystemExit(f"Nenašiel som match pre {args.model!r} extra=''")

    engine_text = engine_link = model_text = model_link = ""
    filtered: List[Tuple[str, str]] = []
    for model_text, model_link in model_candidates:
        open_cat(model_link)
        try:
            engine_links = motors(_last_id(model_link), require=False)
        except Exception as exc:  # noqa: BLE001
            print(f"# skip model {model_text.strip()[:60]}: {exc}", file=sys.stderr)
            continue
        if not engine_links:
            print(f"# skip model (no motors): {model_text.strip()[:70]}", file=sys.stderr)
            continue
        filtered = []
        for text, link in engine_links:
            # len Text — Link obsahuje path IDs (…220…), false-positive na „220“
            label = text.lower()
            if args.engine.lower() not in label:
                continue
            if args.kw and args.kw.lower() not in label:
                continue
            if any(ex in label for ex in excludes):
                continue
            filtered.append((text, link))
        if not filtered:
            filtered = [
                (t, l) for t, l in engine_links if args.engine.lower() in t.lower()
            ]
        if filtered:
            engine_text, engine_link = filtered[0]
            break
        print(
            f"# skip model (no engine {args.engine!r}): {model_text.strip()[:70]}",
            file=sys.stderr,
        )
    if not filtered or not engine_link:
        raise SystemExit(f"Žiadny motor pre {args.engine!r} kw={args.kw!r}")
    open_cat(engine_link)

    cat_links = categories(_last_id(engine_link))
    # Imobilizér / zámky / spínacie skrinky sú vnorené pod Elektro / Dvere / …
    cat_text, cat_link = _find_category_deep(
        cat_links,
        args.category,
        open_cat=open_cat,
        categories=lambda last: categories(last, require=False),
    )
    full = BASE + cat_link
    print(full)
    print(
        f"# {make_text.strip()} · {model_text.strip()} · {engine_text.strip()} · {cat_text.strip()}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
