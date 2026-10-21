"""Ochrana hostingu (HostCreators): chránime HQ + WC API, nie rýchlosť shopu.

wait_if_slow() meria najviac raz za SG_GUARD_EVERY_S (30 s):
  - HQ (SG_GUARD_HQ_URL, default https://hq.smartgaraz.sk/) — TTFB > SG_GUARD_SLOW_S (3 s) = pomalé
  - WC API (products?per_page=1) — 429 / 5xx / 508 (limit zdrojov) = problém
Pri 508 (Resource Limit Is Reached) hneď dlhá pauza, aby účet nesuspendovali.
Inak pauza 30/60/120/240 s, kým sa HQ aj API nespamätajú.
"""
from __future__ import annotations
import logging, os, time

LOGGER = logging.getLogger("shop_guard")
_last = 0.0
BAD = (429, 500, 502, 503, 504, 508)


def _probe() -> tuple:
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
    api_code = 200
    wc = os.environ.get("WC_URL", "").rstrip("/")
    if wc:
        try:
            r = requests.get(f"{wc}/wp-json/wc/v3/products", timeout=30,
                             params={"per_page": 1, "_fields": "id"},
                             auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")))
            api_code = r.status_code
        except Exception:
            api_code = 599
    return hq_dt, hq_code, api_code


def wait_if_slow() -> None:
    global _last
    every = float(os.environ.get("SG_GUARD_EVERY_S", "30"))
    slow = float(os.environ.get("SG_GUARD_SLOW_S", "3"))
    if time.monotonic() - _last < every:
        return
    _last = time.monotonic()
    for pause in (30, 60, 120, 240, 240, 240, 240):
        dt, hq_code, api_code = _probe()
        if 508 in (hq_code, api_code):
            LOGGER.error("shop_guard: hosting hlási limit zdrojov (508) — pauza 600s")
            time.sleep(600)
            continue
        if dt <= slow and hq_code not in BAD and api_code not in BAD:
            return
        LOGGER.warning("shop_guard: HQ %.1fs (%s), WC API %s — pauza %ss", dt, hq_code, api_code, pause)
        time.sleep(pause)
