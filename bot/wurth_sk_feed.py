#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Würth SK cenník (XLSX / CSV / BMEcat) → cena a sklad. Obsah môže byť v súbore.

Majiteľ žiada u obchodného zástupcu Würth týždenný cenník (XLSX alebo BMEcat).
Príde ako príloha e-mailu na Gmail. Bot schránku zatiaľ nečíta — súbor sa sem
dostane ručne: nahratie v HQ, alebo súbor v bot/data/wurth_sk_inbox/.
Automatické stiahnutie z Gmailu je až ďalší krok.

Würth SK: všetky ceny v súbore sú bez DPH. Objednávka do 99,99 € netto má
manipulačný poplatok 6,80 €, od 100 € je bez poplatku (viď calculate_retail /
wurth_price_floor). Predaj = netto × 1,23, potom marža, fee floor a min. 20 %.
Riadok bez ceny nenechá starú cenu. Riadok bez skladu nemení sklad.
Nové SKU vzniknú ako koncept.

Súbor: --file, inak bot/data/wurth_sk_inbox/, inak HQ GET /api/sync/wurth-pricelist.

  python bot/wurth_sk_feed.py --file cennik.xlsx          # náhľad
  python bot/wurth_sk_feed.py --file cennik.xlsx --apply
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pricing import calculate_retail  # noqa: E402

KEEP_PRICE_SKUS = {"0500300002"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

SKU_HEADERS = (
    "artikel", "artikelnr", "artikel nr", "article", "article number", "sku",
    "cislo artiklu", "cislo artikla", "supplier pid", "supplier aid", "art nr",
    "art. nr", "material", "matnr", "objednavacie cislo", "produktove cislo",
    "item number", "order code", "artikelnummer",
)
PRICE_HEADERS = (
    "cena bez dph", "netto", "net price", "nettocena", "nakupna cena", "preis",
    "price amount", "jednotkova cena", "b2b", "cena netto", "price", "cena",
)
PRICE_SKIP = ("s dph", "brutto", "retail", "moc", "predaj", "gross")
EAN_HEADERS = ("ean", "gtin", "barcode", "ciarovy kod", "ean kod")
PACK_HEADERS = (
    "mj balenie", "mnozstvo v baleni", "balenie", "pack", "vpe", "pocet",
    "content unit", "order unit", "verpackung", "verpackungseinheit", "mj",
)
STOCK_HEADERS = (
    "stav skladu", "dostupnost", "availability", "lager", "sklad", "stock", "qty",
)
ORIGIN_HEADERS = ("povod", "dodavane z", "dispatch", "krajina skladu", "origin", "land")
NAME_HEADERS = ("nazov", "name", "description short", "popis", "bezeichnung", "artikelbezeichnung")
IMAGE_HEADERS = ("obrazok", "image", "mime", "foto", "url obrazku", "mime source")
DESC_HEADERS = ("description long", "dlhy popis", "popis dlhy", "description")


@dataclass
class FeedRow:
    sku: str
    net_price: Optional[float] = None
    ean: str = ""
    pack_qty: Optional[int] = None
    stock_text: str = ""
    stock_qty: Optional[float] = None
    origin_text: str = ""
    name: str = ""
    description: str = ""
    image_url: str = ""


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def parse_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace("\xa0", " ").replace("€", "").replace("EUR", "").strip()
    if not text or text.lower() in ("none", "nan"):
        return None
    text = text.replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse_pack(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)", text.replace(" ", ""))
    if not m:
        return None
    n = int(m.group(1))
    return n if n > 0 else None


def _header_index(headers: Sequence[str]) -> Dict[str, int]:
    folded = [_fold(h) for h in headers]

    def find(cands: Sequence[str], skip: Sequence[str] = ()) -> Optional[int]:
        best = None
        best_len = -1
        for i, h in enumerate(folded):
            if not h or any(s in h for s in skip):
                continue
            for cand in cands:
                if h == cand or cand in h:
                    if len(cand) > best_len:
                        best, best_len = i, len(cand)
        return best

    out = {}
    for key, cands, skip in (
        ("sku", SKU_HEADERS, ()),
        ("price", PRICE_HEADERS, PRICE_SKIP),
        ("ean", EAN_HEADERS, ()),
        ("pack", PACK_HEADERS, ("cena",)),
        ("stock", STOCK_HEADERS, ("balen",)),
        ("origin", ORIGIN_HEADERS, ()),
        ("name", NAME_HEADERS, ("long", "dlhy")),
        ("image", IMAGE_HEADERS, ()),
        ("description", DESC_HEADERS, ("short",)),
    ):
        idx = find(cands, skip)
        if idx is not None:
            out[key] = idx
    return out


def rows_from_table(headers: Sequence[str], data: Iterable[Sequence[Any]]) -> List[FeedRow]:
    idx = _header_index(headers)
    if "sku" not in idx:
        raise ValueError("V súbore nevidím stĺpec s číslom artiklu (Artikel / Číslo artiklu / SKU).")
    rows: List[FeedRow] = []
    for raw in data:
        cells = list(raw)
        def cell(key: str) -> str:
            i = idx.get(key)
            if i is None or i >= len(cells):
                return ""
            return "" if cells[i] is None else str(cells[i]).strip()

        sku = re.sub(r"\s+", "", cell("sku"))
        if not sku or _fold(sku) in ("artikel", "sku", "cislo artiklu"):
            continue
        stock_raw = cell("stock")
        qty = parse_money(stock_raw) if re.fullmatch(r"\d+([.,]\d+)?", stock_raw.replace(" ", "")) else None
        rows.append(FeedRow(
            sku=sku,
            net_price=parse_money(cell("price")) if "price" in idx else None,
            ean=re.sub(r"\s+", "", cell("ean")),
            pack_qty=parse_pack(cell("pack")) if cell("pack") else None,
            stock_text=stock_raw,
            stock_qty=qty,
            origin_text=cell("origin"),
            name=cell("name"),
            description=cell("description"),
            image_url=cell("image"),
        ))
    return rows


def parse_csv(data: bytes) -> List[FeedRow]:
    text = None
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV sa nedá prečítať.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), dialect)
    table = [row for row in reader if any(str(c).strip() for c in row)]
    if not table:
        return []
    return rows_from_table(table[0], table[1:])


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(0, n - 1)


