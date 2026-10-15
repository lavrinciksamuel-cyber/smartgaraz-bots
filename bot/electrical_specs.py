#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extrakcia elektrických parametrov batérie/alternátora (Lavro SGV — nie TecDoc).

Voľný text (názov, atribúty, popis) → štruktúrované meta:
  ``_sg_batt_ah``, ``_sg_batt_cca_a``, ``_sg_batt_voltage_v``, ``_sg_batt_tech``
  ``_sg_alt_amps``, ``_sg_alt_voltage_v``
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _blob_from_raw(raw: Dict[str, Any]) -> str:
    parts: List[str] = [
        str(raw.get("name") or ""),
        str(raw.get("sku") or ""),
        str(raw.get("brand") or ""),
        str(raw.get("description") or ""),
        str(raw.get("short_description") or ""),
    ]
    attrs = raw.get("attributes") or {}
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            parts.append(f"{k} {v}")
    return " ".join(parts)


def extract_battery_specs(text: str) -> Dict[str, Any]:
    """Kapacita Ah, CCA/štartovací prúd A, napätie V, technológia AGM/EFB."""
    t = str(text or "")
    out: Dict[str, Any] = {}
    # 12V / 12 V
    vm = re.search(r"\b(12|24)\s*V\b", t, re.I)
    if vm:
        out["voltage_v"] = int(vm.group(1))
    # 60Ah / 60 Ah / 60AH
    am = re.search(r"\b(\d{2,3})\s*A\s*h\b", t, re.I)
    if not am:
        am = re.search(r"\b(\d{2,3})\s*Ah\b", t, re.I)
    if am:
        out["capacity_ah"] = int(am.group(1))
    # CCA / štartovací prúd: 540A, 610 A (nie Ah)
    cca = re.search(
        r"(?:CCA|EN|štartovac[ií]\s*prúd|startovaci\s*proud|starting\s*current)"
        r"[^\d]{0,12}(\d{3,4})\s*A\b",
        t,
        re.I,
    )
    if not cca:
        # „12V 60AH 540A“ — A po Ah
        cca = re.search(r"\bAh\b[^\d]{0,8}(\d{3,4})\s*A\b", t, re.I)
    if not cca:
        cca = re.search(r"\b(\d{3,4})\s*A\b(?!\s*h)", t, re.I)
    if cca:
        val = int(cca.group(1))
        # Filter nonsense (not 12A from 12V misread)
        if val >= 200:
            out["cca_a"] = val
    tech = ""
    if re.search(r"\bAGM\b", t, re.I):
        tech = "AGM"
    elif re.search(r"\bEFB\b", t, re.I):
        tech = "EFB"
    elif re.search(r"start[\s\-]?stop", t, re.I):
        tech = "Start-Stop"
    if tech:
        out["tech"] = tech
    return out


def extract_alternator_specs(text: str) -> Dict[str, Any]:
    """Prúd alternátora (A) + napätie."""
    t = str(text or "")
    out: Dict[str, Any] = {}
    vm = re.search(r"\b(12|24)\s*V\b", t, re.I)
    if vm:
        out["voltage_v"] = int(vm.group(1))
    am = re.search(
        r"(?:prúd|proud|amps?|output|výkon)[^\d]{0,12}(\d{2,3})\s*A\b",
        t,
        re.I,
    )
    if not am:
        am = re.search(r"\b(\d{2,3})\s*A\b(?!\s*h)", t, re.I)
    if am:
        val = int(am.group(1))
        if 30 <= val <= 300:
            out["amps"] = val
    return out


def looks_like_battery(raw: Dict[str, Any]) -> bool:
    blob = _blob_from_raw(raw).lower()
    return bool(
        re.search(
            r"batér|bater|akumul|battery|agm|efb|start[\s\-]?stop",
            blob,
            re.I,
        )
    ) and not re.search(r"alternátor|alternator|štartér(?!\s*bat)|starter\s*motor", blob, re.I)


def looks_like_alternator(raw: Dict[str, Any]) -> bool:
    blob = _blob_from_raw(raw).lower()
    return bool(re.search(r"alternátor|alternator|dynamo", blob, re.I))


def electrical_specs_meta(raw: Dict[str, Any]) -> List[Dict[str, str]]:
    """WC meta_data rows — bez TecDoc ID."""
    blob = _blob_from_raw(raw)
    rows: List[Dict[str, str]] = []
    if looks_like_battery(raw):
        spec = extract_battery_specs(blob)
        if spec.get("capacity_ah") is not None:
            rows.append({"key": "_sg_batt_ah", "value": str(spec["capacity_ah"])})
        if spec.get("cca_a") is not None:
            rows.append({"key": "_sg_batt_cca_a", "value": str(spec["cca_a"])})
        if spec.get("voltage_v") is not None:
            rows.append({"key": "_sg_batt_voltage_v", "value": str(spec["voltage_v"])})
        if spec.get("tech"):
            rows.append({"key": "_sg_batt_tech", "value": str(spec["tech"])})
    elif looks_like_alternator(raw):
        spec = extract_alternator_specs(blob)
        if spec.get("amps") is not None:
            rows.append({"key": "_sg_alt_amps", "value": str(spec["amps"])})
        if spec.get("voltage_v") is not None:
            rows.append({"key": "_sg_alt_voltage_v", "value": str(spec["voltage_v"])})
    return rows
