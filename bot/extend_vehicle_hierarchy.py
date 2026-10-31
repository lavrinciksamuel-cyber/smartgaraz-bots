#!/usr/bin/env python3
"""Doplní vlastnú SG hierarchiu vozidiel (vehicle_hierarchy_sk.json) o modely
zo seedu bot/data/vehicle_seed_cn.csv (čínske značky, verejné trhové fakty:
názov značky/modelu, roky uvedenia na EU trh, pohon).

Politika: bot/data/VEHICLE_TAXONOMY_POLICY.md + CATALOG_LICENSING.md
 - iba vlastné sgv-* ID, žiadne TecDoc / dodávateľské vehicle ID,
 - iba pridáva (existujúce modely nikdy neprepisuje),
 - motory = bežné trhové označenia (ENGINE_LABELS) alebo pohon (BEV/PHEV…).

  python bot/extend_vehicle_hierarchy.py --dry-run
  python bot/extend_vehicle_hierarchy.py --apply --priority A
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TREE = ROOT / "data" / "vehicle_hierarchy_sk.json"
SEED = ROOT / "data" / "vehicle_seed_cn.csv"

# Verejné trhové označenia pohonných jednotiek (katalógy výrobcov / tlač).
ENGINE_LABELS = {
    "sgv-model-mg-zs": ["1.0 T-GDI", "1.5 VTi", "1.5 Hybrid+", "BEV"],
    "sgv-model-mg-3": ["1.5 VTi", "1.5 Hybrid+"],
    "sgv-model-mg-hs": ["1.5 T-GDI", "1.5 PHEV", "1.5 Hybrid+"],
    "sgv-model-mg-4": ["BEV"],
    "sgv-model-byd-seal-u": ["1.5 DM-i", "BEV"],
    "sgv-model-byd-atto-2": ["BEV", "1.5 DM-i"],
    "sgv-model-byd-seal": ["BEV"],
    "sgv-model-byd-dolphin-surf": ["BEV"],
    "sgv-model-byd-atto-3": ["BEV"],
    "sgv-model-byd-dolphin": ["BEV"],
    "sgv-model-byd-sealion-7": ["BEV"],
    "sgv-model-jaecoo-7": ["1.6 T-GDI", "1.5 PHEV"],
    "sgv-model-jaecoo-5": ["1.6 T-GDI", "Hybrid", "BEV"],
    "sgv-model-omoda-5": ["1.6 T-GDI", "1.5 T-GDI", "BEV"],
    "sgv-model-omoda-9": ["1.5 PHEV"],
    "sgv-model-leapmotor-t03": ["BEV"],
    "sgv-model-leapmotor-b10": ["BEV"],
    "sgv-model-leapmotor-c10": ["BEV", "REEV"],
    "sgv-model-xpeng-g6": ["BEV"],
}
PT_LABEL = {"ICE": "Benzín", "HEV": "Hybrid", "PHEV": "PHEV", "BEV": "BEV", "EREV": "REEV"}


def _engines(row: dict) -> list:
    sg = row["model_sg"]
    if sg in ENGINE_LABELS:
        return list(ENGINE_LABELS[sg])
    out = []
    for p in (row.get("powertrain") or "").replace(" ", "").split("/"):
        lab = PT_LABEL.get(p.upper())
        if lab and lab not in out:
            out.append(lab)
    return out


def _year(v: str):
    try:
        return int(str(v).strip()[:4])
    except Exception:
        return None


def extend(tree: dict, rows: list, priorities: set) -> dict:
    makes = tree.setdefault("makes", {})
    idx = tree.setdefault("make_index", {})
    existing = {m.get("sg_id") for mm in makes.values() for m in mm.values() if isinstance(m, dict)}
    added, skipped = [], []
    this_year = dt.date.today().year
    for r in rows:
        if (r.get("priority") or "").strip().upper() not in priorities:
            continue
        make, model, msg, ksg = r["make"].strip(), r["model"].strip(), r["model_sg"].strip(), r["make_sg"].strip()
        if "(" in model or "neover" in (r.get("powertrain") or "") or not msg.startswith("sgv-model-") or not ksg.startswith("sgv-make-"):
            skipped.append(f"{make} {model}: neoverené / bez sgv ID")
            continue
        if msg in existing:
            continue
        engines = _engines(r)
        if not engines:
            skipped.append(f"{make} {model}: bez pohonu")
            continue
        y0 = _year(r.get("eu_market_from")) or None
        y1 = _year(r.get("year_to")) or this_year
        node = {
            "sg_id": msg,
            "engines": engines,
            "year_from": y0,
            "year_to": y1,
            "priority": (r.get("priority") or "").strip().upper(),
            "provenance": "SG kurácia – verejné trhové fakty (seed vehicle_seed_cn.csv)",
        }
        if y0 is None:
            node.pop("year_from")
        makes.setdefault(make, {})[model] = node
        if make not in idx:
            idx[make] = {"sg_id": ksg, "aliases": sorted({make.upper(), make.lower()} - {make})}
        existing.add(msg)
        added.append(f"{make} {model}")
    return {"added": added, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--priority", default="A", help="čiarkou, napr. A,B")
    a = ap.parse_args()
    tree = json.loads(TREE.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(SEED.open(encoding="utf-8"), delimiter=";"))
    res = extend(tree, rows, {p.strip().upper() for p in a.priority.split(",") if p.strip()})
    sys.path.insert(0, str(ROOT))
    from validate_vehicle_hierarchy import validate  # noqa: E402
    errs = validate(tree)
    res["validation_errors"] = errs[:20]
    if a.apply and res["added"] and not errs:
        TREE.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        res["written"] = True
    print("SG_RESULT " + json.dumps({"added": len(res["added"]), "skipped": len(res["skipped"]),
                                     "errors": len(errs), "written": bool(res.get("written"))}, ensure_ascii=False))
    for k in ("added", "skipped", "validation_errors"):
        for x in res[k]:
            print(f"{k}: {x}")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