def parse_xlsx(data: bytes) -> List[FeedRow]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(tag + "t")))
        sheet_name = "xl/worksheets/sheet1.xml"
        root = ET.fromstring(zf.read(sheet_name))
        table: List[List[str]] = []
        for row in root.findall("m:sheetData/m:row", NS):
            vals: List[str] = []
            for cell in row.findall("m:c", NS):
                idx = _col_index(cell.get("r") or "")
                kind = cell.get("t")
                v = cell.find("m:v", NS)
                inline = cell.find("m:is", NS)
                if kind == "s" and v is not None and v.text:
                    val = shared[int(v.text)]
                elif kind == "inlineStr" and inline is not None:
                    tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                    val = "".join(t.text or "" for t in inline.iter(tag))
                elif v is not None and v.text:
                    val = v.text
                else:
                    val = ""
                while len(vals) <= idx:
                    vals.append("")
                vals[idx] = val
            if any(vals):
                table.append(vals)
    if not table:
        return []
    return rows_from_table(table[0], table[1:])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _child_text(el: ET.Element, name: str) -> str:
    name = name.upper()
    for node in el.iter():
        if _local(node.tag) == name and node.text and node.text.strip():
            return node.text.strip()
    return ""


def parse_bmecat(data: bytes) -> List[FeedRow]:
    root = ET.fromstring(data)
    rows: List[FeedRow] = []
    for art in root.iter():
        if _local(art.tag) not in ("ARTICLE", "PRODUCT"):
            continue
        sku = ""
        for tag in ("SUPPLIER_AID", "SUPPLIER_PID", "BUYER_AID", "MANUFACTURER_PID"):
            sku = _child_text(art, tag)
            if sku:
                break
        if not sku:
            continue
        price = None
        for node in art.iter():
            if _local(node.tag) != "ARTICLE_PRICE":
                continue
            ptype = _fold(node.get("price_type") or node.get("type") or "")
            amount = _child_text(node, "PRICE_AMOUNT")
            val = parse_money(amount)
            if val is None:
                continue
            if "net" in ptype or not ptype or price is None:
                price = val
                if "net" in ptype:
                    break
        image = ""
        for node in art.iter():
            if _local(node.tag) == "MIME_SOURCE" and node.text:
                image = node.text.strip()
                break
        pack = parse_pack(_child_text(art, "QUANTITY_MIN") or _child_text(art, "NO_CU_PER_OU"))
        stock = _child_text(art, "STOCK") or _child_text(art, "AVAILABILITY")
        rows.append(FeedRow(
            sku=re.sub(r"\s+", "", sku),
            net_price=price,
            ean=re.sub(r"\s+", "", _child_text(art, "EAN") or _child_text(art, "INTERNATIONAL_PID")),
            pack_qty=pack,
            stock_text=stock,
            stock_qty=parse_money(stock) if re.fullmatch(r"\d+([.,]\d+)?", stock.replace(" ", "")) else None,
            name=_child_text(art, "DESCRIPTION_SHORT"),
            description=_child_text(art, "DESCRIPTION_LONG"),
            image_url=image,
        ))
    return rows


