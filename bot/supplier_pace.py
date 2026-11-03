#!/usr/bin/env python3
"""Request pacing and run outcome for scheduled supplier jobs.

Logs and the job summary are counts and status only.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Europe/Bratislava")
except Exception:  # pragma: no cover
    _TZ = timezone(timedelta(hours=1))

ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = ROOT / "data" / "supplier_guard"

_CHALLENGE = (
    "cf-challenge",
    "cf-browser-verification",
    "/cdn-cgi/challenge",
    "challenge-platform",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "just a moment",
    "attention required",
    "please verify you are human",
    "verify you are a human",
)

# Meta, ktoré smie prepísať obnovenie ceny/skladu. Fitment a OE sa nedotýkajú.
PRICE_STOCK_META = frozenset({
    "_cost_price",
    "_supplier_stock",
    "_delivery_days",
    "_delivery_text",
    "_last_b2b_sync",
    "_sg_avail",
    "_sg_avail_src",
    "_sg_avail_checked",
    "_sg_avail_oos",
})
PRICE_STOCK_FIELDS = frozenset({
    "regular_price",
    "sale_price",
    "stock_status",
    "manage_stock",
    "stock_quantity",
    "meta_data",
})

_INSTALLED = False
_GUARDS: dict[str, "Guard"] = {}
_LAST_HEARTBEAT = 0.0


class SupplierStop(RuntimeError):
    """Stop this run."""

    kind = "stop"

    def __init__(self, kind: str = "stop", status: Optional[int] = None):
        self.kind = kind
        self.status = status
        super().__init__(f"supplier stop kind={kind} http={status}")


class SupplierBlocked(SupplierStop):
    def __init__(self, kind: str = "blocked", status: Optional[int] = None):
        super().__init__(kind=kind, status=status)


class LoginFailed(SupplierStop):
    def __init__(self, status: Optional[int] = None):
        super().__init__(kind="login", status=status)


class DailyCapReached(SupplierStop):
    def __init__(self):
        super().__init__(kind="cap", status=None)


class RunCapReached(SupplierStop):
    def __init__(self):
        super().__init__(kind="run_cap", status=None)


def resume_requested() -> bool:
    return (os.environ.get("SG_GUARD_RESUME") or "").strip().lower() in {"1", "true", "yes"}


def primary_account() -> bool:
    """True when this runner should execute the scheduled job."""
    return (os.environ.get("SG_ACCOUNT_INDEX") or "0").strip() in {"", "0"}


def today_key() -> str:
    return datetime.now(_TZ).date().isoformat()


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_challenge(status: Optional[int], text: str = "") -> bool:
    """True when the response should stop the run."""
    if status in (401, 403, 429):
        return True
    body = (text or "")[:8000].lower()
    return any(mark in body for mark in _CHALLENGE)


def live_fetch_skipped(url: str, source: str = "") -> bool:
    """True when a live fetch for this product is skipped."""
    host = (urlparse(url or "").hostname or "").lower()
    src = (source or "").lower()
    if host == "eshop.wurth.sk" or host.endswith(".eshop.wurth.sk"):
        return True
    if "lkq" in host or src == "lkq":
        return True
    if "nextis" in host or src == "szekop":
        return True
    return False


def polite_hosts() -> list[str]:
    return [h.strip().lower() for h in os.environ.get("SG_POLITE_HOSTS", "").split(",") if h.strip()]


def host_matches(host: str, suffixes: list[str]) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    for s in suffixes:
        if h == s or h.endswith("." + s):
            return True
    return False


def decide_outcome(
    *,
    login_ok: Optional[bool] = None,
    seen: int = 0,
    new: int = 0,
    updated: int = 0,
    checked: int = 0,
    skipped_existing: int = 0,
    blocked: bool = False,
    paused: bool = False,
    exhausted: bool = False,
    cap: bool = False,
) -> tuple[bool, str]:
    """Whether the run succeeded. Returns (ok, status)."""
    if paused:
        return False, "paused"
    if blocked:
        return False, "blocked"
    if login_ok is False:
        return False, "login"
    if exhausted and checked <= 0 and seen <= 0 and new <= 0 and updated <= 0:
        return True, "exhausted"
    if cap and login_ok is not False:
        return True, "cap"
    if checked > 0 or seen > 0 or new > 0 or updated > 0 or skipped_existing > 0:
        return True, "work"
    return False, "empty"


def mask_secret(value: str) -> None:
    """Register a secret with the Actions log masker. Prints no other text."""
    if value:
        print(f"::add-mask::{value}", flush=True)


def job_summary(title: str, *, status: str, **counts: Any) -> None:
    """Counts and status only. Free text is dropped."""
    lines = [f"### {title}", "", f"- status: {'ok' if status == 'ok' else 'failed'}"]
    for key, value in counts.items():
        if isinstance(value, bool) or isinstance(value, int):
            lines.append(f"- {key}: {int(value)}")
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + text + "\n")
    except OSError:
        pass


def _guard_dir() -> Path:
    raw = (os.environ.get("SG_GUARD_DIR") or "").strip()
    return Path(raw) if raw else DEFAULT_DIR


class Guard:
    """Súborový stav jedného slugu (cache medzi behmi)."""

    def __init__(self, slug: str, root: Optional[Path] = None):
        self.slug = slug
        self.root = root or _guard_dir()
        self.path = self.root / f"{slug}.json"
        self.sleep_min = env_float("SG_POLITE_MIN", 12)
        self.sleep_max = env_float("SG_POLITE_MAX", 28)
        self.daily_cap = env_int("SG_DAILY_CAP", 40)
        self.run_cap = env_int("SG_RUN_CAP", 40)
        self.fail_limit = env_int("SG_GUARD_FAILS", 2)

    def _empty(self) -> dict:
        return {
            "day": today_key(),
            "requests": 0,
            "run_requests": 0,
            "run_id": "",
            "failures": 0,
            "paused": False,
            "paused_reason": "",
        }

    def _load(self) -> dict:
        data = self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, ValueError):
            pass
        if data.get("day") != today_key():
            data["day"] = today_key()
            data["requests"] = 0
        run_id = os.environ.get("GITHUB_RUN_ID") or f"pid-{os.getpid()}"
        if data.get("run_id") != run_id:
            data["run_id"] = run_id
            data["run_requests"] = 0
        data["paused"] = bool(data.get("paused"))
        data["failures"] = int(data.get("failures") or 0)
        data["requests"] = int(data.get("requests") or 0)
        data["run_requests"] = int(data.get("run_requests") or 0)
        return data

    def _save(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _mutate(self, fn) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover
            fcntl = None
        with open(self.path, "r+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load()
                fn(data)
                text = json.dumps(data, ensure_ascii=False) + "\n"
                fh.seek(0)
                fh.write(text)
                fh.truncate()
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return data

    def snapshot(self) -> dict:
        if not self.path.exists():
            return self._empty()
        return self._load()

    def begin(self, *, resume: bool = False) -> dict:
        def fn(data: dict) -> None:
            if resume:
                data["paused"] = False
                data["failures"] = 0
                data["paused_reason"] = ""

        if resume or self.path.exists():
            data = self._mutate(fn)
        else:
            data = self._empty()
            if resume:
                data["paused"] = False
                data["failures"] = 0
                self._save(data)
        data["cap_left"] = max(0, self.daily_cap - int(data["requests"]))
        data["run_left"] = max(0, self.run_cap - int(data["run_requests"]))
        return data

    def note_success(self) -> None:
        def fn(data: dict) -> None:
            data["failures"] = 0
            data["paused"] = False
            data["paused_reason"] = ""

        self._mutate(fn)

    def note_failure(self, kind: str) -> dict:
        def fn(data: dict) -> None:
            data["failures"] = int(data.get("failures") or 0) + 1
            if data["failures"] >= self.fail_limit:
                data["paused"] = True
                data["paused_reason"] = kind

        return self._mutate(fn)

    def before_request(self) -> None:
        """Count one request or stop when the cap is reached."""
        blocked = {"raise": None}

        def fn(data: dict) -> None:
            if int(data["requests"]) >= self.daily_cap:
                blocked["raise"] = DailyCapReached()
                return
            if int(data["run_requests"]) >= self.run_cap:
                blocked["raise"] = RunCapReached()
                return
            data["requests"] = int(data["requests"]) + 1
            data["run_requests"] = int(data["run_requests"]) + 1

        self._mutate(fn)
        if blocked["raise"] is not None:
            raise blocked["raise"]
        lo = min(self.sleep_min, self.sleep_max)
        hi = max(self.sleep_min, self.sleep_max)
        if hi > 0:
            time.sleep(random.uniform(lo, hi))
        maybe_heartbeat(self.slug)

    def after_response(self, status: Optional[int], text: str = "") -> None:
        if status in (401,):
            raise LoginFailed(status=status)
        if is_challenge(status, text):
            raise SupplierBlocked(kind="blocked", status=status)


def guard(slug: str) -> Guard:
    cur = _GUARDS.get(slug)
    if cur is None:
        cur = Guard(slug)
        _GUARDS[slug] = cur
    return cur


def maybe_heartbeat(slug: str) -> None:
    """Progress mark: timestamp and supplier slug only."""
    global _LAST_HEARTBEAT
    now = time.time()
    if now - _LAST_HEARTBEAT < 60:
        return
    _LAST_HEARTBEAT = now
    ts = int(now)
    print(f"SG_HEARTBEAT t={ts} supplier={slug}", flush=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and sha and token):
        return
    body = json.dumps({
        "state": "success",
        "context": f"sg-heartbeat/{slug}",
        "description": f"t={ts}",
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/statuses/{sha}",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "sg-heartbeat",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(64)
    except Exception:
        return


def install_from_env() -> bool:
    """Wrap requests.Session.send. Reads env on each request."""
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import requests
        from catalog_licensing import CatalogLicensingError, is_wurth_sk_shop
    except Exception:
        return False

    orig = requests.Session.send

    def _send(self, request, **kwargs):
        url = getattr(request, "url", "") or ""
        host = (urlparse(url).hostname or "").lower()
        if is_wurth_sk_shop(host) or is_wurth_sk_shop(url):
            raise CatalogLicensingError("request refused")
        slug = (os.environ.get("SG_GUARD_SLUG") or "").strip()
        hosts = polite_hosts()
        use = bool(slug) and host_matches(host, hosts)
        if use:
            guard(slug).before_request()
        resp = orig(self, request, **kwargs)
        if use:
            snippet = ""
            try:
                if resp.status_code in (200, 401, 403, 429, 503):
                    snippet = resp.text[:8000] if resp.text else ""
            except Exception:
                snippet = ""
            guard(slug).after_response(resp.status_code, snippet)
        return resp

    requests.Session.send = _send  # type: ignore[method-assign]
    _INSTALLED = True
    return True


def reset_for_tests() -> None:
    """Test helper."""
    global _INSTALLED, _LAST_HEARTBEAT
    _INSTALLED = False
    _GUARDS.clear()
    _LAST_HEARTBEAT = 0.0


def restrict_price_stock_payload(payload: dict, product_id: int) -> dict:
    """Keep price, stock, and availability fields."""
    out: dict[str, Any] = {"id": int(product_id)}
    for key in PRICE_STOCK_FIELDS:
        if key == "meta_data" or key not in payload:
            continue
        out[key] = payload[key]
    meta = []
    for row in payload.get("meta_data") or []:
        if not isinstance(row, dict):
            continue
        k = str(row.get("key") or "")
        if k in PRICE_STOCK_META:
            meta.append({"key": k, "value": row.get("value")})
    if meta:
        out["meta_data"] = meta
    return out
