#!/usr/bin/env python3
"""Import jedného dodávateľa (spúšťa HQ cez .github/workflows/supplier-import.yml).

  python bot/supplier_import.py --supplier lkq --mode full|prices --limit 0

full   = nové produkty + obsah z verejných/bežných ciest bota; opakuje dávky
         (heijunka) kým sú nové produkty alebo kým neminie časový rozpočet.
prices = len cena + sklad existujúcich produktov dodávateľa.
Login vždy z HQ. Lockout guard: jeden pokus o login; pri zlyhaní koniec behu.
Na konci riadok `SG_RESULT {json}` – HQ ho číta a ukazuje stav po slovensky.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
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
}
ENV_PREFIX = {"lkq": "LKQ", "wurth": "WURTH", "szekop": "SZEKOP", "pro-tec": "PROTEC",
              "jlm": "JLM", "metabond": "METABOND", "ptak-mopar": "GENERIC"}
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


def wc_max_id() -> int:
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    try:
        r = requests.get(f"{url}/wp-json/wc/v3/products", params={"per_page": 1, "status": "any", "orderby": "id", "order": "desc"},
                         auth=(os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", "")), timeout=60)
        return int(r.json()[0]["id"]) if r.ok and r.json() else 0
    except Exception:
        return 0


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
            try:
                ok = sup.ensure_login()
            except Exception as exc:
                ok, why = False, type(exc).__name__
            if not ok:
                # Würth: eshop často vráti anti-bot Challenge Validation namiesto login formu.
                # Credentials v HQ sú OK — pokračuj public katalógom do konceptov (B2B ceny neskôr).
                if slug == "wurth":
                    print("[import] wurth: login form/challenge zlyhal — pokračujem public katalógom do konceptov", flush=True)
                    os.environ.pop("WURTH_B2B", None)
                    login_used = False
                else:
                    Path("/tmp/sg_lkq_login_failed" if slug == "lkq" else f"/tmp/sg_{slug}_login_failed").write_text("x")
                    return result(False, "Nesprávne meno alebo heslo – skontroluj prihlásenie v HQ. "
                                         "Ďalší pokus som nerobil, aby sa účet nezablokoval.", login=True)
            else:
                print(f"[import] {slug}: prihlásenie OK", flush=True)

    max_id_before = wc_max_id()
    if a.mode == "full" and not max_id_before:
        return result(False, "Nepodarilo sa zistiť existujúce produkty vo WooCommerce – import som nespustil, aby som nesiahol na existujúce produkty.")
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

    created = len(new_ids(max_id_before))
    counts = {"new": created, "updated": 0,
              "minutes": round((time.time() - t0) / 60, 1), "rounds": rounds, "mode": a.mode, "login": login_used, **pub}
    if slug == "lkq" and os.path.exists("/tmp/sg_lkq_login_failed"):
        return result(False, "Prihlásenie do LKQ počas behu zlyhalo – zvyšné úlohy som zastavil.", **counts)
    if rc == 1 and shard:
        rc = 0
    if rc != 0:
        bad = [l for l in out.splitlines() if l.strip() and ("ERROR" in l or "Error" in l or "Traceback" in l or "zlyhal" in l)]
        why = (bad[-1] if bad else (out.strip().splitlines() or ["neznáma chyba"])[-1])[-250:]
        if rc == 124:
            why = "vypršal čas behu (časť sa stihla)"
        return result(False, f"Import skončil chybou: {why}", **counts)
    return result(True, f"Hotovo: {counts['new'] or 0} nových produktov" + (f", zverejnených {pub['published']}, na kontrolu {pub['held']}." if pub else "."), **counts)


if __name__ == "__main__":
    sys.exit(main())