def parse_feed(data: bytes, filename: str = "") -> List[FeedRow]:
    name = filename.lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        return parse_xlsx(data)
    stripped = data.lstrip()
    if name.endswith(".xml") or stripped.startswith(b"<") or stripped.startswith(b"\xef\xbb\xbf<"):
        return parse_bmecat(data)
    return parse_csv(data)


def interpret_stock(row: FeedRow) -> Optional[Dict[str, Any]]:
    """None = v súbore nie je sklad, nič nemeníme. Neznámy text sa nehádá."""
    text = _fold(f"{row.origin_text} {row.stock_text}")
    tokens = set(text.split())
    qty = row.stock_qty
    if not text and qty is None:
        return None
    if any(k in text for k in ("nedostup", "vypred", "out of stock", "nie je skladom")):
        return {"avail": "na", "origin": "", "qty": 0.0}
    if text in ("0", "0 0") or qty == 0:
        return {"avail": "na", "origin": "", "qty": 0.0}
    origin = ""
    avail = ""
    if "skladom v sr" in text or "slovensko" in text or "slovakia" in text or tokens & {"sk", "sr"}:
        origin, avail = "SK", "sr"
    elif "sklad v eu" in text or "nemecko" in text or "deutschland" in text or tokens & {"de", "eu"}:
        origin, avail = "DE", "eu"
    if avail:
        return {"avail": avail, "origin": origin, "qty": qty}
    if qty is not None and qty > 0 and (not text or re.fullmatch(r"\d+(?: \d+)?", text)):
        return {"avail": "", "origin": "", "qty": qty, "qty_only": True}
    if text and not avail:
        return {"avail": "", "origin": "", "unparsed": row.stock_text or row.origin_text}
    return None


