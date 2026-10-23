#!/usr/bin/env python3
"""Import jedného dodávateľa (spúšťa HQ cez .github/workflows/supplier-import.yml).

  python bot/supplier_import.py --supplier lkq --mode full|prices --limit 0

full   = nové produkty + obsah z verejných/bežných ciest bota; opakuje dávky
         (heijunka) kým sú nové produkty alebo kým neminie časový rozpočet.
prices = len cena + sklad existujúcich produktov dodávateľa.
Login vždy z HQ. Lockout guard: shardy sa prihlasujú s rozostupom; pri odmietnutí NAJVIAC
jeden ďalší pokus po náhodných 2–5 min, potom koniec behu (nikdy viac — lockout).
Na konci riadok `SG_RESULT {json}` – HQ ho číta a ukazuje stav po slovensky.
"""
from __future__ import annotations
import argparse, json, os, random, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# HQ slug -> (get_supplier kód, host pre ceny/sklad, login povinný)
SUPPLIERS = {
    "lkq": ("lkq", "lkq.sk", True),
    "wurth": ("wurth", "wurth", False),
    "szekop": ("szekop", "nextis", False),
    "pro-tec": ("pro-tec", "pro-tec", False),
    "jlm": ("jlm", "jlm", False),
    "metabond": ("metabond", "metabond", False),
    "ptak-mopar": ("generic", "", False),
    "ideo": ("ideo", "ideo.sk", True),
}
ENV_PREFIX = {"lkq": "LKQ", "wurth": "WURTH", "szekop": "SZEKOP", "pro-tec": "PROTEC",
              "jlm": "JLM", "metabond": "METABOND", "ptak-mopar": "GENERIC", "ideo": "IDEO"}
BUDGET_S = int(os.environ.get("SG_IMPORT_BUDGET_S", str(int(4.5 * 3600))))


def result(ok: bool, msg: str, **kw) -> int:
    print("SG_RESULT " + json.dumps({"ok": ok, "msg": msg, **kw}, ensure_ascii=False), flush=True)
    return 0 if ok else 1


