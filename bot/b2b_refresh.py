#!/usr/bin/env python3
"""Update price, stock, and availability for products already stored.

  python bot/b2b_refresh.py --supplier szekop
  python bot/b2b_refresh.py --supplier lkq --limit 12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from supplier_pace import (  # noqa: E402
    DailyCapReached,
    LoginFailed,
    RunCapReached,
    SupplierBlocked,
    SupplierStop,
    decide_outcome,
    guard,
    install_from_env,
    job_summary,
    primary_account,
    restrict_price_stock_payload,
    resume_requested,
)


def _meta(product: Dict[str, Any]) -> Dict[str, Any]:
    return {
        m.get("key"): m.get("value")
        for m in (product.get("meta_data") or [])
        if isinstance(m, dict) and m.get("key")
    }


def _matches(slug: str, product: Dict[str, Any]) -> bool:
    meta = _meta(product)
    src = str(meta.get("_supplier_source") or meta.get("_sg_supplier") or "").lower()
    url = str(meta.get("supplier_url") or "").lower()
    if slug == "lkq":
        return src == "lkq" or "lkq.sk" in url
    if slug == "szekop":
        return src == "szekop" or "nextis.cz" in url or "nextis." in url
    return False


def _wc_get(session, url: str, auth, params: dict):
    try:
        from shop_guard import wait_if_slow
        wait_if_slow()
    except Exception:
        pass
    return session.get(url, params=params, auth=auth, timeout=90)


def select_existing(slug: str, limit: int, start_page: int) -> tuple[List[dict], int]:
    """Next matching products. Returns (batch, next page)."""
    import requests
    base = (os.environ.get("WC_URL") or "").rstrip("/")
    key = os.environ.get("WC_CONSUMER_KEY") or ""
    secret = os.environ.get("WC_CONSUMER_SECRET") or ""
    if not (base and key and secret):
        raise RuntimeError("Chýbajú WC kľúče — dodávateľa som nekontaktoval")
    auth = (key, secret)
    session = requests.Session()
    found: List[dict] = []
    page = max(1, int(start_page or 1))
    scanned = 0
    wrapped = False
    max_pages = int(os.environ.get("SG_WC_SCAN_PAGES", "25") or 25)
    while scanned < max_pages and len(found) < limit:
        resp = _wc_get(session, f"{base}/wp-json/wc/v3/products", auth, {
            "status": "any", "per_page": 100, "page": page, "orderby": "id", "order": "asc",
            "_fields": "id,sku,regular_price,sale_price,stock_status,stock_quantity,manage_stock,meta_data",
        })
        if resp.status_code == 400 and page > 1 and not wrapped:
            wrapped = True
            page = 1
            continue
        if not resp.ok:
            raise RuntimeError(f"WooCommerce HTTP {resp.status_code} — dodávateľa som nekontaktoval")
        rows = resp.json()
        scanned += 1
        if not isinstance(rows, list) or not rows:
            if not wrapped and start_page > 1:
                wrapped = True
                page = 1
                continue
            return found, 1
        for product in rows:
            if not _matches(slug, product):
                continue
            url = str(_meta(product).get("supplier_url") or "").strip()
            if not url:
                continue
            found.append(product)
            if len(found) >= limit:
                break
        if len(rows) < 100:
            return found, 1
        page += 1
    return found, page


def _cursor_path(slug: str) -> Path:
    raw = (os.environ.get("SG_GUARD_DIR") or "").strip()
    root = Path(raw) if raw else (ROOT / "data" / "supplier_guard")
    return root / f"{slug}-cursor.json"


def _load_cursor(slug: str) -> int:
    path = _cursor_path(slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("page") or data.get("id") or 1)
    except (OSError, ValueError, TypeError):
        return 1


def _save_cursor(slug: str, cursor: int) -> None:
    path = _cursor_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"page": int(cursor)}) + "\n", encoding="utf-8")


def _clear_login_markers(slug: str) -> None:
    paths = []
    if slug == "lkq":
        paths = [
            os.environ.get("SG_LKQ_ATTEMPT_FILE", "/tmp/sg_lkq_login_attempted"),
            os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed"),
        ]
    elif slug == "szekop":
        paths = [os.environ.get("SG_SZEKOP_ATTEMPT_FILE", "/tmp/sg_szekop_login_attempted")]
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _apply_defaults(slug: str) -> None:
    if slug == "lkq":
        os.environ.setdefault("SG_GUARD_SLUG", "lkq")
        os.environ.setdefault("SG_POLITE_HOSTS", "lkq.sk")
        os.environ.setdefault("SG_POLITE_MIN", "12")
        os.environ.setdefault("SG_POLITE_MAX", "28")
        os.environ.setdefault("SG_DAILY_CAP", "40")
        os.environ.setdefault("SG_RUN_CAP", "40")
        os.environ["SG_LKQ_ONE_LOGIN"] = "1"
    else:
        os.environ.setdefault("SG_GUARD_SLUG", "szekop-b2b")
        os.environ.setdefault("SG_POLITE_HOSTS", "nextis.cz")
        os.environ.setdefault("SG_POLITE_MIN", "20")
        os.environ.setdefault("SG_POLITE_MAX", "45")
        os.environ.setdefault("SG_DAILY_CAP", "30")
        os.environ.setdefault("SG_RUN_CAP", "30")
        os.environ["SZEKOP_B2B"] = "1"
        os.environ["SG_SZEKOP_B2B_REQUIRED"] = "1"
        os.environ["SG_SZEKOP_ONE_LOGIN"] = "1"
    os.environ.setdefault("SG_HTTP_RETRIES", "0")
    os.environ["CRAWL_DELAY"] = "0"


def _login(slug: str):
    from suppliers import get_supplier
    code = "lkq" if slug == "lkq" else "szekop"
    sup = get_supplier(code)
    ok = bool(sup.ensure_login())
    if not ok:
        raise LoginFailed()
    return sup


def _payload_for(slug: str, parsed, product: dict) -> dict:
    from availability import apply_to_payload
    from pricing import price_from_offer
    from secret_crawler import _offer_from_product_raw, build_wc_payload

    raw = parsed.to_ai_dict() if hasattr(parsed, "to_ai_dict") else {}
    offer = _offer_from_product_raw(parsed)
    pricing = price_from_offer(offer)
    body: Dict[str, Any] = {}
    if pricing is not None:
        body = build_wc_payload(offer, pricing)
    apply_to_payload(body, raw)
    # Stock fields only when a price result is missing.
    if pricing is None and getattr(parsed, "stock_status", None):
        body.setdefault("stock_status", parsed.stock_status)
        if getattr(parsed, "stock_qty", None) is not None and body.get("manage_stock") is not False:
            body["manage_stock"] = True
            body["stock_quantity"] = int(parsed.stock_qty)
    return restrict_price_stock_payload(body, int(product["id"]))


def _differs(product: dict, payload: dict) -> bool:
    for key in ("regular_price", "sale_price", "stock_status"):
        if key not in payload:
            continue
        if str(product.get(key) or "") != str(payload.get(key) or ""):
            return True
    if "stock_quantity" in payload and str(product.get("stock_quantity") or "") != str(payload.get("stock_quantity") or ""):
        return True
    current = _meta(product)
    for row in payload.get("meta_data") or []:
        key = row.get("key")
        if key and str(current.get(key) or "") != str(row.get("value") or ""):
            return True
    return False


def _write(updates: List[dict]) -> int:
    if not updates:
        return 0
    import requests
    base = (os.environ.get("WC_URL") or "").rstrip("/")
    auth = (os.environ.get("WC_CONSUMER_KEY") or "", os.environ.get("WC_CONSUMER_SECRET") or "")
    try:
        from shop_guard import wait_if_slow
        wait_if_slow()
    except Exception:
        pass
    written = 0
    session = requests.Session()
    step = 10
    for i in range(0, len(updates), step):
        chunk = updates[i:i + step]
        resp = session.post(f"{base}/wp-json/wc/v3/products/batch", auth=auth, json={"update": chunk}, timeout=180)
        if not resp.ok:
            print(f"status=failed http={resp.status_code}", flush=True)
            continue
        written += len((resp.json() or {}).get("update") or [])
    return written


def _probe(slug: str):
    """One public robots/sitemap read. A block is not retried."""
    import requests
    from public_pages import UA, discover_public
    if slug == "lkq":
        from suppliers.lkq import LkqSupplier as supplier_cls
    else:
        from suppliers.szekop import SzekopSupplier as supplier_cls
    session = requests.Session()
    session.headers["User-Agent"] = UA
    return discover_public(session, supplier_cls.base_url, limit=1)


def refresh(slug: str, limit: int, *, clear_markers: bool = True) -> dict:
    """Return counts."""
    out = {
        "checked": 0, "updated": 0, "unchanged": 0, "failed": 0,
        "blocked": False, "login_ok": None, "exhausted": False, "cap": False,
        "paused": False, "reason": "",
    }
    if slug not in {"lkq", "szekop"}:
        out["reason"] = "supplier"
        return out
    if not primary_account():
        out["exhausted"] = True
        out["reason"] = "other-account"
        out["login_ok"] = None
        return out
    _apply_defaults(slug)
    install_from_env()
    g = guard(os.environ["SG_GUARD_SLUG"])
    state = g.begin(resume=resume_requested())
    probed_ok = False
    if state.get("paused") and not resume_requested():
        if slug == "lkq":
            found = _probe(slug)
            probed_ok = found.status == "ok"
            if found.status == "blocked":
                out["blocked"] = True
                out["paused"] = True
                out["reason"] = "blocked"
                print("stop=blocked", flush=True)
                return out
            if probed_ok:
                state = g.begin(resume=True)
        if state.get("paused"):
            out["paused"] = True
            out["reason"] = "paused"
            print("stop=paused", flush=True)
            return out
    if slug == "lkq" and not probed_ok:
        found = _probe(slug)
        if found.status == "blocked":
            out["blocked"] = True
            out["reason"] = "blocked"
            print("stop=blocked", flush=True)
            return out
    if int(state.get("cap_left") or 0) <= 0:
        out["cap"] = True
        out["reason"] = "cap"
        return out
    cursor = _load_cursor(slug)
    try:
        batch, new_cursor = select_existing(slug, limit, cursor)
    except RuntimeError as exc:
        out["reason"] = "shop"
        out["failed"] = 1
        print("status=failed", flush=True)
        return out
    _save_cursor(slug, new_cursor)
    if not batch:
        out["exhausted"] = True
        out["reason"] = "exhausted"
        return out
    if clear_markers:
        _clear_login_markers(slug)
    try:
        sup = _login(slug)
    except LoginFailed:
        out["login_ok"] = False
        out["reason"] = "login"
        print("stop=login", flush=True)
        return out
    except SupplierStop as exc:
        out["login_ok"] = False
        out["blocked"] = exc.kind != "login"
        out["reason"] = exc.kind
        return out
    out["login_ok"] = True
    updates: List[dict] = []
    for product in batch:
        url = str(_meta(product).get("supplier_url") or "")
        try:
            parsed = sup.parse_product(url)
        except (DailyCapReached, RunCapReached):
            out["cap"] = True
            out["reason"] = "cap"
            break
        except SupplierStop as exc:
            out["blocked"] = exc.kind != "login"
            out["login_ok"] = False if exc.kind == "login" else out["login_ok"]
            out["reason"] = exc.kind
            break
        except Exception:
            out["failed"] += 1
            continue
        out["checked"] += 1
        if not parsed:
            out["failed"] += 1
            continue
        try:
            payload = _payload_for(slug, parsed, product)
        except Exception:
            out["failed"] += 1
            continue
        if not _differs(product, payload):
            out["unchanged"] += 1
            continue
        updates.append(payload)
    if updates and not out["blocked"] and out["login_ok"] is not False:
        out["updated"] = _write(updates)
    elif updates and (out["blocked"] or out["login_ok"] is False):
        # Čo sa stihlo načítať, ešte zapíš — ďalšie fetch už nešli.
        out["updated"] = _write(updates)
    return out


def finish(slug: str, out: dict) -> int:
    ok, why = decide_outcome(
        login_ok=out.get("login_ok"),
        seen=int(out.get("seen") or 0),
        updated=int(out.get("updated") or 0),
        checked=int(out.get("checked") or 0),
        blocked=bool(out.get("blocked")),
        paused=bool(out.get("paused")),
        exhausted=bool(out.get("exhausted")),
        cap=bool(out.get("cap")),
    )
    if out.get("reason") == "shop":
        ok, why = False, "shop"
    title = "LKQ" if slug == "lkq" else "Szekop"
    job_summary(
        title,
        status="ok" if ok else "failed",
        checked=int(out.get("checked") or 0),
        updated=int(out.get("updated") or 0),
        unchanged=int(out.get("unchanged") or 0),
        failed=int(out.get("failed") or 0),
    )
    try:
        g = guard(os.environ.get("SG_GUARD_SLUG") or slug)
        if ok:
            g.note_success()
        elif why not in {"paused", "cap", "exhausted", "other-account", "shop"}:
            g.note_failure(why)
    except Exception:
        pass
    print("SG_RESULT " + json.dumps({
        "ok": ok,
        "status": "ok" if ok else "failed",
        "checked": int(out.get("checked") or 0),
        "updated": int(out.get("updated") or 0),
        "unchanged": int(out.get("unchanged") or 0),
        "failed": int(out.get("failed") or 0),
    }, ensure_ascii=False), flush=True)
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True, choices=["lkq", "szekop"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    limit = args.limit or (12 if args.supplier == "lkq" else int(os.environ.get("SG_SZEKOP_REFRESH_LIMIT", "12") or 12))
    if args.supplier == "lkq" and args.limit == 0:
        limit = int(os.environ.get("SG_LKQ_REFRESH_LIMIT", "12") or 12)
    out = refresh(args.supplier, max(1, limit))
    return finish(args.supplier, out)


if __name__ == "__main__":
    sys.exit(main())
