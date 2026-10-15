#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Denný quiet režim — cez SK pracovný deň šetriť web (žiadny bulk).

Europe/Bratislava 07:00–20:00 = quiet (default).

Quiet ≠ úplná pauza: povolený je **quiet drip** — malé dávky + dlhý sleep
+ homepage smoke gate, aby stránka nemrzla.

  - Veľký heijunka / overnight / Würth multi-leaf = stále pauza
  - Quiet drip: `--quiet-drip` (limit ≤8, sleep ≥2 s, 1 leaf)

Vypnutie quiet: SG_DAYTIME_QUIET=0
Vynútenie quiet: SG_DAYTIME_QUIET=1 (aj v noci)
Vynútenie plného behu: SG_DAYTIME_QUIET=force_run
Vypnutie drip (len publish): SG_QUIET_DRIP=0
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Bratislava")
QUIET_START = 7   # inclusive
QUIET_END = 20    # exclusive

# Heijunka cez deň — pomalé, spoľahlivé (web speed).
QUIET_DRIP_MAX_LIMIT = 8
QUIET_DRIP_MIN_SLEEP = 2.0
QUIET_DRIP_MAX_LEAVES = 1
QUIET_DRIP_PUBLISH_LIMIT = 15
QUIET_DRIP_SMOKE_URL = "https://www.smartgaraz.sk/"


@dataclass(frozen=True)
class QuietDripProfile:
    """Limity pre denný quiet drip import."""

    max_limit: int = QUIET_DRIP_MAX_LIMIT
    min_sleep: float = QUIET_DRIP_MIN_SLEEP
    max_leaves: int = QUIET_DRIP_MAX_LEAVES
    publish_limit: int = QUIET_DRIP_PUBLISH_LIMIT


def is_daytime_quiet(now: datetime | None = None) -> bool:
    flag = (os.getenv("SG_DAYTIME_QUIET") or "1").strip().lower()
    if flag in ("0", "false", "off", "no", "force_run"):
        return False
    if flag in ("force", "force_quiet", "always"):
        return True
    dt = now or datetime.now(TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    else:
        dt = dt.astimezone(TZ)
    return QUIET_START <= dt.hour < QUIET_END


def quiet_drip_enabled() -> bool:
    """Quiet drip zapnutý (default áno). SG_QUIET_DRIP=0 → len publish/smoke."""
    flag = (os.getenv("SG_QUIET_DRIP") or "1").strip().lower()
    return flag not in ("0", "false", "off", "no")


def quiet_drip_profile() -> QuietDripProfile:
    """Profil limít; env môže sprísniť, nie uvoľniť nad stropy."""
    try:
        lim = int(os.getenv("SG_QUIET_DRIP_LIMIT") or QUIET_DRIP_MAX_LIMIT)
    except ValueError:
        lim = QUIET_DRIP_MAX_LIMIT
    try:
        sleep = float(os.getenv("SG_QUIET_DRIP_SLEEP") or QUIET_DRIP_MIN_SLEEP)
    except ValueError:
        sleep = QUIET_DRIP_MIN_SLEEP
    try:
        leaves = int(os.getenv("SG_QUIET_DRIP_LEAVES") or QUIET_DRIP_MAX_LEAVES)
    except ValueError:
        leaves = QUIET_DRIP_MAX_LEAVES
    try:
        pub = int(os.getenv("SG_QUIET_DRIP_PUBLISH") or QUIET_DRIP_PUBLISH_LIMIT)
    except ValueError:
        pub = QUIET_DRIP_PUBLISH_LIMIT
    return QuietDripProfile(
        max_limit=max(1, min(lim, QUIET_DRIP_MAX_LIMIT)),
        min_sleep=max(QUIET_DRIP_MIN_SLEEP, sleep),
        max_leaves=max(1, min(leaves, QUIET_DRIP_MAX_LEAVES)),
        publish_limit=max(1, min(pub, QUIET_DRIP_PUBLISH_LIMIT)),
    )


def clamp_for_quiet_drip(limit: int, sleep: float) -> tuple[int, float]:
    """Vynúť quiet drip stropy na limit/sleep."""
    p = quiet_drip_profile()
    return min(max(1, limit), p.max_limit), max(float(sleep), p.min_sleep)


def homepage_smoke_ok(url: str | None = None, timeout: float = 20.0) -> tuple[bool, int]:
    """Jidoka: homepage musí byť HTTP 200, inak zastav drip."""
    target = url or QUIET_DRIP_SMOKE_URL
    try:
        import requests

        r = requests.get(target, timeout=timeout, allow_redirects=True)
        return r.status_code == 200, int(r.status_code)
    except Exception:
        return False, 0


def quiet_message() -> str:
    return (
        f"DAYTIME_QUIET Europe/Bratislava {QUIET_START:02d}:00–{QUIET_END:02d}:00 "
        "— bulk heijunka/import pauza (web speed). "
        "Povolený quiet drip: --quiet-drip (≤8 ks, sleep≥2s). "
        "Plný beh: SG_DAYTIME_QUIET=force_run / --force-daytime."
    )


if __name__ == "__main__":
    mode = "quiet" if is_daytime_quiet() else "run"
    drip = "drip_on" if quiet_drip_enabled() else "drip_off"
    print(mode, drip, datetime.now(TZ).isoformat())
