#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Publikuj draft produkty, ktoré prejdú tvrdou QA bránou (cena, foto, SKU, bez sudov).

  python bot/publish_ready_drafts.py --dry-run
  python bot/publish_ready_drafts.py --limit 40
  python bot/publish_ready_drafts.py --limit 0   # všetky ready
  python bot/publish_ready_drafts.py --fitment-only --limit 20
  python bot/publish_ready_drafts.py --min-id 9500 --limit 20
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
LOG = logging.getLogger("publish_ready")

BULK_RE = re.compile(r"\b(20|25|55|58|60|200|205|208)\s*l\b", re.I)
BAD_NAME_RE = re.compile(r"placeholder|lorem|test product|untitled", re.I)


def wc():
    url = (os.environ.get("WC_URL") or "").rstrip("/")
    ck = os.environ.get("WC_CONSUMER_KEY") or os.environ.get("WC_KEY")
    cs = os.environ.get("WC_CONSUMER_SECRET") or os.environ.get("WC_SECRET")
    if not all([url, ck, cs]):
        raise SystemExit("Chýba WC_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET")
    return url, HTTPBasicAuth(ck, cs)


def fetch_drafts(url, auth, session: requests.Session | None = None, stop_after_ready: int = 0):
    """Najnovšie drafty prvé (id desc) — kaizen dávky majú vysoké WC id.

    Staré fronty (no_cost / bulk) by inak vytláčali nové fitment SKU
    mimo window 12×50, keď draftov pribudne nad ~600.

    stop_after_ready>0: predčasný koniec listovania po N draftoch (stačí na --limit).
    """
    sess = session or requests.Session()
    out = []
    page = 1
    while page <= 12:
        r = sess.get(
            f"{url}/wp-json/wc/v3/products",
            auth=auth,
            params={
                "status": "draft",
                "per_page": 100,
                "page": page,
                "orderby": "id",
                "order": "desc",
            },
            timeout=60,
        )
        batch = r.json() if r.ok else []
        if not batch:
            break
        out.extend(batch)
        if stop_after_ready and len(out) >= max(stop_after_ready * 3, stop_after_ready + 50):
            # 3× limit kandidátov stačí na QA rejecty; neťahaj celých 600.
            break
        if len(batch) < 100:
            break
        page += 1
    return out


