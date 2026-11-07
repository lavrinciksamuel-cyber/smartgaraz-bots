#!/usr/bin/env python3
"""List shop product images, remove high-confidence broken ones, reuse one placeholder.

Logs and the job summary are counts only. The row list goes to the existing HQ
alert endpoint when that token is configured. Images on other hosts are not
downloaded. 403 and 429 stop the run. Nothing is uploaded.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from image_integrity import fetch_verified, image_magic  # noqa: E402
from photo_repair import (  # noqa: E402
    HIGH_CONFIDENCE,
    accept_attachment,
    classify_for_delete,
    pick_media_id,
    placeholder_id_from_setting,
    placeholder_search,
    plan_gallery,
    ratio_exceeded,
    within_cap,
)
from supplier_pace import job_summary  # noqa: E402

LOG = logging.getLogger("photo_integrity")
REPORT = ROOT / "reports" / "photo-integrity.json"
_PAGE_SIZE = 50
_MAX_PAGES = 500
_GAP_S = 0.4
_WORKERS = 2


def _mask(value: str) -> None:
    text = (value or "").strip()
    if len(text) < 4:
        return
    print(f"::add-mask::{text}", file=sys.stderr, flush=True)


def _mask_env() -> None:
    for name in ("WC_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET", "HQ_SYNC_TOKEN", "HQ_URL"):
        _mask(os.environ.get(name) or "")
    host = urlparse((os.environ.get("WC_URL") or "").strip()).hostname or ""
    _mask(host)
    if host.lower().startswith("www."):
        _mask(host[4:])


def same_site(url: str, shop: str) -> bool:
    def key(raw: str) -> str:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        return host

    base = shop if "://" in (shop or "") else "https://" + (shop or "")
    shop_key = key(base)
    return bool(shop_key) and key(url) == shop_key


class HttpStop(RuntimeError):
    def __init__(self, status: int):
        self.status = int(status)
        super().__init__(f"http {status}")


class _Paced:
    """Start downloads a small gap apart. Transfers may overlap."""

    def __init__(self, inner, gap: float):
        self._inner = inner
        self._gap = gap
        self._lock = threading.Lock()
        self._next = 0.0

    def get(self, url, timeout=None, verify=True):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(self._next, now) + self._gap
        if wait > 0:
            time.sleep(wait)
        return self._inner.get(url, timeout=timeout, verify=verify)


def _shop() -> str:
    return (os.environ.get("WC_URL") or "").strip().rstrip("/")


def _auth():
    key = (os.environ.get("WC_CONSUMER_KEY") or "").strip()
    secret = (os.environ.get("WC_CONSUMER_SECRET") or "").strip()
    if key and secret:
        return (key, secret)
    return None


def _products_page(session, shop: str, page: int) -> tuple[list, int]:
    auth = _auth()
    if auth:
        url = shop + "/wp-json/wc/v3/products"
        params = {
            "per_page": _PAGE_SIZE,
            "page": page,
            "status": "any",
            "orderby": "id",
            "order": "asc",
            "_fields": "id,sku,images",
        }
    else:
        url = shop + "/wp-json/wc/store/v1/products"
        params = {"per_page": _PAGE_SIZE, "page": page}
    resp = None
    status = 0
    for attempt in (0, 1):
        resp = session.get(url, params=params, auth=auth, timeout=60)
        status = int(resp.status_code or 0)
        if status in (403, 429):
            raise HttpStop(status)
        if status >= 500 and attempt == 0:
            time.sleep(2)
            continue
        break
    assert resp is not None
    if status == 400 and page > 1:
        return [], page - 1
    if status != 200:
        raise RuntimeError("list")
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(type(exc).__name__) from None
    if not isinstance(data, list):
        raise RuntimeError("list")
    raw_pages = resp.headers.get("X-WP-TotalPages") or resp.headers.get("x-wp-totalpages") or ""
    try:
        pages = int(raw_pages)
    except ValueError:
        pages = page if len(data) < _PAGE_SIZE else page + 1
    return data, max(pages, 1)


def iter_pages(session, shop: str) -> Iterator[tuple[int, list]]:
    seen: set[int] = set()
    for page in range(1, _MAX_PAGES + 1):
        try:
            from shop_guard import wait_if_slow
            wait_if_slow()
        except Exception as exc:
            LOG.info("guard %s", type(exc).__name__)
        rows, pages = _products_page(session, shop, page)
        fresh = []
        for row in rows:
            try:
                pid = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            fresh.append(row)
        if not fresh:
            return
        yield page, fresh
        if page >= pages:
            return


def _public_counts(summary: dict) -> dict:
    return {
        "checked": int(summary.get("checked") or 0),
        "broken": int(summary.get("broken") or 0),
        "deleted": int(summary.get("deleted") or 0),
        "placeholder_set": int(summary.get("placeholder_set") or 0),
        "skipped": int(summary.get("skipped") or 0),
    }


def _send_hq(items: list[dict], summary: dict, extra: Optional[dict] = None) -> bool:
    token = (os.environ.get("HQ_SYNC_TOKEN") or "").strip()
    if not token or not items:
        return False
    _mask(token)
    try:
        from hq_url import hq_url
        base = hq_url()
    except Exception:
        return False
    listed = items[:2000]
    body_summary = _public_counts(summary)
    if extra:
        for key, value in extra.items():
            if isinstance(value, int):
                body_summary[key] = value
    payload = json.dumps({
        "workflow": "photo-integrity.yml",
        "repo": os.environ.get("GITHUB_REPOSITORY") or "",
        "title": "photo integrity",
        "body": json.dumps({"items": listed, "summary": body_summary}, ensure_ascii=False),
        "url": "",
        "level": "warning",
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/sync/bot-alert",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "smartgaraz-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as exc:
        LOG.info("hq http %s", int(exc.code))
    except Exception as exc:
        LOG.info("hq %s", type(exc).__name__)
    return False


def _write_report(summary: dict) -> None:
    """Counts only. The row list is not written to disk."""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps({"summary": _public_counts(summary)}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _mark_skipped(rows: list[dict], summary: dict) -> None:
    for row in rows:
        if row.get("action"):
            continue
        row["action"] = "skipped"
        summary["skipped"] = int(summary.get("skipped") or 0) + 1


def _finish(summary: dict, items: list[dict], *, status: str, extra: Optional[dict] = None) -> int:
    summary["broken"] = len(items)
    summary["reported"] = 1 if _send_hq(items, summary, extra) else 0
    _write_report(summary)
    counts = _public_counts(summary)
    job_summary("Photo integrity", status="ok" if status == "ok" else "failed", **counts)
    LOG.info(
        "checked=%s broken=%s deleted=%s placeholder_set=%s skipped=%s",
        counts["checked"], counts["broken"], counts["deleted"],
        counts["placeholder_set"], counts["skipped"],
    )
    return 0 if status == "ok" else 1


def _outcome(session, url: str) -> Optional[str]:
    """'' when the file is fine, a reason when it is corrupt, None when unread."""
    data, reason = fetch_verified(session, url, timeout=30, pause=0.8, verify=True, retries=1)
    if reason in ("http_403", "http_429"):
        raise HttpStop(int(reason.rsplit("_", 1)[-1]))
    if not reason:
        return ""
    if data and image_magic(data) and reason in HIGH_CONFIDENCE:
        return reason
    if data and image_magic(data) and reason:
        return reason
    return None


def _product_images(product: dict) -> list[tuple[int, str]]:
    found = []
    for image in product.get("images") or []:
        if not isinstance(image, dict):
            continue
        src = str(image.get("src") or "").strip()
        if not (src.startswith("http://") or src.startswith("https://")):
            continue
        try:
            media_id = int(image.get("id") or 0)
        except (TypeError, ValueError):
            media_id = 0
        found.append((media_id, src.split("?", 1)[0]))
    return found


def _check_page(session, shop: str, rows: list, summary: dict, items: list, cache: dict, placeholder_id: Optional[int]) -> None:
    jobs = []
    for row in rows:
        try:
            pid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        sku = str(row.get("sku") or "")
        for media_id, url in _product_images(row):
            if placeholder_id and media_id == placeholder_id:
                continue
            if not same_site(url, shop):
                summary["external"] += 1
                continue
            jobs.append((pid, sku, media_id, url))
    if not jobs:
        return

    lock = threading.Lock()
    url_locks: dict[str, threading.Lock] = {}

    def lock_for(url: str) -> threading.Lock:
        with lock:
            found = url_locks.get(url)
            if found is None:
                found = threading.Lock()
                url_locks[url] = found
            return found

    def run_job(job):
        pid, sku, media_id, url = job
        with lock_for(url):
            with lock:
                if url in cache:
                    return pid, sku, media_id, url, cache[url], True
            outcome = _outcome(session, url)
            with lock:
                cache[url] = outcome
            return pid, sku, media_id, url, outcome, False

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(run_job, job) for job in jobs]
        try:
                for fut in as_completed(futures):
                    pid, sku, media_id, url, outcome, cached = fut.result()
                    if not cached:
                        summary["checked"] += 1
                        if outcome is None:
                            summary["unreadable"] += 1
                    if outcome not in HIGH_CONFIDENCE:
                        continue
                    items.append({
                        "product_id": pid,
                        "sku": sku,
                        "image_id": media_id,
                        "url": url,
                        "reason": outcome,
                        "action": "",
                    })
        except HttpStop:
            for fut in futures:
                fut.cancel()
            raise


def _call(session, method: str, url: str, auth, *, stop: bool = True, **kwargs):
    timeout = kwargs.pop("timeout", 60)
    try:
        resp = session.request(method, url, auth=auth, timeout=timeout, **kwargs)
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            return None, "timeout"
        return None, "error"
    status = int(getattr(resp, "status_code", 0) or 0)
    if stop and status in (403, 429):
        raise HttpStop(status)
    if status == 429:
        raise HttpStop(status)
    return resp, status


def _media_view(row: dict) -> dict:
    title = row.get("title")
    if isinstance(title, dict):
        title = title.get("rendered") or ""
    return {
        "src": str(row.get("source_url") or ""),
        "title": str(title or ""),
        "slug": str(row.get("slug") or ""),
    }


def find_placeholder(session, shop: str, auth) -> tuple[Optional[int], int]:
    """Return ``(attachment id or None, missing 0/1)``. Never uploads."""
    if not auth:
        return None, 1
    resp, status = _call(
        session, "GET",
        shop + "/wp-json/wc/v3/settings/products/woocommerce_placeholder_image",
        auth, timeout=30,
    )
    setting_id = None
    if resp is not None and status == 200:
        try:
            setting_id = placeholder_id_from_setting(resp.json() or {})
        except Exception:
            setting_id = None
    if setting_id:
        media, media_status = _call(
            session, "GET", shop + f"/wp-json/wp/v2/media/{setting_id}", auth,
            timeout=30, stop=False,
        )
        if media is not None and media_status == 200:
            try:
                view = _media_view(media.json() or {})
            except Exception:
                view = None
            if view and accept_attachment(from_setting=True, **view):
                return setting_id, 0
    resp, status = _call(
        session, "GET", shop + "/wp-json/wp/v2/media", auth, timeout=30, stop=False,
        params={"search": placeholder_search(), "per_page": 20, "media_type": "image"},
    )
    if resp is None or status != 200:
        return None, 1
    try:
        rows = resp.json()
    except Exception:
        return None, 1
    if not isinstance(rows, list):
        return None, 1
    found = pick_media_id(rows)
    return (found, 0) if found else (None, 1)


def _recheck(session, url: str) -> str:
    _data, reason = fetch_verified(session, url, timeout=30, pause=0, verify=True, retries=0)
    decision = classify_for_delete(reason)
    if decision == "stop":
        raise HttpStop(int(str(reason).rsplit("_", 1)[-1]))
    return decision


def _delete_media(session, shop: str, auth, media_id: int) -> str:
    resp, status = _call(
        session, "DELETE",
        shop + f"/wp-json/wp/v2/media/{int(media_id)}",
        auth, timeout=40, params={"force": "true"},
    )
    if status == "timeout" or resp is None:
        return "skipped"
    if status in (200, 204, 410):
        return "deleted"
    return "skipped"


def _put_images(session, shop: str, auth, product_id: int, images: list) -> bool:
    resp, status = _call(
        session, "PUT",
        shop + f"/wp-json/wc/v3/products/{int(product_id)}",
        auth, timeout=60, json={"images": images},
    )
    return resp is not None and status in (200, 201)


def _fresh_images(session, shop: str, auth, product_id: int) -> Optional[list]:
    resp, status = _call(
        session, "GET",
        shop + f"/wp-json/wc/v3/products/{int(product_id)}",
        auth, timeout=40, params={"_fields": "id,images"},
    )
    if resp is None or status != 200:
        return None
    try:
        payload = resp.json() or {}
    except Exception:
        return None
    return payload.get("images") if isinstance(payload, dict) else None


def repair(session, shop: str, auth, items: list[dict], summary: dict, placeholder_id: Optional[int]) -> None:
    """Remove confirmed broken attachments. Does not upload."""
    if not auth:
        _mark_skipped(items, summary)
        return
    by_product: dict[int, list[dict]] = {}
    for row in items:
        if row.get("action"):
            continue
        by_product.setdefault(int(row["product_id"]), []).append(row)
    removed: set[int] = set()
    used = int(summary.get("deleted") or 0)
    for product_id, rows in by_product.items():
        confirmed = []
        for row in rows:
            if not row.get("image_id"):
                row["action"] = "skipped"
                summary["skipped"] += 1
                continue
            if _recheck(session, row["url"]) != "delete":
                row["action"] = "skipped"
                summary["skipped"] += 1
                continue
            confirmed.append(row)
        if not confirmed:
            continue
        new_ids = []
        for row in confirmed:
            media_id = int(row["image_id"])
            if media_id not in removed and media_id not in new_ids:
                new_ids.append(media_id)
        if new_ids and not within_cap(len(new_ids), used):
            _mark_skipped(confirmed, summary)
            continue
        current = _fresh_images(session, shop, auth, product_id)
        if current is None:
            _mark_skipped(confirmed, summary)
            continue
        broken_ids = {int(row["image_id"]) for row in confirmed}
        plan = plan_gallery(current, broken_ids, placeholder_id)
        if not plan["delete_ids"]:
            _mark_skipped(confirmed, summary)
            continue
        try:
            from shop_guard import wait_if_slow
            wait_if_slow()
        except Exception as exc:
            LOG.info("guard %s", type(exc).__name__)
        if not _put_images(session, shop, auth, product_id, plan["images"]):
            _mark_skipped(confirmed, summary)
            continue
        if plan["placeholder_set"]:
            summary["placeholder_set"] += 1
        action = "replaced" if plan["placeholder_set"] else "deleted"
        deleted_now: set[int] = set()
        for media_id in plan["delete_ids"]:
            if media_id in removed:
                deleted_now.add(media_id)
                continue
            if _delete_media(session, shop, auth, media_id) != "deleted":
                continue
            removed.add(media_id)
            deleted_now.add(media_id)
            summary["deleted"] += 1
            used += 1
            for row in confirmed:
                if int(row["image_id"]) == media_id:
                    row["action"] = action
        for row in confirmed:
            if row.get("action"):
                continue
            if int(row["image_id"]) in deleted_now:
                row["action"] = action
            else:
                row["action"] = "skipped"
                summary["skipped"] += 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _mask_env()
    shop = _shop()
    summary = {
        "checked": 0,
        "broken": 0,
        "deleted": 0,
        "placeholder_set": 0,
        "skipped": 0,
        "pages": 0,
        "products": 0,
        "unreadable": 0,
        "external": 0,
    }
    items: list[dict] = []
    if not shop:
        LOG.info("status=failed config")
        return _finish(summary, items, status="failed")

    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "smartgaraz-bot"
    session.headers["Accept-Encoding"] = "identity"
    paced = _Paced(session, _GAP_S)
    cache: dict[str, Optional[str]] = {}
    auth = _auth()
    placeholder_id: Optional[int] = None
    try:
        if auth:
            placeholder_id, _missing = find_placeholder(session, shop, auth)
        for page, rows in iter_pages(session, shop):
            summary["pages"] = page
            summary["products"] += len(rows)
            _check_page(paced, shop, rows, summary, items, cache, placeholder_id)
            LOG.info(
                "page=%s products=%s checked=%s broken=%s",
                page, summary["products"], summary["checked"], len(items),
            )
        extra = {"placeholder_missing": 0 if placeholder_id else 1}
        if placeholder_id:
            extra["placeholder_id"] = int(placeholder_id)
        if ratio_exceeded(int(summary["checked"]), len(items)):
            LOG.info("stop=ratio")
            _mark_skipped(items, summary)
            return _finish(summary, items, status="failed", extra=extra)
        repair(session, shop, auth, items, summary, placeholder_id)
        _mark_skipped(items, summary)
    except HttpStop as exc:
        LOG.info("stop=%s", exc.status)
        _mark_skipped(items, summary)
        extra = {"placeholder_missing": 0 if placeholder_id else 1}
        if placeholder_id:
            extra["placeholder_id"] = int(placeholder_id)
        return _finish(summary, items, status="failed", extra=extra)
    except Exception as exc:
        LOG.info("status=failed %s", type(exc).__name__)
        _mark_skipped(items, summary)
        return _finish(summary, items, status="failed")
    extra = {"placeholder_missing": 0 if placeholder_id else 1}
    if placeholder_id:
        extra["placeholder_id"] = int(placeholder_id)
    return _finish(summary, items, status="ok", extra=extra)


if __name__ == "__main__":
    raise SystemExit(main())