def wc_count(params: dict):
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    if not url:
        return None
    try:
        r = requests.get(f"{url}/wp-json/wc/v3/products", params={"per_page": 1, "status": "any", **params},
                         auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")), timeout=60)
        return int(r.headers.get("X-WP-Total", "0")) if r.ok else None
    except Exception:
        return None


T_START = time.time()


def run(cmd, env, timeout=None) -> tuple[int, str]:
    """Spusti krok a priebežne streamuj log (vidno v Actions hneď, nie až na konci)."""
    print("[import] " + " ".join(cmd[1:]), flush=True)
    left = max(120, int(timeout if timeout else BUDGET_S - (time.time() - T_START)))
    end = time.time() + left
    p = subprocess.Popen(cmd, cwd=str(ROOT.parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    tail: list = []
    rc = None
    for line in p.stdout:
        sys.stdout.write(line)
        tail.append(line)
        if len(tail) > 4000:
            del tail[:1000]
        if time.time() > end:
            p.terminate()
            rc = 124
            break
    try:
        p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
    sys.stdout.flush()
    return (rc if rc is not None else p.returncode), "".join(tail)


def _wc_max_id_once() -> int:
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    try:
        r = requests.get(f"{url}/wp-json/wc/v3/products", params={"per_page": 1, "status": "any", "orderby": "id", "order": "desc", "_fields": "id"},
                         auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")), timeout=60)
        if not r.ok:
            print(f"[import] WC max id: HTTP {r.status_code}", flush=True)
            return 0
        d = r.json()
        return int(d[0]["id"]) if d else 0
    except Exception as exc:
        print(f"[import] WC max id: {type(exc).__name__}", flush=True)
        return 0


def _hq_max_id_once() -> int:
    """HQ zrkadlo wc_products (/api/sync/existing-skus → max_id). Môže mierne zaostávať — OK ako spodná hranica."""
    import requests
    tok = os.environ.get("HQ_SYNC_TOKEN", "").strip()
    if not tok:
        return 0
    hq = os.environ.get("HQ_URL", "https://hq.smartgaraz.sk").rstrip("/")
    try:
        r = requests.get(f"{hq}/api/sync/existing-skus", timeout=60,
                         headers={"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip", "User-Agent": "smartgaraz-bot"})
        if not r.ok:
            print(f"[import] HQ max id: HTTP {r.status_code}", flush=True)
            return 0
        return int(r.json().get("max_id") or 0)
    except Exception as exc:
        print(f"[import] HQ max id: {type(exc).__name__}", flush=True)
        return 0


def wc_max_id() -> int:
    """Najvyššie ID produktu pred importom. Prechodné výpadky (508/timeout) → opakuj
    s backoffom (~4 min): WC REST, potom HQ zrkadlo. 0 = všetko zlyhalo → bezpečnostný stop."""
    waits = [int(x) for x in os.environ.get("SG_MAXID_WAITS", "0,30,75,120").split(",")]
    for n, wait in enumerate(waits, 1):
        if wait:
            w = wait + random.uniform(0, 15)
            print(f"[import] existujúce produkty: pokus {n}/{len(waits)} o {w:.0f}s", flush=True)
            time.sleep(w)
        mid = _wc_max_id_once()
        if mid:
            return mid
        mid = _hq_max_id_once()
        if mid:
            print("[import] max id z HQ (WC REST neodpovedal)", flush=True)
            return mid
    return 0


def _shard_index() -> int:
    try:
        return int(os.environ.get("SG_SHARD", "").split("/")[0])
    except Exception:
        return -1


def login_stagger(slug: str) -> None:
    """Shardy sa neprihlasujú naraz (dodávateľ môže odmietnuť súbežné loginy)."""
    i = _shard_index()
    if i < 0 or os.environ.get("SG_LOGIN_STAGGER", "1") == "0":
        return
    w = min(120.0, i * 5.0) + random.uniform(0, 8)
    print(f"[import] {slug}: login o {w:.0f}s (rozostup shardov)", flush=True)
    time.sleep(w)


def try_login(sup) -> bool:
    try:
        return bool(sup.ensure_login())
    except Exception as exc:
        print(f"[import] login: {type(exc).__name__}", flush=True)
        return False


def reset_login_guard(slug: str, sup) -> None:
    """Pred JEDINÝM opakovaním: zmaž guard marker z prvého pokusu (inak by ensure_login hneď vrátil False)."""
    for f in {os.environ.get("SG_LKQ_FAIL_MARKER", "/tmp/sg_lkq_login_failed"), f"/tmp/sg_{slug}_login_failed",
              os.environ.get("SG_LKQ_COOKIE_FILE", "/tmp/sg_lkq_session.pkl")}:
        try:
            os.remove(f)
        except Exception:
            pass
    try:
        sup._logged_in = False
        sup.http.session.cookies.clear()
    except Exception:
        pass


def new_ids(max_id_before: int) -> list:
    """ID produktov vytvorených po začiatku behu (id > max_id_before) — presný počet."""
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    ids, page = [], 1
    while page < 200:
        for wait in (0, 10, 30, 60):
            time.sleep(wait)
            try:
                r = requests.get(f"{url}/wp-json/wc/v3/products", params={"per_page": 100, "page": page, "status": "any", "orderby": "id", "order": "desc", "_fields": "id"},
                                 auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")), timeout=90)
                if r.ok:
                    break
            except Exception:
                r = None
        if r is None or not r.ok:
            return ids
        batch = [int(x["id"]) for x in r.json()]
        ids += [i for i in batch if i > max_id_before]
        if not batch or min(batch) <= max_id_before:
            return ids
        page += 1
    return ids


KNOWN_SOURCES = {"lkq", "wurth", "szekop", "pro-tec", "jlm", "metabond", "ideo"}


def new_by_source(max_id_before: int) -> dict:
    """Nové produkty v tomto behu podľa _supplier_source (matrix beží paralelne → celkový
    počet nových ID zahŕňa aj iných dodávateľov)."""
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    out, page = {}, 1
    while page < 200:
        r = None
        for wait in (0, 10, 30):
            time.sleep(wait)
            try:
                r = requests.get(f"{url}/wp-json/wc/v3/products", params={"per_page": 100, "page": page, "status": "any", "orderby": "id", "order": "desc", "_fields": "id,meta_data"},
                                 auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")), timeout=120)
                if r.ok:
                    break
            except Exception:
                r = None
        if r is None or not r.ok:
            return out
        batch = r.json()
        for p in batch:
            if int(p["id"]) <= max_id_before:
                continue
            src = next((str(m.get("value") or "") for m in p.get("meta_data") or [] if m.get("key") == "_supplier_source"), "") or "?"
            out[src] = out.get(src, 0) + 1
        if not batch or min(int(p["id"]) for p in batch) <= max_id_before:
            return out
        page += 1
    return out


def own_new(by_src: dict, code: str) -> int:
    if code == "generic":
        return sum(v for k, v in by_src.items() if k not in KNOWN_SOURCES)
    return int(by_src.get(code, 0))


def wurth_blocked_result(**kw) -> int:
    return result(False, "Würth blokuje automatický prístup (ochrana webu challenge/403) – "
                         "shard som hneď ukončil, nič sa neimportovalo. Neobchádzame to; skúsi sa ďalší beh.",
                  blocked=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "prices"])
    ap.add_argument("--limit", type=int, default=0, help="0 = všetko (v rámci časového rozpočtu)")
    a = ap.parse_args()
    slug = a.supplier.strip().lower()
    if slug not in SUPPLIERS:
        return result(False, f"Neznámy dodávateľ „{slug}“ – bot preň ešte nie je.")
    code, host, needs_login = SUPPLIERS[slug]
    limit = a.limit if a.limit and a.limit > 0 else 0

    from suppliers.hq_credentials import supplier_profile, supplier_login
    prof = supplier_profile(slug)
    user, pwd = supplier_login(slug)
    pre = ENV_PREFIX[slug]
    if slug == "wurth":
        # Pri ochrane webu nečakaj 5 min/pokus — drží to 1 z 20 runner slotov.
        os.environ.setdefault("WURTH_CHALLENGE_PAUSE", "0")
        os.environ.setdefault("SG_WURTH_FAST_FAIL", "1")
    if user and pwd:
        os.environ[f"{pre}_USER"], os.environ[f"{pre}_PASSWORD"] = user, pwd
        if slug == "wurth":
            os.environ["WURTH_B2B"] = "1"
    elif needs_login:
        return result(False, "Chýba meno alebo heslo – doplň prihlásenie v HQ (Dodávatelia).")
    base_url = ""
    if code == "generic":
        base_url = (prof.get("website") or "").strip()
        if not base_url:
            return result(False, "Chýba web dodávateľa – doplň ho v HQ (Dodávatelia → Pták / Mopar).")
    if slug == "lkq" and os.environ.get("SG_LKQ_DISABLED") == "1":
        return result(False, "LKQ je dočasne vypnuté.")

    # Lockout guard: presne JEDEN kontrolný pokus o login; pri zlyhaní koniec.
    login_used = False
    if user and pwd and code != "generic":
        from suppliers import get_supplier
        sup = get_supplier(code)
        if getattr(sup, "_public_only", False):
            print(f"[import] {slug}: verejný režim (bez prihlásenia)", flush=True)
        elif hasattr(sup, "ensure_login"):
            login_used = True
            login_stagger(slug)
            ok = try_login(sup)
            if not ok and slug != "wurth":
                # Súbežné loginy shardov dodávateľ občas odmietne aj pri správnom hesle →
                # NAJVIAC JEDEN ďalší pokus po náhodných 2–5 min (viac nie — lockout).
                w = random.uniform(120, 300)
                print(f"[import] {slug}: login odmietnutý — jediný ďalší pokus o {w:.0f}s", flush=True)
                time.sleep(w)
                reset_login_guard(slug, sup)
                ok = try_login(sup)
            if not ok:
                # Würth: eshop často vráti anti-bot Challenge Validation namiesto login formu.
                # Credentials v HQ sú OK — pokračuj public katalógom do konceptov (B2B ceny neskôr).
                if slug == "wurth":
                    if getattr(sup, "_blocked", False) and sup.probe_blocked():
                        print("[import] wurth: login aj verejný katalóg vracajú challenge/403 — koniec bez čakania", flush=True)
                        return wurth_blocked_result(new=0, login=False)
                    print("[import] wurth: login form/challenge zlyhal — pokračujem public katalógom do konceptov", flush=True)
                    os.environ.pop("WURTH_B2B", None)
                    login_used = False
                else:
                    Path("/tmp/sg_lkq_login_failed" if slug == "lkq" else f"/tmp/sg_{slug}_login_failed").write_text("x")
                    return result(False, "Nesprávne meno alebo heslo – skontroluj prihlásenie v HQ. "
                                         "Skúsil som to 2× (druhý pokus o pár minút); ďalší pokus som nerobil, "
                                         "aby sa účet nezablokoval.", login=True)
            else:
                print(f"[import] {slug}: prihlásenie OK", flush=True)

    max_id_before = wc_max_id()
    if a.mode == "full" and not max_id_before:
        return result(False, "Nepodarilo sa zistiť existujúce produkty vo WooCommerce ani v HQ (4 pokusy za ~4 min) – "
                             "import som nespustil, aby som nesiahol na existujúce produkty. Skúsi sa ďalší beh.", transient=True)
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    env = {**os.environ, "SG_SKIP_EXISTING": "1" if a.mode == "full" else "0", "SG_DAYTIME_QUIET": "force_run", "AI_MODE": os.environ.get("AI_MODE", "cheap")}
    py = sys.executable
    t0 = time.time()
    rc, out, rounds = 0, "", 0
    shard = os.environ.get("SG_SHARD", "").strip()
    if a.mode == "prices":
        if not host:
            return result(False, "Pre tohto dodávateľa ešte nevieme aktualizovať ceny a sklad.")
        env["SG_ONLY_SUPPLIER_HOST"] = host
        rc, out = run([py, str(ROOT / "secret_crawler.py"), "--existing", "--status", "any",
                       "--limit", str(limit), "--sleep", "0.1"], env)
        rounds = 1
    else:
        shard = os.environ.get("SG_SHARD", "").strip()
        if slug in ("szekop", "wurth") and shard:
            # Plný katalóg: paralelné shardy s kurzorom (catalog_shard.py), 1 kolo = celý rozpočet.
            mins = max(10, int((BUDGET_S - (time.time() - t0)) / 60) - 15)
            cmd = [py, str(ROOT / "catalog_shard.py"), "--supplier", slug, "--shard", shard, "--budget-min", str(mins)]
        elif slug == "lkq":
            cmd = [py, str(ROOT / "heijunka_vehicle_parts.py"), "--jobs", os.environ.get("SG_LKQ_JOBS", "4"), "--limit", "50",
                   "--sleep", "0.2", "--force-daytime", "--no-publish"]
            if shard:
                cmd += ["--shard", shard]
        elif slug == "szekop":
            cmd = [py, str(ROOT / "heijunka_szekop_parts.py"), "--jobs", "8", "--limit", "50", "--force-daytime", "--no-publish", "--expand-filters", "--reset-failed"]
        else:
            cmd = [py, str(ROOT / "import_pipeline.py"), "--supplier", code, "--limit", str(limit or 5000),
                   "--cheap", "--force-daytime", "--skip-existing", "--sleep", "0.1"]
            if base_url:
                cmd += ["--base-url", base_url]
        repeat = slug == "lkq" or (slug == "szekop" and not shard)
        idle = 0
        while True:
            rounds += 1
            before = len(new_ids(max_id_before))
            rc, out = run(cmd, env)
            if slug == "lkq" and os.path.exists("/tmp/sg_lkq_login_failed"):
                break
            got = len(new_ids(max_id_before)) - before
            print(f"[import] kolo {rounds}: nových {got}", flush=True)
            # Shard: kolo, kde všetky joby zlyhali (rc=1, napr. LKQ nemá kategóriu), nie je koniec
            if not repeat or (rc != 0 and not (shard and rc == 1)):
                break
            idle = idle + 1 if got == 0 else 0
            # Shard: prvé kolá často len preskakujú už importované joby → nezastavuj
            # sa na „0 nových“; koniec až po prejdení fronty alebo rozpočtu.
            if shard and "fronta prejdená" in out:
                break
            if (idle >= (10**6 if shard else 2)) or (limit and len(new_ids(max_id_before)) >= limit):
                break
            if time.time() - t0 > BUDGET_S - 1200:
                break

    pub = {}
    if a.mode == "full" and not (slug == "lkq" and os.path.exists("/tmp/sg_lkq_login_failed")):
        src = {"pro-tec": "pro-tec", "ptak-mopar": "generic"}.get(slug, slug)
        _, pout = run([py, str(ROOT / "publish_ready_drafts.py"), "--limit", "0", "--supplier", src, "--sleep", "0.1",
                       "--min-id", str(max_id_before + 1)], env)
        import re as _re
        m = _re.search(r"drafts=(\d+) ready=(\d+) reject=(\{.*\})", pout)
        if m:
            try:
                rej = json.loads(m.group(3).replace("'", '"'))
            except Exception:
                rej = {}
            pub = {"published": int(m.group(2)), "held": sum(rej.values()) if rej else 0, "held_reasons": rej}

    by_src = new_by_source(max_id_before) if a.mode == "full" else {}
    created = own_new(by_src, code) if by_src else 0
    counts = {"new": created, "new_all_suppliers": sum(by_src.values()), "new_by_source": by_src, "updated": 0,
              "minutes": round((time.time() - t0) / 60, 1), "rounds": rounds, "mode": a.mode, "login": login_used, **pub}
    if slug == "lkq" and os.path.exists("/tmp/sg_lkq_login_failed"):
        return result(False, "Prihlásenie do LKQ počas behu zlyhalo – zvyšné úlohy som zastavil.", **counts)
    if rc == 1 and shard:
        rc = 0
    if slug == "wurth" and a.mode == "full" and created == 0:
        blocked = ("ochrana webu" in out or "challenge" in out.lower()) and (
            "discover: 0 URL" in out or '"parsed": 0' in out or "crawl končím" in out)
        if blocked:
            return wurth_blocked_result(**counts)
    if rc != 0:
        bad = [l for l in out.splitlines() if l.strip() and ("ERROR" in l or "Error" in l or "Traceback" in l or "zlyhal" in l)]
        why = (bad[-1] if bad else (out.strip().splitlines() or ["neznáma chyba"])[-1])[-250:]
        if rc == 124:
            why = "vypršal čas behu (časť sa stihla)"
        return result(False, f"Import skončil chybou: {why}", **counts)
    return result(True, f"Hotovo ({slug}): {counts['new'] or 0} nových produktov" + (f", zverejnených {pub['published']}, na kontrolu {pub['held']}." if pub else "."), **counts)


if __name__ == "__main__":
    sys.exit(main())
