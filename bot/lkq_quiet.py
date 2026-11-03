#!/usr/bin/env python3
"""Scheduled LKQ run: stock update, then an optional small catalog step."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from b2b_refresh import finish as finish_refresh, refresh  # noqa: E402
from supplier_pace import decide_outcome, job_summary, primary_account  # noqa: E402


def _found_count(text: str) -> int:
    total = 0
    marker = "Nájdených produktov:"
    for line in text.splitlines():
        idx = line.find(marker)
        if idx < 0:
            continue
        tail = line[idx + len(marker):].strip()
        digits = []
        for ch in tail:
            if ch.isdigit():
                digits.append(ch)
            elif digits:
                break
        if digits:
            total += int("".join(digits))
    return total


def discover_new(jobs: int) -> dict:
    """Optional catalog step. Returns counts."""
    if jobs <= 0:
        return {"ran": False, "rc": 0, "seen": 0, "exhausted": False, "blocked": False}
    env = dict(os.environ)
    env["SG_LKQ_KEEP_SESSION"] = "1"
    env["SG_LKQ_ONE_LOGIN"] = "1"
    env["SG_LKQ_MAX_JOBS"] = str(jobs)
    env.setdefault("SG_LKQ_MAX_PAGES", "2")
    limit = os.environ.get("SG_LKQ_NEW_LIMIT", "8")
    cmd = [
        sys.executable, "-u", str(ROOT / "heijunka_vehicle_parts.py"),
        "--jobs", str(jobs), "--limit", str(limit),
        "--sleep", "1", "--force-daytime", "--no-publish",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT.parent), env=env, text=True, capture_output=True)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Výstup heijunky patrí do logu (ceny rediguje sitecustomize v dcérskom procese).
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    low = text.lower()
    blocked = "supplier stop" in low or "kind=blocked" in low
    return {
        "ran": True,
        "rc": int(proc.returncode or 0),
        "seen": _found_count(text),
        "exhausted": "fronta prejdená" in text,
        "blocked": blocked or proc.returncode not in (0, None) and "login zlyhal" in low,
        "login_failed": "login zlyhal" in low or "session neplatná" in low,
    }


def main() -> int:
    if not primary_account():
        job_summary("LKQ", status="ok", new=0, checked=0, updated=0)
        print('SG_RESULT {"ok": true, "status": "ok", "new": 0, "checked": 0, "updated": 0}', flush=True)
        return 0
    limit = int(os.environ.get("SG_LKQ_REFRESH_LIMIT", "12") or 12)
    out = refresh("lkq", max(1, limit), clear_markers=True)
    discover = {"ran": False, "rc": 0, "seen": 0, "exhausted": False, "blocked": False, "login_failed": False}
    new_jobs = int(os.environ.get("SG_LKQ_NEW_JOBS", "1") or 0)
    if new_jobs > 0 and not out.get("paused") and not out.get("blocked") and out.get("login_ok") is not False and not out.get("cap"):
        if out.get("login_ok") is None:
            for stale in (
                os.environ.get("SG_LKQ_ATTEMPT_FILE", "/tmp/sg_lkq_login_attempted"),
                os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed"),
            ):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        discover = discover_new(new_jobs)
    login_ok = out.get("login_ok")
    if discover.get("login_failed"):
        login_ok = False
    blocked = bool(out.get("blocked") or discover.get("blocked"))
    seen = int(discover.get("seen") or 0)
    exhausted = bool(out.get("exhausted")) and not discover.get("ran")
    if discover.get("exhausted") and seen == 0 and int(out.get("checked") or 0) == 0:
        exhausted = True
    ok, why = decide_outcome(
        login_ok=login_ok,
        seen=seen,
        updated=int(out.get("updated") or 0),
        checked=int(out.get("checked") or 0),
        blocked=blocked,
        paused=bool(out.get("paused")),
        exhausted=exhausted,
        cap=bool(out.get("cap")),
    )
    if out.get("reason") == "shop":
        ok, why = False, "shop"
    if discover.get("ran") and discover.get("rc") not in (0, None) and not ok:
        why = why or "discover"
    # finish_refresh zapíše poistku (úspech zmaže sériu, pád ju zvýši).
    merged = dict(out)
    merged["blocked"] = blocked
    merged["login_ok"] = login_ok
    merged["seen"] = seen
    merged["exhausted"] = exhausted and seen == 0 and int(out.get("checked") or 0) == 0
    code = finish_refresh("lkq", merged)
    job_summary(
        "LKQ",
        status="ok" if ok else "failed",
        new=seen,
        checked=int(out.get("checked") or 0),
        updated=int(out.get("updated") or 0),
    )
    if not ok:
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
