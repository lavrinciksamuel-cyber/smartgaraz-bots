"""Ochrana hostingu (HostCreators): chránime HQ + WC API, nie rýchlosť shopu.

wait_if_slow() meria najviac raz za SG_GUARD_EVERY_S (30 s):
  - HQ (SG_GUARD_HQ_URL, default https://hq.smartgaraz.sk/) — TTFB > SG_GUARD_SLOW_S (3 s) = pomalé
  - WC API (products?per_page=1) — 429 / 5xx / 508 (limit zdrojov) = problém
Pri 508 (Resource Limit Is Reached) hneď dlhá pauza, aby účet nesuspendovali.
Inak pauza 30/60/120/240 s, kým sa HQ aj API nespamätajú.
"""
from __future__ import annotations
import http.client, logging, os, threading, time

# WooCommerce Tracks (Jetpack) pridá pri každom zverejnenom produkte Set-Cookie tk_ai,
# takže batch 100 produktov vráti >200 hlavičiek a http.client spadne na
# "got more than 100 headers" (zápis prebehol, odpoveď sa však stratila).
http.client._MAXHEADERS = max(getattr(http.client, "_MAXHEADERS", 100), 2000)

LOGGER = logging.getLogger("shop_guard")
_last = 0.0
BAD = (429, 500, 502, 503, 504, 508)


def _probe() -> tuple:
    """(hq_dt, hq_code, api_code, api_dt)."""
    import requests
    hq = os.environ.get("SG_GUARD_HQ_URL", "https://hq.smartgaraz.sk/")
    t = time.monotonic()
    hq_code = 0
    try:
        r = requests.get(hq, timeout=20, headers={"User-Agent": "SmartGarazBot-guard"}, stream=True)
        hq_code = r.status_code
        r.close()
    except Exception:
        hq_code = 599
    hq_dt = time.monotonic() - t
    api_code, api_dt = 200, 0.0
    wc = os.environ.get("WC_URL", "").rstrip("/")
    if wc:
        t = time.monotonic()
        try:
            r = requests.get(f"{wc}/wp-json/wc/v3/products", timeout=30,
                             params={"per_page": 1, "_fields": "id"},
                             auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")))
            api_code = r.status_code
        except Exception:
            api_code = 599
        api_dt = time.monotonic() - t
    return hq_dt, hq_code, api_code, api_dt


def _is_day() -> bool:
    """07:00–23:00 Europe/Bratislava (SG_GUARD_DAY=0 vypne, =1 vynúti)."""
    flag = os.environ.get("SG_GUARD_DAY", "").strip()
    if flag in ("0", "1"):
        return flag == "1"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return 7 <= datetime.now(ZoneInfo("Europe/Bratislava")).hour < 23


_lock = threading.Lock()
_extra = 0.0  # adaptívna pauza medzi zápismi (s) — rastie pri pomalom shope, klesá pri zdravom


def wait_if_slow() -> None:
    """HQ = tvrdá brzda (TTFB > 3 s / 5xx). WC API > SG_GUARD_API_SLOW_S (8 s) / 5xx = backoff.
    Lock: kým jeden worker čaká, ostatné workery jobu stoja tiež (adaptívna súbežnosť)."""
    global _last, _extra
    every = float(os.environ.get("SG_GUARD_EVERY_S", "30"))
    slow = float(os.environ.get("SG_GUARD_SLOW_S", "3"))
    api_slow = float(os.environ.get("SG_GUARD_API_SLOW_S", "8"))
    min_gap = 0.0
    if _is_day():
        # Cez deň (07–23 SK) majú prednosť zákazníci: prísnejšie prahy + min. pauza medzi zápismi.
        every = min(every, float(os.environ.get("SG_GUARD_DAY_EVERY_S", "15")))
        slow = min(slow, float(os.environ.get("SG_GUARD_DAY_SLOW_S", "1.5")))
        api_slow = min(api_slow, float(os.environ.get("SG_GUARD_DAY_API_SLOW_S", "4")))
        min_gap = float(os.environ.get("SG_GUARD_DAY_MIN_GAP_S", "3"))
    with _lock:
        if time.monotonic() - _last >= every:
            _last = time.monotonic()
            for pause in (30, 60, 120, 240, 240, 240, 240):
                dt, hq_code, api_code, api_dt = _probe()
                if 508 in (hq_code, api_code):
                    LOGGER.error("shop_guard: hosting hlási limit zdrojov (508) — pauza 600s")
                    time.sleep(600)
                    continue
                hq_ok = dt <= slow and hq_code not in BAD
                api_ok = api_code not in BAD and api_dt <= api_slow
                if hq_ok and api_ok:
                    # pomalší, ale ešte OK shop → mierne pribrzdi; zdravý → povoľ
                    _extra = min(20.0, _extra + 2.0) if api_dt > api_slow / 2 else max(0.0, _extra - 2.0)
                    break
                _extra = min(20.0, _extra + 5.0)
                LOGGER.warning("shop_guard: HQ %.1fs (%s), WC API %.1fs (%s) — pauza %ss, krok +%.0fs",
                               dt, hq_code, api_dt, api_code, pause, _extra)
                time.sleep(pause)
        extra = max(_extra, min_gap)
    if extra > 0:
        time.sleep(extra)
