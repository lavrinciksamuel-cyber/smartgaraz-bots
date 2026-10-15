"""Supplier logins + bot settings from SmartGaraz HQ, with env-var fallback.

Usage:
    from suppliers.hq_credentials import supplier_login, bot_settings
    user, pwd = supplier_login("lkq")            # HQ first, then LKQ_USER / LKQ_PASSWORD
    cfg = bot_settings("heijunka-parts-nightly.yml")  # {} when HQ unreachable

Env: HQ_SYNC_TOKEN (GitHub secret), HQ_URL (default https://hq.smartgaraz.sk).
Never logs secret values.
"""
from __future__ import annotations
import json, os, sys, urllib.error, urllib.parse, urllib.request

HQ_URL = os.environ.get("HQ_URL", "https://hq.smartgaraz.sk").rstrip("/")
_cache: dict[str, dict] = {}

ENV_NAMES = {
    "protec": ("PROTEC_USER", "PROTEC_PASSWORD"),
    "pro-tec": ("PROTEC_USER", "PROTEC_PASSWORD"),
    "metabond": ("METABOND_USER", "METABOND_PASSWORD"),  # supplier -> (user env, password env); extend as needed
    "lkq": ("LKQ_USER", "LKQ_PASSWORD"),
    "szekop": ("SZEKOP_USER", "SZEKOP_PASSWORD"),
    "wurth": ("WURTH_USER", "WURTH_PASSWORD"),
}

def _get(path: str, params: dict) -> dict | None:
    tok = os.environ.get("HQ_SYNC_TOKEN", "").strip()
    if not tok:
        return None
    url = f"{HQ_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "User-Agent": "smartgaraz-bot"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[hq] {path} {params} -> HTTP {e.code}, using env fallback", file=sys.stderr)
    except Exception as e:  # network etc.
        print(f"[hq] {path} unreachable ({type(e).__name__}), using env fallback", file=sys.stderr)
    return None

def supplier_profile(slug: str) -> dict:
    slug = slug.lower()
    if slug not in _cache:
        _cache[slug] = _get("/api/sync/credentials", {"supplier": slug}) or {}
    return _cache[slug]

def supplier_login(slug: str) -> tuple[str, str]:
    p = supplier_profile(slug)
    ue, pe = ENV_NAMES.get(slug.lower(), (f"{slug.upper()}_USER", f"{slug.upper()}_PASSWORD"))
    user = (p.get("username") or os.environ.get(ue, "") or "").strip()
    pwd = (p.get("password") or os.environ.get(pe, "") or "").strip()
    src = "HQ" if p.get("password") else "env"
    if os.environ.get("HQ_CRED_QUIET") != "1":
        print(f"[hq] {slug}: login from {src}{' (missing!)' if not (user and pwd) else ''}", file=sys.stderr)
    return user, pwd

def bot_settings(workflow_file: str) -> dict:
    d = _get("/api/sync/bot", {"workflow": workflow_file}) or {}
    s = d.get("settings")
    return s if isinstance(s, dict) else {}

def apply_env_overrides(workflow_file: str) -> dict:
    """Export HQ settings as env (BOT_LIMIT, BOT_BATCH, BOT_SUPPLIERS) unless already set."""
    s = bot_settings(workflow_file)
    if s.get("limit"): os.environ.setdefault("BOT_LIMIT", str(s["limit"]))
    if s.get("batch"): os.environ.setdefault("BOT_BATCH", str(s["batch"]))
    if s.get("suppliers"): os.environ.setdefault("BOT_SUPPLIERS", ",".join(s["suppliers"]))
    return s

if __name__ == "__main__":  # CLI: python -m suppliers.hq_credentials (cwd bot/) lkq  -> exports to $GITHUB_ENV (masked)
    ghenv = os.environ.get("GITHUB_ENV")
    for slug in sys.argv[1:]:
        u, p = supplier_login(slug)
        ue, pe = ENV_NAMES.get(slug, (f"{slug.upper()}_USER", f"{slug.upper()}_PASSWORD"))
        for v in (u, p):
            if v: print(f"::add-mask::{v}")
        if ghenv and u and p:
            with open(ghenv, "a") as f:
                f.write(f"{ue}={u}\n{pe}={p}\n")


def export_logins(*slugs: str) -> None:
    """Put HQ logins into os.environ (LKQ_USER/LKQ_PASSWORD …) so legacy scripts
    that read env directly use HQ too. HQ wins; existing env stays when HQ has none."""
    for slug in slugs or tuple(ENV_NAMES):
        p = supplier_profile(slug)
        if p.get("username") and p.get("password"):
            ue, pe = ENV_NAMES.get(slug, (f"{slug.upper()}_USER", f"{slug.upper()}_PASSWORD"))
            os.environ[ue], os.environ[pe] = p["username"], p["password"]
