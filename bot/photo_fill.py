#!/usr/bin/env python3
"""Fill a missing featured image for one supplier. Counts only in the log.

Uses that supplier's existing catalog access. Does not upload a new placeholder
and does not delete the shared placeholder attachment. 403 and 429 stop the run.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from image_integrity import fetch_verified, upload_media  # noqa: E402
from photo_repair import matches_placeholder  # noqa: E402
from supplier_pace import job_summary  # noqa: E402

LOG = logging.getLogger("photo_fill")
BATCH = 6
SUPPLIERS = ("lkq", "szekop", "wurth")
_SKIP = frozenset({"no_ref", "no_public"})


class StopFill(RuntimeError):
    def __init__(self, status: int):
        self.status = int(status)
        super().__init__(f"http {status}")


def empty_state() -> dict:
    return {"todo": [], "done": [], "failed": {}, "last_checked": ""}


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    state = empty_state()
    if isinstance(raw, dict):
        state["todo"] = [int(i) for i in raw.get("todo") or [] if str(i).isdigit() or isinstance(i, int)]
        state["done"] = [int(i) for i in raw.get("done") or [] if str(i).isdigit() or isinstance(i, int)]
        failed = raw.get("failed") or {}
        if isinstance(failed, dict):
            clean = {}
            for key, val in failed.items():
                if not (str(key).lstrip("-").isdigit() or isinstance(key, int)):
                    continue
                if not isinstance(val, dict):
                    val = {"reason": "failed", "last_checked": ""}
                clean[str(int(key))] = val
            state["failed"] = clean
        state["last_checked"] = str(raw.get("last_checked") or "")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")


def product_supplier(source: str) -> str:
    src = (source or "").casefold().replace("ü", "u")
    if "lkq" in src:
        return "lkq"
    if "szekop" in src:
        return "szekop"
    if "wurth" in src:
        return "wurth"
    return ""


def needs_fill(images, placeholder_id: Optional[int]) -> bool:
    rows = list(images or [])
    if not rows:
        return True
    first = rows[0] if isinstance(rows[0], dict) else {}
    try:
        media_id = int(first.get("id") or 0)
    except (TypeError, ValueError):
        media_id = 0
    if placeholder_id and media_id == placeholder_id:
        return True
    src = str(first.get("src") or "")
    title = str(first.get("name") or first.get("alt") or "")
    slug = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return matches_placeholder(title, slug, src)


def sync_candidates(state: dict, candidate_ids: list[int]) -> dict:
    cand: list[int] = []
    seen_c: set[int] = set()
    for raw in candidate_ids:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in seen_c:
            seen_c.add(i)
            cand.append(i)
    cand_set = set(cand)
    done = [int(i) for i in state.get("done") or [] if int(i) not in cand_set]
    failed_in = state.get("failed") or {}
    failed = {}
    for key, val in failed_in.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if i in cand_set:
            failed[str(i)] = val
    todo: list[int] = []
    seen: set[int] = set()
    for raw in list(state.get("todo") or []):
        i = int(raw)
        if i in cand_set and i not in seen and str(i) not in failed:
            seen.add(i)
            todo.append(i)
    for i in cand:
        if i in seen or str(i) in failed:
            continue
        seen.add(i)
        todo.append(i)
    return {
        "todo": todo,
        "done": done,
        "failed": failed,
        "last_checked": str(state.get("last_checked") or ""),
    }


def next_ids(state: dict, batch: int, today: str) -> list[int]:
    out: list[int] = []
    for raw in state.get("todo") or []:
        out.append(int(raw))
        if len(out) >= batch:
            return out
    for key, val in (state.get("failed") or {}).items():
        checked = str((val or {}).get("last_checked") or "")
        if today and checked.startswith(today):
            continue
        i = int(key)
        if i not in out:
            out.append(i)
        if len(out) >= batch:
            break
    return out


def mark(state: dict, product_id: int, status: str, reason: str, now: str) -> dict:
    pid = int(product_id)
    state["todo"] = [int(i) for i in state.get("todo") or [] if int(i) != pid]
    state["done"] = [int(i) for i in state.get("done") or [] if int(i) != pid]
    failed = dict(state.get("failed") or {})
    failed.pop(str(pid), None)
    state["last_checked"] = now
    if status == "done":
        state["done"].append(pid)
    elif status == "failed":
        failed[str(pid)] = {"reason": reason or "failed", "last_checked": now}
    elif status == "todo" and pid not in state["todo"]:
        state["todo"].append(pid)
    state["failed"] = failed
    return state


def featured_gallery(images, *, new_id: Optional[int] = None, new_src: str = "", placeholder_id: Optional[int] = None) -> list[dict]:
    """Featured image first. Placeholder stays in the library and leaves this product."""
    if new_id:
        first: dict = {"id": int(new_id)}
    elif new_src:
        first = {"src": new_src}
    else:
        raise ValueError("missing image")
    out = [first]
    seen = {int(new_id)} if new_id else set()
    for image in images or []:
        if not isinstance(image, dict):
            continue
        try:
            media_id = int(image.get("id") or 0)
        except (TypeError, ValueError):
            media_id = 0
        if media_id <= 0 or media_id in seen:
            continue
        if placeholder_id and media_id == placeholder_id:
            continue
        src = str(image.get("src") or "")
        title = str(image.get("name") or image.get("alt") or "")
        slug = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if matches_placeholder(title, slug, src):
            continue
        seen.add(media_id)
        out.append({"id": media_id})
    return out


def _mask(value: str) -> None:
    text = (value or "").strip()
    if len(text) >= 4:
        print(f"::add-mask::{text}", file=sys.stderr, flush=True)


def _quiet_loggers() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("suppliers", "suppliers.lkq", "suppliers.szekop", "wurth_photo_backfill", "urllib3"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _meta(product: dict) -> dict:
    out = {}
    for row in product.get("meta_data") or []:
        if isinstance(row, dict) and row.get("key"):
            out[str(row["key"])] = row.get("value") or ""
    return out


def _wc_get(session, url: str, auth, params: dict):
    try:
        resp = session.get(url, auth=auth, params=params, timeout=60)
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            raise StopFill(0) from None
        raise
    status = int(resp.status_code or 0)
    if status in (403, 429):
        raise StopFill(status)
    return resp, status


def list_candidates(session, shop: str, auth, supplier: str, placeholder_id: Optional[int]) -> list[dict]:
    found = []
    seen: set[int] = set()
    for page in range(1, 501):
        try:
            from shop_guard import wait_if_slow
            wait_if_slow()
        except Exception as exc:
            LOG.info("guard %s", type(exc).__name__)
        resp, status = _wc_get(
            session,
            shop + "/wp-json/wc/v3/products",
            auth,
            {
                "per_page": 50,
                "page": page,
                "status": "any",
                "orderby": "id",
                "order": "asc",
                "_fields": "id,sku,images,meta_data",
            },
        )
        if status == 400 and page > 1:
            break
        if status != 200:
            raise RuntimeError("list")
        try:
            rows = resp.json()
        except Exception as exc:
            raise RuntimeError(type(exc).__name__) from None
        if not isinstance(rows, list) or not rows:
            break
        fresh = False
        for row in rows:
            try:
                pid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            fresh = True
            meta = _meta(row)
            if product_supplier(str(meta.get("_supplier_source") or meta.get("_sg_supplier") or "")) != supplier:
                continue
            if not needs_fill(row.get("images") or [], placeholder_id):
                continue
            found.append({
                "id": pid,
                "sku": str(row.get("sku") or ""),
                "images": row.get("images") or [],
                "supplier_url": str(meta.get("supplier_url") or ""),
            })
        if not fresh or len(rows) < 50:
            break
    return found


def _raise_supplier_stop(exc: BaseException) -> None:
    from supplier_pace import SupplierStop
    if isinstance(exc, SupplierStop):
        raise StopFill(int(getattr(exc, "status", None) or 403)) from None


def szekop_image_url(supplier_url: str) -> tuple[str, str]:
    os.environ["SZEKOP_B2B"] = "0"
    os.environ["SG_SZEKOP_PHASE"] = "1"
    if not supplier_url:
        return "", "no_ref"
    from suppliers import get_supplier
    supplier = get_supplier("szekop")
    if not getattr(supplier, "_public_only", False):
        return "", "no_public"
    try:
        product = supplier.parse_product(supplier_url)
    except Exception as exc:
        _raise_supplier_stop(exc)
        return "", "no_image"
    images = list(getattr(product, "existing_images", None) or [])
    return (images[0], "") if images else ("", "no_image")


def lkq_image_url(supplier_url: str, sku: str = "") -> tuple[str, str]:
    if not supplier_url and not sku:
        return "", "no_ref"
    import requests
    from lkq_public import image_for, is_public_host
    from public_pages import UA
    session = requests.Session()
    session.headers["User-Agent"] = UA
    image, reason = image_for(session, supplier_url, sku)
    if reason in {"http_403", "http_429"}:
        raise StopFill(int(reason.rsplit("_", 1)[-1]))
    if image:
        return image, ""
    if not supplier_url or is_public_host(supplier_url):
        return "", reason or "no_image"
    from suppliers.hq_credentials import export_logins
    export_logins("lkq")
    from suppliers import get_supplier
    supplier = get_supplier("lkq")
    try:
        product = supplier.parse_product(supplier_url)
    except Exception as exc:
        _raise_supplier_stop(exc)
        return "", "no_image"
    images = list(getattr(product, "existing_images", None) or [])
    return (images[0], "") if images else ("", "no_image")


def wurth_image_url(sku: str, ctx: dict) -> tuple[str, str]:
    from wurth_photo_backfill import match_article, normalize_article
    article = normalize_article(sku)
    if not article:
        return "", "no_ref"
    try:
        found = match_article(
            article,
            ba_pages=ctx.get("ba") or {},
            kos_pages=ctx.get("kos") or {},
            http=ctx["http"],
        )
    except Exception as exc:
        _raise_supplier_stop(exc)
        return "", "no_public"
    if not found or not found.urls:
        return "", "no_public"
    return found.urls[0], ""


def load_wurth_context() -> dict:
    from wurth_photo_backfill import (
        BA_HOST,
        BA_SITEMAP,
        KOS_HOST,
        KOS_SITEMAP,
        PublicHttp,
        article_from_ba_product_url,
        article_from_kosovo_url,
        load_article_index,
    )
    os.environ.setdefault("SG_POLITE_HOSTS", f"{BA_HOST},{KOS_HOST}")
    os.environ.setdefault("SG_POLITE_MIN", "12")
    os.environ.setdefault("SG_POLITE_MAX", "28")
    http = PublicHttp(pause_s=0)
    try:
        ba = load_article_index(http, BA_SITEMAP, article_from_ba_product_url)
    except Exception as exc:
        _raise_supplier_stop(exc)
        ba = {}
    try:
        kos = load_article_index(http, KOS_SITEMAP, article_from_kosovo_url)
    except Exception as exc:
        _raise_supplier_stop(exc)
        kos = {}
    return {"http": http, "ba": ba, "kos": kos}


def _download(session, url: str) -> tuple[Optional[bytes], str]:
    try:
        data, reason = fetch_verified(session, url, timeout=40, pause=0, verify=True, retries=0)
    except Exception as exc:
        _raise_supplier_stop(exc)
        if "timeout" in type(exc).__name__.lower():
            raise StopFill(0) from None
        return None, "download"
    if reason in {"http_403", "http_429"}:
        raise StopFill(int(reason.rsplit("_", 1)[-1]))
    if reason:
        return None, "integrity"
    return data, ""


def _apply(session, shop: str, auth, item: dict, data: bytes, placeholder_id: Optional[int]) -> str:
    media_id, url, status = upload_media(data, f"f{int(item['id'])}.jpg")
    if status in (403, 429):
        raise StopFill(status)
    if status == 0 or (not media_id and not url):
        return "upload"
    gallery = featured_gallery(
        item.get("images") or [],
        new_id=media_id,
        new_src=url or "",
        placeholder_id=placeholder_id,
    )
    try:
        resp = session.put(
            shop + f"/wp-json/wc/v3/products/{int(item['id'])}",
            auth=auth,
            json={"images": gallery},
            timeout=60,
        )
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            raise StopFill(0) from None
        return "upload"
    code = int(resp.status_code or 0)
    if code in (403, 429):
        raise StopFill(code)
    if code not in (200, 201):
        return "upload"
    return ""


def _source_url(supplier: str, item: dict, ctx: Optional[dict]) -> tuple[str, str]:
    if supplier == "szekop":
        return szekop_image_url(item.get("supplier_url") or "")
    if supplier == "lkq":
        return lkq_image_url(item.get("supplier_url") or "", item.get("sku") or "")
    if supplier == "wurth":
        return wurth_image_url(item.get("sku") or "", ctx or {})
    return "", "no_ref"


def run(supplier: str, *, batch: int = BATCH) -> int:
    _quiet_loggers()
    for name in ("WC_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET", "HQ_SYNC_TOKEN", "HQ_URL"):
        _mask(os.environ.get(name) or "")
    shop = (os.environ.get("WC_URL") or "").strip().rstrip("/")
    key = (os.environ.get("WC_CONSUMER_KEY") or "").strip()
    secret = (os.environ.get("WC_CONSUMER_SECRET") or "").strip()
    summary = {"checked": 0, "filled": 0, "failed": 0, "skipped": 0, "todo": 0, "stopped": 0}
    state_path = ROOT / "data" / "photo_fill" / f"{supplier}.json"
    state = load_state(state_path)
    if supplier not in SUPPLIERS or not shop or not key or not secret:
        LOG.info("status=failed config")
        job_summary("Photo fill", status="failed", **summary)
        return 1

    import requests
    session = requests.Session()
    session.headers["User-Agent"] = "smartgaraz-bot"
    session.headers["Accept-Encoding"] = "identity"
    auth = (key, secret)
    os.environ.setdefault("SG_POLITE_MIN", "12")
    os.environ.setdefault("SG_POLITE_MAX", "28")
    os.environ.setdefault("SG_HTTP_RETRIES", "0")
    os.environ["CRAWL_DELAY"] = "0"
    if supplier == "szekop":
        os.environ["SZEKOP_B2B"] = "0"
        os.environ["SG_SZEKOP_PHASE"] = "1"
    ctx = None
    placeholder_id = None
    try:
        from photo_integrity import HttpStop, find_placeholder
        try:
            placeholder_id, _missing = find_placeholder(session, shop, auth)
        except HttpStop as exc:
            raise StopFill(exc.status) from None
        wurth_allowed = True
        if supplier == "wurth":
            from media_licensing import wurth_media_allowed
            wurth_allowed = wurth_media_allowed()
            if wurth_allowed:
                ctx = load_wurth_context()
        candidates = list_candidates(session, shop, auth, supplier, placeholder_id)
        state = sync_candidates(state, [row["id"] for row in candidates])
        by_id = {int(row["id"]): row for row in candidates}
        chosen = next_ids(state, batch, _today())
        for pid in chosen:
            item = by_id.get(int(pid))
            if not item:
                mark(state, pid, "done", "", _now())
                continue
            summary["checked"] += 1
            if supplier == "wurth" and not wurth_allowed:
                mark(state, pid, "failed", "no_public", _now())
                summary["skipped"] += 1
                continue
            try:
                image_url, reason = _source_url(supplier, item, ctx)
            except StopFill:
                raise
            if reason:
                mark(state, pid, "failed", reason, _now())
                if reason in _SKIP:
                    summary["skipped"] += 1
                else:
                    summary["failed"] += 1
                continue
            data, reason = _download(session, image_url)
            if reason or not data:
                mark(state, pid, "failed", reason or "download", _now())
                summary["failed"] += 1
                continue
            outcome = _apply(session, shop, auth, item, data, placeholder_id)
            if outcome:
                mark(state, pid, "failed", outcome, _now())
                summary["failed"] += 1
                continue
            mark(state, pid, "done", "", _now())
            summary["filled"] += 1
    except StopFill as exc:
        summary["stopped"] = exc.status
        LOG.info("stop=%s", exc.status)
        summary["todo"] = len(state.get("todo") or [])
        save_state(state_path, state)
        job_summary("Photo fill", status="failed", **summary)
        _write_report(supplier, summary)
        return 1
    except Exception as exc:
        LOG.info("status=failed %s", type(exc).__name__)
        summary["todo"] = len(state.get("todo") or [])
        save_state(state_path, state)
        job_summary("Photo fill", status="failed", **summary)
        _write_report(supplier, summary)
        return 1
    summary["todo"] = len(state.get("todo") or [])
    save_state(state_path, state)
    job_summary("Photo fill", status="ok", **summary)
    _write_report(supplier, summary)
    LOG.info(
        "checked=%s filled=%s failed=%s skipped=%s todo=%s",
        summary["checked"], summary["filled"], summary["failed"], summary["skipped"], summary["todo"],
    )
    return 0


def _write_report(supplier: str, summary: dict) -> None:
    path = ROOT / "reports" / f"photo-fill-{supplier}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    public = {k: int(v) for k, v in summary.items() if isinstance(v, int)}
    path.write_text(json.dumps({"summary": public}) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    supplier = ""
    batch = BATCH
    args = list(argv)
    while args:
        cur = args.pop(0)
        if cur == "--supplier" and args:
            supplier = args.pop(0).strip().lower()
        elif cur == "--batch" and args:
            try:
                batch = max(1, int(args.pop(0)))
            except ValueError:
                batch = BATCH
    if supplier not in SUPPLIERS:
        LOG.info("status=failed config")
        return 1
    return run(supplier, batch=batch)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
