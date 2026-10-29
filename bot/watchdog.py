#!/usr/bin/env python3
"""Bot watchdog – keeps the GitHub bots running without a human (stdlib only, cheap).

Every ~3 h (.github/workflows/watchdog.yml):
  1. Stuck runs: in_progress longer than its limit → cancel (next time force-cancel);
     queued/pending/waiting > 12 h → cancel.
  2. Health of key scheduled workflows: unhealthy = no successful run within the
     expected window (schedule stopped/delayed/always failing) or the last N
     finished runs failed. Superseded/timeout "cancelled" runs only count via the window.
  3. Self-heal: an unhealthy workflow is re-dispatched ONCE per window (heavy jobs only
     at night 23–07 Bratislava, so the daytime shop throttle stays intact).
  4. Alert: still unhealthy after that one automatic retry → POST HQ /api/sync/bot-alert
     (push to Samuel's phone + HQ audit log). HQ dedupes 1 alert / bot / day.
     A supplier block (Würth 403) finishes as success/"skipped" → never alerts.
  5. Keepalive: once a day re-enables every scheduled workflow (resets GitHub's 60-day
     inactivity auto-disable for public repos) and re-enables any disabled_inactivity one.
  6. Watches the other repo's watchdog (public read API) so a dead watchdog is noticed.
Env: GITHUB_TOKEN (actions: write), GITHUB_REPOSITORY, HQ_SYNC_TOKEN, HQ_URL, SG_PEER_REPO,
     SG_WATCHDOG_DRY=1 (only report).  Logs: workflow names + states only, no secrets/prices.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bratislava")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=1))

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DRY = os.environ.get("SG_WATCHDOG_DRY", "").strip() in {"1", "true", "yes"}
HQ_URL = os.environ.get("HQ_URL", "https://hq.smartgaraz.sk").rstrip("/")
NOW = datetime.now(timezone.utc)
BOT_ACTOR = "github-actions[bot]"

# file: window_h = max hours without success, streak = failed finished runs in a row that count,
# redispatch = may re-run once, heavy = writes a lot to the shop (re-run only at night),
# inputs = dispatch inputs (so a re-run does the real job, not a preview), name = for Samuel.
H = 1
D = 24
CFG = {
    "catalog-matrix.yml":       dict(window_h=14, streak=4, redispatch=True, heavy=True, name="Import katalógu (LKQ/Szekop/Würth)"),
    "supplier-schedule.yml":    dict(window_h=20, streak=3, redispatch=True, heavy=True, name="ideo / Pro-Tec / TSS import"),
    "promote-drafts.yml":       dict(window_h=14, streak=3, redispatch=True, heavy=False, name="Zverejňovanie konceptov"),
    "cheap-ops.yml":            dict(window_h=30, streak=2, redispatch=True, heavy=False, name="Denná údržba"),
    "availability-backfill.yml": dict(window_h=36, streak=1, redispatch=True, heavy=True, name="Dostupnosť produktov"),
    "protec-price-sync.yml":    dict(window_h=36, streak=1, redispatch=True, heavy=True, name="Pro-Tec ceny a sklad"),
    "heijunka-parts-nightly.yml": dict(window_h=36, streak=2, redispatch=True, heavy=True, name="Nočný import dielov LKQ"),
    "repair-wurth.yml":         dict(window_h=36, streak=2, redispatch=True, heavy=False, name="Oprava Würth produktov"),
    "repair-ideo.yml":          dict(window_h=36, streak=2, redispatch=True, heavy=False, name="Oprava ideo produktov"),
    "repair-tss.yml":           dict(window_h=36, streak=2, redispatch=True, heavy=False, name="Oprava TSS produktov"),
    "refresh-clicked.yml":      dict(window_h=20, streak=3, redispatch=True, heavy=False, name="Obnova klikaných produktov"),
    "daily-site-eval.yml":      dict(window_h=36, streak=2, redispatch=True, heavy=False, name="Denná kontrola webu"),
    "security-monitor.yml":     dict(window_h=3, streak=4, redispatch=False, heavy=False, name="Bezpečnostný monitor"),
    "wurth-catalog-sync.yml":   dict(window_h=8 * D + 8, streak=1, redispatch=True, heavy=True, name="Würth týždenný katalóg"),
    "wurth-photo-backfill.yml": dict(window_h=8 * D + 8, streak=1, redispatch=True, heavy=True, inputs={"apply": "true"}, name="Würth fotky"),
    "reprice-min-profit.yml":   dict(window_h=8 * D + 8, streak=1, redispatch=True, heavy=True, inputs={"args": "--apply"}, name="Kontrola minimálnej marže"),
}
STUCK_RUN_H = 6.5      # longest job timeout is 360 min
STUCK_QUEUE_H = 12
BAD = {"failure", "timed_out", "startup_failure"}


def log(msg: str) -> None:
    print(msg, flush=True)


def gh(method: str, path: str, body=None, token: str | None = TOKEN, ok_codes=(200, 201, 202, 204)):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sg-watchdog"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    if data is not None:
        hdr["Content-Type"] = "application/json"
    for i in range(4):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdr)
            with urllib.request.urlopen(req, timeout=40) as f:
                raw = f.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in ok_codes:
                return {}
            if e.code >= 500 or e.code == 429:
                time.sleep(5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GitHub API {method} {path.split('?')[0]} nedostupné")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def sk(d) -> str:
    return d.astimezone(TZ).strftime("%d.%m. %H:%M") if d else "nikdy"


def night() -> bool:
    h = NOW.astimezone(TZ).hour
    return h >= 23 or h < 7


def alert(wf: str, title: str, body: str, url: str = "", level: str = "error") -> None:
    log(f"::warning::ALERT {wf}: {title} – {body}")
    tok = os.environ.get("HQ_SYNC_TOKEN", "")
    if DRY or not tok:
        log("  (alert neodoslaný: dry-run alebo chýba HQ_SYNC_TOKEN)")
        return
    payload = json.dumps({"workflow": wf, "repo": REPO, "title": title, "body": body, "url": url, "level": level}).encode()
    for i in range(3):
        try:
            req = urllib.request.Request(HQ_URL + "/api/sync/bot-alert", data=payload, method="POST",
                                         headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                                                  "User-Agent": "sg-watchdog"})
            with urllib.request.urlopen(req, timeout=40) as f:
                log(f"  HQ alert: {f.read()[:120].decode(errors='ignore')}")
                return
        except Exception as e:  # HQ down → try next watchdog run
            log(f"  HQ alert pokus {i + 1} zlyhal: {type(e).__name__}")
            time.sleep(10 * (i + 1))


def runs_of(wf_id, n=40):
    return gh("GET", f"/repos/{REPO}/actions/workflows/{wf_id}/runs?per_page={n}&branch=main").get("workflow_runs", [])


def stuck_runs() -> None:
    for status in ("in_progress", "queued", "pending", "waiting"):
        try:
            rs = gh("GET", f"/repos/{REPO}/actions/runs?status={status}&per_page=100").get("workflow_runs", [])
        except Exception as e:
            log(f"stuck check {status}: {e}")
            continue
        for r in rs:
            if r.get("name") == "Bot watchdog":
                continue
            start = ts(r.get("run_started_at") or r.get("created_at"))
            age_h = (NOW - start).total_seconds() / 3600
            limit = STUCK_RUN_H if status == "in_progress" else STUCK_QUEUE_H
            if age_h <= limit:
                continue
            force = status == "in_progress" and age_h > limit + 2.5  # normal cancel didn't help last time
            log(f"STUCK {r['path'].split('/')[-1]} run {r['id']} {status} {age_h:.1f} h → {'force-cancel' if force else 'cancel'}")
            if not DRY:
                try:
                    gh("POST", f"/repos/{REPO}/actions/runs/{r['id']}/{'force-cancel' if force else 'cancel'}", ok_codes=(202, 409))
                except Exception as e:
                    log(f"  cancel zlyhal: {e}")


def check(wf: dict, cfg: dict) -> None:
    f = wf["path"].split("/")[-1]
    name = cfg.get("name", f)
    win = timedelta(hours=cfg["window_h"])
    runs = runs_of(wf["id"])
    active = [r for r in runs if r["status"] != "completed"]
    done = [r for r in runs if r["status"] == "completed"]
    last_ok = next((ts(r["created_at"]) for r in done if r["conclusion"] == "success"), None)
    finished = [r for r in done if r["conclusion"] not in ("cancelled", "skipped", "neutral")]
    streak = 0
    for r in finished:
        if r["conclusion"] in BAD:
            streak += 1
        else:
            break
    in_win = [r for r in runs if ts(r["created_at"]) > NOW - win]
    ever_sched = any(r["event"] == "schedule" for r in runs)
    stale = (last_ok is None or NOW - last_ok > win) and (bool(in_win) or ever_sched)
    failing = streak >= cfg["streak"]
    state = "OK" if not (stale or failing) else ("STALE" if stale else f"FAIL×{streak}")
    log(f"{f:30} {state:8} posledný úspech {sk(last_ok)} · beží {len(active)}")
    if state == "OK":
        return
    if active:
        log("  → beh práve beží/čaká, počkám")
        return
    last_bad = next((r for r in finished if r["conclusion"] in BAD), done[0] if done else None)
    # Watchdog smie reštartovať raz za okno; jeho reštart = workflow_dispatch od github-actions[bot].
    mine = [r for r in done if r["event"] == "workflow_dispatch" and ts(r["created_at"]) > NOW - win
            and (r.get("triggering_actor") or {}).get("login") == BOT_ACTOR]
    url = (last_bad or {}).get("html_url", "")
    if cfg.get("redispatch") and not mine:
        if cfg.get("heavy") and not night():
            log("  → ťažký bot: automatický reštart až v noci (23–07), cez deň šetríme web")
            return
        log("  → spúšťam 1× znova (watchdog)")
        if not DRY:
            try:
                gh("POST", f"/repos/{REPO}/actions/workflows/{wf['id']}/dispatches",
                   {"ref": "main", "inputs": cfg.get("inputs", {})})
                return
            except Exception as e:
                log(f"  reštart zlyhal: {e}")
        else:
            return
    why = ("posledný úspešný beh " + sk(last_ok)) if stale else f"{streak}× po sebe zlyhal"
    tail = " · automatický reštart nepomohol" if mine else ""
    alert(f, f"Bot {name} zlyhal", f"{why}{tail}. Repo {REPO.split('/')[-1]}.", url)


def keepalive(wfs) -> None:
    if NOW.hour >= 3 and wfs and not any(w["state"] == "disabled_inactivity" for w in wfs):
        return  # once a day (first run after 00:00 UTC) + whenever something got auto-disabled
    for w in wfs:
        if w["state"] in ("active", "disabled_inactivity"):
            if w["state"] == "disabled_inactivity":
                log(f"KEEPALIVE: {w['path']} bol vypnutý pre neaktivitu → zapínam")
            if not DRY:
                try:
                    gh("PUT", f"/repos/{REPO}/actions/workflows/{w['id']}/enable", ok_codes=(204,))
                except Exception as e:
                    log(f"  enable {w['path']}: {e}")
    log("keepalive: workflowy obnovené (60-dňová neaktivita)")


def peer() -> None:
    p = os.environ.get("SG_PEER_REPO", "").strip()
    if not p:
        return
    try:
        rs = gh("GET", f"/repos/{p}/actions/workflows/watchdog.yml/runs?per_page=5", token=None).get("workflow_runs", [])
    except Exception as e:
        log(f"peer {p}: {e}")
        return
    last = next((ts(r["created_at"]) for r in rs if r["conclusion"] == "success"), None)
    log(f"peer watchdog {p}: posledný úspech {sk(last)}")
    ref = last or (ts(rs[-1]["created_at"]) if rs else None)  # nový peer: počítaj od jeho prvého behu
    if ref and NOW - ref > timedelta(hours=14):
        alert("watchdog.yml", "Strážca botov v druhom repozitári nebeží",
              f"{p.split('/')[-1]}: posledný úspešný beh {sk(last)}.", f"https://github.com/{p}/actions")


def tss_fresh() -> None:
    """TSS: cenník + sklad sťahuje hosting cron do HQ; bot z neho importuje. Starý súbor = cron stojí."""
    if os.environ.get("SG_CHECK_TSS", "") != "1" or not os.environ.get("HQ_SYNC_TOKEN"):
        return
    import gzip
    try:
        req = urllib.request.Request(HQ_URL + "/api/sync/tss-catalog", headers={
            "Authorization": "Bearer " + os.environ["HQ_SYNC_TOKEN"], "Accept-Encoding": "gzip", "User-Agent": "sg-watchdog"})
        with urllib.request.urlopen(req, timeout=60) as f:
            raw = f.read()
        data = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        gen = ts(str(data.get("generated_at") or ""))
    except Exception as e:
        log(f"TSS katalóg v HQ: nedá sa prečítať ({type(e).__name__}) – skúsim neskôr")
        return
    log(f"TSS katalóg v HQ: {sk(gen)} · {data.get('count')} položiek")
    if gen is None or NOW - gen > timedelta(hours=36):
        alert("tss-hq-cron", "TSS cenník sa neaktualizuje",
              f"Posledné stiahnutie TSS v HQ: {sk(gen)}. Hosting cron asi stojí.")


def main() -> int:
    if not REPO or not TOKEN:
        log("chýba GITHUB_REPOSITORY/GITHUB_TOKEN")
        return 1
    log(f"watchdog {REPO} {sk(NOW)} (SK){' DRY' if DRY else ''}")
    try:
        wfs = gh("GET", f"/repos/{REPO}/actions/workflows?per_page=100").get("workflows", [])
    except Exception as e:
        log(f"::warning::GitHub API nedostupné ({e}) – skúsim pri ďalšom behu")
        return 0
    stuck_runs()
    for w in wfs:
        f = w["path"].split("/")[-1]
        if f in CFG and w["state"] == "active":
            try:
                check(w, CFG[f])
            except Exception as e:
                log(f"{f}: kontrola zlyhala ({type(e).__name__}: {e})")
    keepalive(wfs)
    tss_fresh()
    peer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
