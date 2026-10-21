"""Ochrana HostCreators: ak shop odpovedá pomaly / 429 / 5xx, bot počká.

wait_if_slow() meria TTFB homepage najviac raz za SG_GUARD_EVERY_S (30 s).
> SG_GUARD_SLOW_S (3 s) alebo chyba → pauza 30/60/120/240 s, kým sa shop
nespamätá. Takto môže bežať veľa paralelných botov bez spomalenia shopu.
"""
from __future__ import annotations
import logging, os, time

LOGGER = logging.getLogger("shop_guard")
_last = 0.0


def _probe() -> float:
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/") + "/"
    t = time.monotonic()
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "SmartGarazBot-guard"}, stream=True)
        r.close()
        if r.status_code in (429, 500, 502, 503, 504, 508):
            return 99.0
    except Exception:
        return 99.0
    return time.monotonic() - t


def wait_if_slow() -> None:
    global _last
    every = float(os.environ.get("SG_GUARD_EVERY_S", "30"))
    slow = float(os.environ.get("SG_GUARD_SLOW_S", "3"))
    if not os.environ.get("WC_URL") or time.monotonic() - _last < every:
        return
    _last = time.monotonic()
    for pause in (30, 60, 120, 240, 240, 240):
        dt = _probe()
        if dt <= slow:
            return
        LOGGER.warning("shop_guard: shop pomalý (%.1fs) — pauza %ss", dt, pause)
        time.sleep(pause)
