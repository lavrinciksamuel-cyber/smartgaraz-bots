#!/usr/bin/env python3
"""List shop product images and record ones that fail the integrity check.

Logs and the job summary are counts only. The row list (id, sku, url, reason)
is sent to the existing HQ alert endpoint when that token is configured.
Images on other hosts are not downloaded. 403 and 429 stop the run.
Stored files are not replaced.
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

from image_integrity import CORRUPT_REASONS, fetch_verified, image_magic  # noqa: E402
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


def _image_urls(product: dict) -> list[str]:
    found = []
    for image in product.get("images") or []:
        if not isinstance(image, dict):
            continue
        src = str(image.get("src") or "").strip()
        if src.startswith("http://") or src.startswith("https://"):
            found.append(src.split("?", 1)[0])
    return found


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


def _send_hq(items: list[dict], summary: dict) -> bool:
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
    summary["listed"] = len(listed)
    payload = json.dumps({
        "workflow": "photo-integrity.yml",
        "repo": os.environ.get("GITHUB_REPOSITORY") or "",
        "title": "photo integrity",
        "body": json.dumps({"items": listed, "summary": summary}, ensure_ascii=False),
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
    """Counts only. The row list is not written to disk (public logs/artifacts)."""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in summary.items() if k != "items"}
    REPORT.write_text(
        json.dumps({"summary": public}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _finish(summary: dict, items: list[dict], *, status: str) -> int:
    summary["broken"] = len(items)
    summary["listed"] = min(len(items), 2000)
    if not summary.get("reasons"):
        summary.pop("reasons", None)
    summary["reported"] = 1 if _send_hq(items, summary) else 0
    summary["repaired"] = 0
    _write_report(summary)
    counts = {
        "checked": int(summary.get("checked") or 0),
        "broken": int(summary["broken"]),
        "repaired": 0,
        "reported": int(summary["reported"]),
        "pages": int(summary.get("pages") or 0),
        "products": int(summary.get("products") or 0),
        "stopped": int(summary.get("stopped") or 0),
        "unreadable": int(summary.get("unreadable") or 0),
        "external": int(summary.get("external") or 0),
    }
    for key, value in (summary.get("reasons") or {}).items():
        if isinstance(value, int):
            counts[str(key)] = value
    job_summary("Photo integrity", status="ok" if status == "ok" else "failed", **counts)
    LOG.info(
        "checked=%s broken=%s repaired=0 reported=%s stopped=%s",
        counts["checked"], counts["broken"], counts["reported"], counts["stopped"],
    )
    return 0 if status == "ok" else 1


def _outcome(session, url: str) -> Optional[str]:
    """'' when the file is fine, a reason when it is corrupt, None when unread."""
    data, reason = fetch_verified(session, url, timeout=30, pause=0.8, verify=True, retries=1)
    if reason in ("http_403", "http_429"):
        raise HttpStop(int(reason.rsplit("_", 1)[-1]))
    if not reason:
        return ""
    if data and image_magic(data) and reason in CORRUPT_REASONS:
        return reason
    return None


def _check_page(session, shop: str, rows: list, summary: dict, items: list, cache: dict) -> None:
    jobs = []
    for row in rows:
        try:
            pid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        sku = str(row.get("sku") or "")
        for url in _image_urls(row):
            if not same_site(url, shop):
                summary["external"] += 1
                continue
            jobs.append((pid, sku, url))
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
        pid, sku, url = job
        with lock_for(url):
            with lock:
                if url in cache:
                    return pid, sku, url, cache[url], True
            outcome = _outcome(session, url)
            with lock:
                cache[url] = outcome
            return pid, sku, url, outcome, False

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(run_job, job) for job in jobs]
        try:
            for fut in as_completed(futures):
                pid, sku, url, outcome, cached = fut.result()
                if not cached:
                    summary["checked"] += 1
                    if outcome is None:
                        summary["unreadable"] += 1
                if not outcome:
                    continue
                summary["reasons"][outcome] = int(summary["reasons"].get(outcome) or 0) + 1
                items.append({
                    "product_id": pid,
                    "sku": sku,
                    "url": url,
                    "reason": outcome,
                })
        except HttpStop:
            for fut in futures:
                fut.cancel()
            raise


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _mask_env()
    shop = _shop()
    summary = {
        "checked": 0,
        "broken": 0,
        "repaired": 0,
        "pages": 0,
        "products": 0,
        "stopped": 0,
        "unreadable": 0,
        "external": 0,
        "reasons": {},
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
    try:
        for page, rows in iter_pages(session, shop):
            summary["pages"] = page
            summary["products"] += len(rows)
            _check_page(paced, shop, rows, summary, items, cache)
            LOG.info(
                "page=%s products=%s checked=%s broken=%s",
                page, summary["products"], summary["checked"], len(items),
            )
    except HttpStop as exc:
        summary["stopped"] = exc.status
        LOG.info("stop=%s", exc.status)
        return _finish(summary, items, status="failed")
    except Exception as exc:
        LOG.info("status=failed %s", type(exc).__name__)
        return _finish(summary, items, status="failed")
    return _finish(summary, items, status="ok")


if __name__ == "__main__":
    raise SystemExit(main())
