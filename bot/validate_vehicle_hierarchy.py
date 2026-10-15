#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validácia SmartGaráž vehicle taxonómie — žiadne cudzie vehicle ID.

  python bot/validate_vehicle_hierarchy.py
  python bot/validate_vehicle_hierarchy.py --path bot/data/vehicle_hierarchy_sk.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT = ROOT / "data" / "vehicle_hierarchy_sk.json"

# Zakázané vzory v celom JSON (case-insensitive)
FORBIDDEN_PATTERNS = [
    (r"\btecdoc\b", "TecDoc mention in data payload (policy note must stay in provenance.forbidden only)"),
    (r"\bk\s*-?\s*type\b", "kType / k-type vehicle key"),
    (r"\bktypenr\b", "TecDoc ktypenr"),
    (r"\blinkage\b", "TecDoc linkage"),
    (r"\binter\s*-?\s*cars?\b", "Inter Cars vehicle taxonomy"),
    (r"\bautodoc\b", "Autodoc vehicle taxonomy"),
    (r"\bherth\+?\s*buss\b", "Herth+Buss / TecDoc ecosystem id"),
]

# V provenance.forbidden_inputs je spomenutie TecDoc OK — kontroluj len makes/make_index
FORBIDDEN_IN_TREE = [
    re.compile(p, re.I) for p, _ in FORBIDDEN_PATTERNS
]


def _walk_strings(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(k, path + f".key")
            yield from _walk_strings(v, path + f".{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + f"[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def validate(data: dict) -> list[str]:
    errors: list[str] = []
    if data.get("version", 0) < 2:
        errors.append("version must be >= 2 (SG-owned schema with provenance)")
    if data.get("owner") != "SmartGaráž":
        errors.append("owner must be 'SmartGaráž'")
    prov = data.get("provenance") or {}
    if not isinstance(prov, dict) or not prov.get("forbidden_inputs"):
        errors.append("provenance.forbidden_inputs missing")
    if "makes" not in data or not isinstance(data["makes"], dict) or not data["makes"]:
        errors.append("makes tree empty")

    make_index = data.get("make_index") or {}
    makes = data.get("makes") or {}

    for make, models in makes.items():
        if not isinstance(models, dict):
            errors.append(f"make {make}: models must be object")
            continue
        mi = make_index.get(make) or {}
        if not str(mi.get("sg_id") or "").startswith("sgv-make-"):
            errors.append(f"make {make}: missing make_index.sg_id sgv-make-*")
        for model, meta in models.items():
            if not isinstance(meta, dict):
                errors.append(f"{make}/{model}: meta must be object")
                continue
            sg_id = str(meta.get("sg_id") or "")
            if not sg_id.startswith("sgv-model-"):
                errors.append(f"{make}/{model}: missing sg_id sgv-model-*")
            engines = meta.get("engines")
            if not isinstance(engines, list) or not engines:
                errors.append(f"{make}/{model}: engines must be non-empty list of labels")
            gens = meta.get("generations")
            if gens is not None:
                if not isinstance(gens, list):
                    errors.append(f"{make}/{model}: generations must be a list")
                else:
                    for i, g in enumerate(gens):
                        if not isinstance(g, dict):
                            errors.append(f"{make}/{model}/generations[{i}]: must be object")
                            continue
                        gid = str(g.get("sg_id") or "")
                        if not gid.startswith("sgv-gen-"):
                            errors.append(f"{make}/{model}/gen[{i}]: missing sg_id sgv-gen-*")
                        if not str(g.get("label") or "").strip():
                            errors.append(f"{make}/{model}/gen[{i}]: empty label")
                        for yk in ("year_from", "year_to"):
                            if g.get(yk) is not None and not isinstance(g.get(yk), int):
                                errors.append(f"{make}/{model}/gen[{i}]: {yk} must be int")
                        ge = g.get("engines")
                        if ge is not None and (
                            not isinstance(ge, list) or not all(isinstance(x, str) for x in ge)
                        ):
                            errors.append(f"{make}/{model}/gen[{i}]: engines must be string list")
                        for bad_key in (
                            "tecdoc_id", "ktype", "kType", "ktypenr", "linkage_id",
                            "lkq_vehicle_id", "szekop_vehicle_id", "intercars_id", "vehicle_id",
                        ):
                            if bad_key in g:
                                errors.append(f"{make}/{model}/gen[{i}]: forbidden key {bad_key}")
            # Foreign ID fields must not exist
            for bad_key in (
                "tecdoc_id", "ktype", "kType", "ktypenr", "linkage_id",
                "lkq_vehicle_id", "szekop_vehicle_id", "intercars_id", "vehicle_id",
            ):
                if bad_key in meta:
                    errors.append(f"{make}/{model}: forbidden key {bad_key}")

    # Scan tree strings (exclude provenance text which lists forbidden names on purpose)
    tree_only = {"makes": makes, "make_index": make_index}
    for path, s in _walk_strings(tree_only):
        for cre in FORBIDDEN_IN_TREE:
            if cre.search(s):
                # allow sgv-* only
                errors.append(f"forbidden pattern in {path}: {s[:80]!r}")
                break

    return errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(DEFAULT))
    args = ap.parse_args()
    path = Path(args.path)
    if not path.is_file():
        print(f"FAIL: missing {path}", file=sys.stderr)
        sys.exit(2)
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate(data)
    if errors:
        print(f"FAIL: {path} ({len(errors)} issues)")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    n_makes = len(data.get("makes") or {})
    n_models = sum(len(v) for v in (data.get("makes") or {}).values())
    print(f"OK: {path} makes={n_makes} models={n_models} version={data.get('version')}")


if __name__ == "__main__":
    main()
