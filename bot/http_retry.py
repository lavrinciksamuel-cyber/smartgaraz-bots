"""Self-healing HTTP for every bot (auto-installed by bot/public_ci/sitecustomize.py).

Every requests.Session (and therefore requests.get/post/...) gets an adapter that
retries *transient* errors with exponential backoff:
  - connection errors (request never reached the server) – any method,
  - read timeouts, broken/odd responses (e.g. "got more than 100 headers"),
    HTTP 429/500/502/503/504 – only for idempotent requests: GET/HEAD/PUT/PATCH/
    DELETE/OPTIONS, and Woo POST updates (POST …/products/<id> or …/batch with
    only "update"/"delete" – never "create", so a retry can't duplicate products).
4xx such as 401/403/404 are NOT retried (a supplier block stays a block – no evasion).
Retry-After is respected. Disable with SG_HTTP_RETRY=0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"})
_STATUS = (429, 500, 502, 503, 504)
_SAFE_POST = re.compile(r"/wp-json/wc/v3/(products|orders)/(\d+|batch)(/|\?|$)")
_installed = False


def _safe_post(request) -> bool:
    if (request.method or "").upper() != "POST":
        return False
    url = request.url or ""
    m = _SAFE_POST.search(url)
    if not m:
        return False
    if m.group(2) != "batch":
        return True  # update of an existing id
    body = request.body
    if not body:
        return False
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:
        return False
    return isinstance(data, dict) and not data.get("create")


def _log(msg: str) -> None:
    try:
        sys.__stderr__.write(f"[http-retry] {msg}\n")
        sys.__stderr__.flush()
    except Exception:
        pass


def _token(value: str, *, allow_slash: bool = False) -> str:
    text = (value or "").split(";", 1)[0].strip()
    if not text or len(text) > 64 or "http" in text.lower() or any(ch.isspace() for ch in text):
        return "-"
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.+-_")
    if allow_slash:
        allowed.add("/")
    if any(ch not in allowed for ch in text):
        return "-"
    return text


def note_wc_forbidden(response) -> None:
    """Log a shop 403 without the URL, host, or body text."""
    if int(getattr(response, "status_code", 0) or 0) != 403:
        return
    url = str(getattr(response, "url", "") or "")
    if "/wp-json/wc/" not in url:
        return
    headers = getattr(response, "headers", None) or {}
    try:
        body = response.text or ""
    except Exception:
        body = ""
    ctype = _token(str(headers.get("content-type") or ""), allow_slash=True)
    kind = "html-challenge"
    stripped = body.lstrip()
    looks_json = stripped.startswith("{") or stripped.startswith("[") or "json" in ctype
    if looks_json:
        code = ""
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            raw = data.get("code") or ""
            if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw):
                code = raw
        kind = code or "json"
    print(
        "shop-403 status=403"
        f" cf-mitigated={_token(str(headers.get('cf-mitigated') or ''))}"
        f" server={_token(str(headers.get('server') or ''))}"
        f" content-type={ctype}"
        f" body={kind}",
        flush=True,
    )


def _wrap_send(requests) -> None:
    orig = requests.Session.send

    def send(self, request, **kwargs):
        response = orig(self, request, **kwargs)
        try:
            note_wc_forbidden(response)
        except Exception:
            pass
        return response

    requests.Session.send = send


def install() -> bool:
    global _installed
    if _installed:
        return True
    try:
        import requests
    except Exception:
        return False
    _wrap_send(requests)
    if os.environ.get("SG_HTTP_RETRY", "1").strip() in {"0", "false", "no"}:
        _installed = True
        return True
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except Exception:
        _installed = True
        return True

    tries = max(0, int(os.environ.get("SG_HTTP_RETRIES", "4")))
    kw = dict(total=tries, connect=tries, read=tries, status=tries, other=tries,
              backoff_factor=2, status_forcelist=_STATUS, allowed_methods=_IDEMPOTENT,
              respect_retry_after_header=True, raise_on_status=False)
    try:
        retry = Retry(backoff_max=60, **kw)
    except TypeError:  # urllib3 < 2
        retry = Retry(**kw)

    transient = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                 requests.exceptions.ChunkedEncodingError)

    class SGRetryAdapter(HTTPAdapter):
        def send(self, request, *a, **k):
            if not _safe_post(request):
                return super().send(request, *a, **k)
            for i in range(tries + 1):
                try:
                    r = super().send(request, *a, **k)
                except transient as e:
                    if i >= tries:
                        raise
                    _log(f"POST update retry {i + 1}/{tries} po chybe {type(e).__name__}")
                else:
                    if r.status_code not in _STATUS or i >= tries:
                        return r
                    _log(f"POST update retry {i + 1}/{tries} po HTTP {r.status_code}")
                time.sleep(min(60, 5 * (3 ** i)))

    orig_init = requests.Session.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        ad = SGRetryAdapter(max_retries=retry, pool_maxsize=32)
        self.mount("https://", ad)
        self.mount("http://", ad)

    requests.Session.__init__ = _init
    _installed = True
    return True