def plan_update(row: FeedRow, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Čo zapísať. Existujúci produkt: bez status. Nový: status draft."""
    action = "create" if not existing else "update"
    out: Dict[str, Any] = {"action": action, "sku": row.sku, "notes": []}
    payload: Dict[str, Any] = {}
    meta: List[Dict[str, str]] = [
        {"key": "_supplier_source", "value": "wurth"},
        {"key": "_sg_supplier", "value": "wurth_sk"},
        {"key": "_sg_price_source", "value": "wurth_sk_pricelist"},
    ]
    if existing and existing.get("id"):
        payload["id"] = existing["id"]
    if row.sku in KEEP_PRICE_SKUS:
        out["notes"].append("keep_price")
    elif row.net_price is not None and row.net_price > 0:
        priced = calculate_retail(
            row.net_price,
            supplier="wurth",
            name=row.name or (existing or {}).get("name") or "",
            sku=row.sku,
            cost_has_vat=True,
            vat_rate=0.0,
        )
        payload["regular_price"] = f"{priced.retail_price:.2f}"
        payload["sale_price"] = ""
        meta.append({"key": "_cost_price", "value": f"{row.net_price:.2f}"})
        out["retail"] = priced.retail_price
        out["net"] = row.net_price
    else:
        out["notes"].append("no_fresh_price")

    stock = interpret_stock(row)
    if stock is None:
        out["notes"].append("no_fresh_stock")
    elif stock.get("unparsed"):
        out["notes"].append("stock_unparsed")
    elif stock.get("qty_only"):
        meta.append({"key": "_supplier_stock", "value": f"{stock['qty']:g}"})
    else:
        from availability import classify_wurth, meta_rows, stock_payload
        status = "outofstock" if stock["avail"] == "na" else "instock"
        av = classify_wurth(stock.get("origin") or "", status)
        tmp: Dict[str, Any] = {"meta_data": []}
        stock_payload(tmp, av["avail"], current=existing)
        for key in ("stock_status", "manage_stock", "stock_quantity"):
            if key in tmp:
                payload[key] = tmp[key]
        meta.extend(meta_rows(av))
        if stock.get("origin"):
            meta.append({"key": "_sg_origin", "value": stock["origin"]})

    if row.ean:
        meta.append({"key": "_ean", "value": row.ean})
    if row.pack_qty:
        meta.append({"key": "_sg_pack_qty", "value": str(row.pack_qty)})
        meta.append({"key": "_sg_pack_source", "value": "wurth_sk_pricelist"})
    if row.name and (action == "create" or not (existing or {}).get("name")):
        payload["name"] = row.name
    elif action == "create":
        payload["name"] = f"Würth {row.sku}"
    if row.description:
        payload["description"] = row.description
    if row.image_url:
        payload["images"] = [{"src": row.image_url}]
        meta.append({"key": "_sg_image_source", "value": "wurth_sk_pricelist"})
    if action == "create":
        payload["status"] = "draft"
        payload["type"] = "simple"
        payload["sku"] = row.sku
    payload["meta_data"] = meta
    if "status" in payload and action == "update":
        del payload["status"]
    out["payload"] = payload
    return out


def plan_content_update(product_id: int, *, description: str = "", image_url: str = "", pack_qty: Optional[int] = None) -> Dict[str, Any]:
    """Len fotka / popis / balenie. Cena a sklad sa do payloadu nedostanú."""
    payload: Dict[str, Any] = {"id": product_id, "meta_data": []}
    if description:
        payload["description"] = description
    if image_url:
        payload["images"] = [{"src": image_url}]
        payload["meta_data"].append({"key": "_sg_image_source", "value": "wurth_public_content"})
    if pack_qty:
        payload["meta_data"].append({"key": "_sg_pack_qty", "value": str(pack_qty)})
        payload["meta_data"].append({"key": "_sg_pack_source", "value": "wurth_public_content"})
    return payload


def load_feed_bytes(path: str = "") -> tuple[Optional[bytes], str]:
    if path:
        p = Path(path)
        return p.read_bytes(), p.name
    inbox = ROOT / "data" / "wurth_sk_inbox"
    files = sorted(
        p for p in inbox.glob("*")
        if p.is_file() and p.suffix.lower() in (".xlsx", ".csv", ".xml", ".txt") and not p.name.startswith(".")
    )
    if files:
        return files[-1].read_bytes(), files[-1].name
    token = os.environ.get("HQ_SYNC_TOKEN", "").strip()
    if not token:
        return None, ""
    import requests
    hq = os.environ.get("HQ_URL", "https://hq.smartgaraz.sk").rstrip("/")
    resp = requests.get(
        f"{hq}/api/sync/wurth-pricelist",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "smartgaraz-bot"},
        timeout=120,
    )
    if resp.status_code == 404:
        return None, ""
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    filename = "wurth-pricelist"
    if "json" in ctype:
        body = resp.json()
        filename = str(body.get("filename") or filename)
        if body.get("content_base64"):
            return base64.b64decode(body["content_base64"]), filename
        return None, ""
    return resp.content, filename


def main() -> int:
    ap = argparse.ArgumentParser(description="Würth SK cenník → cena a sklad")
    ap.add_argument("--file", default="", help="XLSX, CSV alebo BMEcat XML")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    args = ap.parse_args()
    data, name = load_feed_bytes(args.file)
    if not data:
        print("SG_RESULT " + json.dumps({
            "ok": True,
            "msg": "Žiadny čerstvý Würth SK cenník (inbox ani HQ). Ceny a sklad ostávajú, nič sa nehádalo.",
            "updated": 0, "created": 0,
        }, ensure_ascii=False))
        return 0
    rows = parse_feed(data, name)
    print(f"[wurth-sk] {name}: {len(rows)} riadkov", flush=True)

    creds_url = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY") or ""
    secret = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not args.apply or not (creds_url and key and secret):
        preview = [plan_update(r) for r in rows[:20]]
        print(json.dumps({
            "file": name, "rows": len(rows), "apply": False,
            "with_price": sum(1 for r in rows if r.net_price and r.net_price > 0),
            "with_stock": sum(1 for r in rows if interpret_stock(r) and not interpret_stock(r).get("unparsed")),
            "preview": [{k: v for k, v in p.items() if k != "payload"} | {"price": (p.get("payload") or {}).get("regular_price"), "status": (p.get("payload") or {}).get("status")} for p in preview],
        }, ensure_ascii=False, indent=2))
        if args.apply:
            print("SG_RESULT " + json.dumps({"ok": False, "msg": "Chýbajú WC kľúče — nič sa nezapísalo."}, ensure_ascii=False))
            return 2
        return 0

    import requests
    from account_split import mine
    from shop_guard import wait_if_slow

    auth = (key, secret)
    session = requests.Session()
    by_sku: Dict[str, Dict[str, Any]] = {}
    page = 1
    while True:
        wait_if_slow()
        resp = session.get(
            f"{creds_url}/wp-json/wc/v3/products", auth=auth,
            params={"status": "any", "per_page": 100, "page": page, "_fields": "id,sku,name,status,regular_price,sale_price,meta_data"},
            timeout=120,
        )
        if not resp.ok:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            sku = str(p.get("sku") or "")
            if sku:
                by_sku[sku] = p
        if len(batch) < 100:
            break
        page += 1

    creates, updates = [], []
    for row in rows:
        if not mine(row.sku):
            continue
        plan = plan_update(row, by_sku.get(row.sku))
        payload = plan["payload"]
        if plan["action"] == "create":
            creates.append(payload)
        else:
            updates.append(payload)

    def flush(kind: str, chunk: List[dict]) -> int:
        if not chunk:
            return 0
        wait_if_slow()
        resp = session.post(
            f"{creds_url}/wp-json/wc/v3/products/batch", auth=auth,
            json={kind: chunk}, timeout=180,
        )
        if not resp.ok:
            print(f"[wurth-sk] batch {kind} HTTP {resp.status_code}", flush=True)
            return 0
        return len((resp.json() or {}).get(kind) or [])

    written_u = written_c = 0
    step = max(1, args.batch)
    for i in range(0, len(updates), step):
        written_u += flush("update", updates[i:i + step])
    for i in range(0, len(creates), step):
        written_c += flush("create", creates[i:i + step])
    print("SG_RESULT " + json.dumps({
        "ok": True,
        "msg": f"Würth SK cenník {name}: upravených {written_u}, nových konceptov {written_c}.",
        "updated": written_u, "created": written_c, "rows": len(rows),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