def qa_reasons(p: dict, max_price: float, require_in_stock: bool = True) -> list[str]:
    name = p.get("name") or ""
    price = 0.0
    try:
        price = float(p.get("regular_price") or p.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    imgs = p.get("images") or []
    cats = p.get("categories") or []
    desc = (p.get("description") or "") + (p.get("short_description") or "")
    sku = (p.get("sku") or "").strip()
    reasons = []
    meta = {m.get("key"): m.get("value") for m in (p.get("meta_data") or []) if isinstance(m, dict)}
    cost_raw = meta.get("_cost_price")
    try:
        cost_f = float(str(cost_raw).replace(",", ".")) if cost_raw not in (None, "") else None
    except (TypeError, ValueError):
        cost_f = None

    if price <= 0:
        reasons.append("no_price")
    # Würth: cena je za balenie — bez overeného balenia/pôvodu (SK/DE) nepredávať.
    if str(meta.get("_supplier_source") or "").lower() == "wurth" and not (
        str(meta.get("_sg_pack_verified") or "") == "1" and meta.get("_sg_origin")
    ):
        reasons.append("wurth_pack_origin_unverified")
    # Jidoka: falošné 1.90 € / podnákladové ceny z chybného prepisu.
    # Drobný retail (žiarovky, poistky, sviečky, malé DOT) bežne 0,5–3 € — OK.
    cat_blob_early = " ".join((c.get("name") or "") for c in cats).lower()
    small_retail = bool(
        re.search(
            r"žiarov|ziarov|\bbulb\b|\bw5w\b|\bp21|\bh\s*[1347]\b|\bh11\b|\bhb\s*[34]\b|"
            r"výbojk|vybojk|halogén|halogen|ledriving|"
            r"poistk|fuse\b|rel[eé]|relay|"
            r"svieč|sviec|spark\s*plug|glow\s*plug|žeravi|zeravi|"
            r"\bdot\s*[45]|brzdov\w*\s+kvapalin",
            name,
            re.I,
        )
        or any(
            x in cat_blob_early
            for x in (
                "žiarovky",
                "ziarovky",
                "poistky",
                "relé",
                "rele",
                "sviečky",
                "sviecky",
                "brzdové kvapaliny",
                "brzdove kvapaliny",
            )
        )
    )
    if 0 < price < 4.0 and not small_retail:
        reasons.append("price_too_low")
    elif small_retail and 0 < price < 0.35:
        reasons.append("price_too_low")
    if cost_f is None or cost_f <= 0:
        reasons.append("no_cost")
    elif price + 0.009 < cost_f:
        reasons.append("below_cost")
    else:
        from pricing import bad_cost_reason
        if bad_cost_reason(
            cost_f, price,
            supplier=str(meta.get("_supplier_source") or ""),
            name=name, sku=sku,
        ) == "cost_outlier":
            reasons.append("cost_outlier")
    # Oleje 4–5 L bežne 80–150 €. Autodiely (kotúče, strmene, filtre…) môžu byť
    # stovky € — default strop je len proti absurdným outlierom, nie proti dielom.
    oilish = bool(re.search(r"\b\d+w[-\s]?\d+\b|motorov[ýy]\s+olej", name, re.I))
    cat_blob = " ".join((c.get("name") or "") for c in cats).lower()
    partish = bool(
        re.search(
            r"brzd|kot[uú][cč]|strme[nň]|do[sš]ti[cč]|filter|filtr|tlmi[cč]|"
            r"spojk|lo[zž]isk|rameno|čap|cap\b|silentblok|rozvod|lambda|"
            r"žiarov|ziarov|stiera[cč]|tesnen|hadic|hadíc|senzor|sn[ií]ma[cč]|"
            r"altern[aá]tor|štart[eé]r|start[eé]r|bat[eé]ri|akumul[aá]tor|"
            r"vstrekov|turbo|egr|dpf|lambda|lambda\s*sonda",
            name,
            re.I,
        )
        or "náhradné diely" in cat_blob
        or "nahradne diely" in cat_blob
    )
    if oilish:
        effective_max = max(200.0, max_price)
    elif partish:
        # Kotúče / strmene / sady / Brembo — volume katalog, strop len absurdy.
        effective_max = max(1500.0, max_price)
    else:
        effective_max = max_price
    if price > effective_max:
        reasons.append("price_high")
    if BULK_RE.search(name):
        reasons.append("bulk_pack")
    if not imgs:
        reasons.append("no_image")
    if not cats:
        reasons.append("no_cat")
    if len(name.strip()) < 8:
        reasons.append("short_name")
    if not sku:
        reasons.append("no_sku")
    if BAD_NAME_RE.search(name):
        reasons.append("placeholder")
    # Navigačné / nebezpečné / non-SKU šum z TecDoc crawl
    junk = re.compile(
        r"^(szekop\s+)?(filtre|1\.9\s*tdi|octavia\s+ii)\b|airbag|káva|kava\b|"
        r"\b(1000|208|205|200|60|58|55)\s*l\b|"
        r"originální\s+díl\s+\w+\s+náhradný\s+diel|"
        r"originálny\s+náhradný\s+diel",
        re.I,
    )
    if junk.search(name.strip()):
        reasons.append("junk_name")
    if len(desc.strip()) < 40:
        reasons.append("thin_desc")
    # Keyword kategória musí sedieť (ak ju vieme určiť)
    try:
        from taxonomy import match_keyword_category

        expected = match_keyword_category(
            {"name": name, "sku": sku, "brand": "", "description": ""}
        )
        cat_names = {(c.get("name") or "").replace("&amp;", "&") for c in cats}
        # Stačí zhoda ľubovoľného uzla v ceste (leaf 5W-30 vs WC „Motorové oleje“)
        # WC default kategória (Auto chémia) bez keyword zhody = vlastne bez kategórie
        if not expected and cat_names <= {"Auto chémia"}:
            reasons.append("no_cat")
        if expected and not any(
            (node in cat_names) or any(node in c or c in node for c in cat_names)
            for node in expected
        ):
            reasons.append("wrong_category")
    except Exception:
        pass
    # dvojité balenie v názve = rizikové
    if re.search(r"\b\d+\s*l\s*/\s*\d+\s*l\b", name, re.I):
        reasons.append("dual_pack_name")
    # Príliš generický názov bez viskozity/rady (jidoka — nepoušťaj von)
    low = name.strip().lower()
    if low in {"motorový olej mpm", "motorovy olej mpm"}:
        reasons.append("generic_name")
    elif re.fullmatch(
        r"motorov[ýy]\s+olej\s+[a-z0-9\-]+",
        low,
    ) and not re.search(r"\b\d+w[-\s]?\d+\b|\b\d+\s*l\b", low):
        reasons.append("generic_name")
    # Samuel: každý hlavný diel musí mať OE číslo. Výnimka: drobné komponenty
    # (sady na opravu, piesty, puzdrá, spony, tesnenia…), chémia, náradie.
    try:
        from oe_publish_rule import needs_oe, has_oe_numbers

        if needs_oe(name, meta) and not has_oe_numbers(meta, sku):
            reasons.append("no_oe")
    except Exception:
        pass
    if require_in_stock:
        st = str(p.get("stock_status") or "").lower()
        qty = p.get("stock_quantity")
        try:
            qty_f = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty_f = None
        try:
            supplier_qty = float(meta.get("_supplier_stock")) if meta.get("_supplier_stock") not in (None, "") else None
        except (TypeError, ValueError):
            supplier_qty = None
        if qty_f is not None and qty_f <= 0:
            reasons.append("out_of_stock")
        elif supplier_qty is not None and supplier_qty <= 0:
            reasons.append("out_of_stock")
        elif st == "outofstock":
            reasons.append("out_of_stock")
    return reasons


def _is_duplicate(p: dict, meta: dict, url: str, auth, session) -> bool:
    """Duplicita: iný (nie tento) produkt s rovnakým SKU alebo EAN už existuje."""
    pid = int(p.get("id") or 0)
    sku = (p.get("sku") or "").strip()
    ean = str(meta.get("_sg_ean") or meta.get("_ean") or meta.get("ean") or p.get("global_unique_id") or "").strip()
    checks = []
    if sku:
        checks.append({"sku": sku})
    if ean and len(ean) >= 8:
        checks.append({"global_unique_id": ean})
    for params in checks:
        try:
            r = session.get(f"{url}/wp-json/wc/v3/products", auth=auth, timeout=40,
                            params={"per_page": 5, "status": "any", **params})
            if r.ok and any(int(x.get("id") or 0) != pid and x.get("status") != "trash" for x in r.json()):
                return True
        except Exception:
            pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=40, help="0 = všetky ready")
    ap.add_argument(
        "--max-price",
        type=float,
        default=500.0,
        help="QA strop pre ne-diely (default 500€); autodiely floor 800€, oleje 150€",
    )
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument(
        "--allow-oos",
        action="store_true",
        help="Povoliť publish aj mimo skladu (default: len na sklade)",
    )
    ap.add_argument(
        "--fitment-only",
        action="store_true",
        help="Len drafty s _sg_fit_make (pilot diely pred starým frontom)",
    )
    ap.add_argument(
        "--min-id",
        type=int,
        default=0,
        help="Len produkty s WC id >= N (nové dávky)",
    )
    ap.add_argument(
        "--name-contains",
        default="",
        help="Len drafty s týmto textom v názve (napr. Würth)",
    )
    ap.add_argument(
        "--supplier",
        default="",
        help="Filter _supplier_source / _sg_supplier (napr. lkq,szekop)",
    )
    args = ap.parse_args()

    url, auth = wc()
    session = requests.Session()
    # Pri name/supplier filtri treba prejsť viac strán — early-stop by inak
    # vynechal staršie ready (Würth mid-ID mimo najnovších 60–100 draftov).
    name_filter = (args.name_contains or "").strip().lower()
    supplier_filter = (args.supplier or "").strip()
    stop_after = 0 if (name_filter or supplier_filter or args.limit == 0) else (
        args.limit if args.limit > 0 else 0
    )
    drafts = fetch_drafts(url, auth, session=session, stop_after_ready=stop_after)
    ready = []
    reject = {}
    suppliers = {
        s.strip().lower()
        for s in (args.supplier or "").split(",")
        if s.strip()
    }
    from account_split import filter_products as _acct_filter
    drafts = _acct_filter(drafts)
    for p in drafts:
        if args.min_id and int(p.get("id") or 0) < args.min_id:
            continue
        if name_filter and name_filter not in (p.get("name") or "").lower():
            continue
        meta = {
            m.get("key"): m.get("value")
            for m in (p.get("meta_data") or [])
            if isinstance(m, dict)
        }
        if suppliers:
            src = str(
                meta.get("_supplier_source") or meta.get("_sg_supplier") or ""
            ).strip().lower()
            if not any(tok in src for tok in suppliers):
                continue
        if args.fitment_only and not (meta.get("_sg_fit_make") or "").strip():
            continue
        # Ručné zadržanie (napr. neoverená generácia auta) — bot nikdy nepublikuje.
        if str(meta.get("_sg_manual_hold") or "").strip() not in ("", "0"):
            reject["manual_hold"] = reject.get("manual_hold", 0) + 1
            continue
        rs = qa_reasons(p, args.max_price, require_in_stock=not args.allow_oos)
        if not rs and _is_duplicate(p, meta, url, auth, session):
            rs = ["duplicate"]
        if rs:
            for r in rs:
                reject[r] = reject.get(r, 0) + 1
            if ("no_cat" in rs or "wrong_category" in rs) and not args.dry_run and meta.get("_sg_hold_reason") != "nejasná kategória":
                try:
                    session.put(
                        f"{url}/wp-json/wc/v3/products/{int(p['id'])}",
                        auth=auth,
                        json={"meta_data": [{"key": "_sg_hold_reason", "value": "nejasná kategória"}]},
                        timeout=40,
                    )
                except Exception:
                    pass
            if "no_oe" in rs and not args.dry_run and meta.get("_sg_hold_reason") != "chýba OE číslo":
                try:
                    session.put(
                        f"{url}/wp-json/wc/v3/products/{int(p['id'])}",
                        auth=auth,
                        json={"meta_data": [{"key": "_sg_hold_reason", "value": "chýba OE číslo"}]},
                        timeout=40,
                    )
                except Exception:
                    pass
            continue
        ready.append(p)
        if args.limit > 0 and len(ready) >= args.limit:
            break

    LOG.info("drafts=%d ready=%d reject=%s", len(drafts), len(ready), reject)
    if args.limit > 0:
        ready = ready[: args.limit]

    ok = fail = 0
    # WC batch update po ≤25 — menej round-tripov než PUT/ks
    if not args.dry_run and ready:
        for i in range(0, len(ready), 25):
            chunk = ready[i : i + 25]
            payload = {"update": [{"id": int(p["id"]), "status": "publish"} for p in chunk]}
            for p in chunk:
                LOG.info(
                    "PUB #%s %s | %s€",
                    p.get("id"),
                    (p.get("name") or "")[:55],
                    p.get("regular_price") or p.get("price"),
                )
            r = session.post(
                f"{url}/wp-json/wc/v3/products/batch",
                auth=auth,
                json=payload,
                timeout=120,
            )
            if r.ok:
                updated = (r.json() or {}).get("update") or []
                ok += sum(1 for u in updated if (u or {}).get("status") == "publish")
                fail += max(0, len(chunk) - sum(1 for u in updated if (u or {}).get("status") == "publish"))
            else:
                # fallback: serial PUT
                LOG.warning("batch FAIL %s — fallback serial", r.status_code)
                for p in chunk:
                    pid = int(p["id"])
                    rr = session.put(
                        f"{url}/wp-json/wc/v3/products/{pid}",
                        auth=auth,
                        json={"status": "publish"},
                        timeout=60,
                    )
                    if rr.ok and (rr.json() or {}).get("status") == "publish":
                        ok += 1
                    else:
                        fail += 1
                        LOG.warning("FAIL #%s %s %s", pid, rr.status_code, (rr.text or "")[:160])
                    time.sleep(args.sleep)
            time.sleep(args.sleep)
    else:
        for p in ready:
            pid = int(p["id"])
            name = p.get("name") or ""
            price = p.get("regular_price") or p.get("price")
            LOG.info("%s #%s %s | %s€", "DRY" if args.dry_run else "PUB", pid, name[:55], price)
            if args.dry_run:
                ok += 1

    LOG.info("Hotovo: published=%d failed=%d dry=%s", ok, fail, args.dry_run)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
