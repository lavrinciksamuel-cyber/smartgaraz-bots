#!/usr/bin/env python3
"""SmartGaráž – externý bezpečnostný skener všetkých webov (len štandardná knižnica Pythonu).

Beží v GitHub Actions (verejné repo → 0 €) každých 15 min:
  1. podpísane (HMAC) stiahne zoznam webov z HQ  GET  /api/sec/scanner/sites
     (keď HQ neodpovie, použije security/sites.default.json),
  2. pre každý web zmeria: dostupnosť, SSL, DNS (DoH Cloudflare), hlavičky, obsah/defacement,
     raz za hodinu aj odhalené citlivé súbory, WordPress a čierne listiny,
  3. podpísane pošle surové pozorovania do HQ  POST /api/sec/scanner/report  – vyhodnotenie,
     incidenty a upozornenia robí HQ.

VEREJNÉ LOGY: vypisujú sa len počty – žiadne nálezy, cesty, IP ani tajomstvá.
Env: SEC_SCANNER_SECRET (GitHub secret), SEC_HQ_URL (voliteľné), SEC_FULL=1 (vynúti hodinové kontroly),
     SEC_DRY=1 (nič neposiela, vypíše len počty).
"""
from __future__ import annotations

import hashlib
import hmac
import html.parser
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VERSION = "1.0"
PROTO = "SGSEC1"
UA = "SmartGarazSecMonitor/1.0 (+https://hq.smartgaraz.sk; bezpecnostny monitoring vlastnych webov)"
TIMEOUT = 15
MAX_BODY = 2_000_000
HERE = os.path.dirname(os.path.abspath(__file__))

SPAM_RE = re.compile(
    r"\b(viagra|cialis|levitra|casino|kasino|poker online|slot gacor|slot online|togel|judi bola|bokep|porn|xxx|"
    r"payday loan|replica watch|cheap jerseys|essay writing|buy followers|crypto giveaway|hacked by|defaced by)\b",
    re.I,
)

# (cesta, krátky dôvod, test obsahu) – nález sa hlási len ak obsah naozaj sedí (žiadne falošné 200 zo SPA / soft-404)
EXPOSED = [
    ("/.env", "env", lambda b, ct: re.search(rb"(?m)^[A-Z][A-Z0-9_]{2,}=", b) is not None and b"<html" not in b[:500].lower()),
    ("/.git/config", "git", lambda b, ct: b"[core]" in b),
    ("/.git/HEAD", "git", lambda b, ct: b.startswith(b"ref: refs/")),
    ("/wp-config.php.bak", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-config.php~", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-config.php.save", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-config.php.old", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-config.php.orig", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-config.bak", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/.wp-config.php.swp", "wp-config", lambda b, ct: b"DB_PASSWORD" in b or b"DB_NAME" in b),
    ("/wp-content/debug.log", "debug.log", lambda b, ct: re.search(rb"PHP (Warning|Notice|Fatal|Deprecated|Parse)", b) is not None),
    ("/debug.log", "debug.log", lambda b, ct: re.search(rb"PHP (Warning|Notice|Fatal|Deprecated|Parse)", b) is not None),
    ("/error_log", "error_log", lambda b, ct: re.search(rb"PHP (Warning|Notice|Fatal|Deprecated|Parse)", b) is not None),
    ("/phpinfo.php", "phpinfo", lambda b, ct: b"phpinfo()" in b or b"PHP Version" in b),
    ("/info.php", "phpinfo", lambda b, ct: b"phpinfo()" in b or b"PHP Version" in b),
    ("/_sgtmp/", "_sgtmp", lambda b, ct: b"Index of" in b or (b"<html" not in b[:500].lower() and len(b) > 0)),
    ("/.DS_Store", "ds_store", lambda b, ct: b[4:8] == b"Bud1"),
    ("/backup.sql", "sql", lambda b, ct: re.search(rb"CREATE TABLE|INSERT INTO", b) is not None),
    ("/dump.sql", "sql", lambda b, ct: re.search(rb"CREATE TABLE|INSERT INTO", b) is not None),
    ("/db.sql", "sql", lambda b, ct: re.search(rb"CREATE TABLE|INSERT INTO", b) is not None),
    ("/.htpasswd", "htpasswd", lambda b, ct: re.search(rb"(?m)^[\w.-]+:\$?[\w./$]{10,}", b) is not None and b"<html" not in b[:500].lower()),
    ("/server-status", "server-status", lambda b, ct: b"Apache Server Status" in b),
]

# DNSBL pre DOMÉNY (bez kľúča). Verejné resolvery (Cloudflare DoH) bývajú zablokované → najprv sa overí
# testovací záznam zoznamu; keď resolver nefunguje, skúsi sa systémový resolver runnera, inak „neoverené“ (nie nález).
DOMAIN_BL = {
    # názov: (zóna, testovací záznam, je_na_liste(ip), resolver_zablokovaný(ip))
    "spamhaus-dbl": ("dbl.spamhaus.org", "dbltest.com", lambda ip: ip.startswith("127.0.1."), lambda ip: ip.startswith("127.255.255.")),
    "surbl": ("multi.surbl.org", "test.surbl.org", lambda ip: ip.startswith("127.0.0.") and ip != "127.0.0.1", lambda ip: ip == "127.0.0.1"),
    "uribl": ("multi.uribl.com", "test.uribl.com", lambda ip: ip in ("127.0.0.2", "127.0.0.4", "127.0.0.8", "127.0.0.14"), lambda ip: ip == "127.0.0.1" or ip.startswith("127.0.0.255")),
}


# ---------------- podpis (zhodné s hq/lib/sec_core.php) ----------------
def canon(method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    return f"{PROTO}\n{method.upper()}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"


def sign(secret: str, c: str) -> str:
    return hmac.new(secret.encode(), c.encode(), hashlib.sha256).hexdigest()


def signed_headers(secret: str, method: str, path: str, body: bytes, key: str = "scanner") -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    return {"X-SG-Key": key, "X-SG-Ts": ts, "X-SG-Nonce": nonce, "X-SG-Sig": sign(secret, canon(method, path, ts, nonce, body))}


# ---------------- HTTP ----------------
class Resp:
    def __init__(self, status=0, headers=None, body=b"", error="", ms=0, url=""):
        self.status, self.headers, self.body, self.error, self.ms, self.url = status, headers or {}, body, error, ms, url

    def h(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


def request(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None,
            max_bytes: int = MAX_BODY, timeout: int = TIMEOUT) -> Resp:
    """Jedna požiadavka bez presmerovaní. Overuje certifikát."""
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    t0 = time.monotonic()
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        hd = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        hd.update(headers or {})
        conn.request(method, path, body=body, headers=hd)
        r = conn.getresponse()
        data = r.read(max_bytes)
        hs = {}
        for k, v in r.getheaders():
            k = k.lower()
            hs[k] = (hs[k] + ", " + v) if k in hs and k != "set-cookie" else v
        conn.close()
        return Resp(r.status, hs, data, "", int((time.monotonic() - t0) * 1000), url)
    except ssl.SSLCertVerificationError as e:
        return Resp(0, {}, b"", "SSL: " + (e.verify_message or "overenie zlyhalo")[:80], int((time.monotonic() - t0) * 1000), url)
    except (socket.timeout, TimeoutError):
        return Resp(0, {}, b"", "timeout", int((time.monotonic() - t0) * 1000), url)
    except Exception as e:  # noqa: BLE001
        return Resp(0, {}, b"", type(e).__name__ + (": " + str(e)[:80] if str(e) else ""), int((time.monotonic() - t0) * 1000), url)


def fetch_follow(url: str, hops: int = 5) -> tuple[Resp, Resp]:
    """Vráti (prvá odpoveď, posledná odpoveď) po nanajvýš `hops` presmerovaniach."""
    first = r = request(url)
    seen = 0
    while r.status in (301, 302, 303, 307, 308) and r.h("location") and seen < hops:
        nxt = urllib.parse.urljoin(r.url, r.h("location"))
        if not nxt.startswith(("http://", "https://")):
            break
        r = request(nxt)
        seen += 1
    return first, r


# ---------------- DNS cez DoH ----------------
def doh(name: str, rtype: str) -> list[str]:
    q = urllib.parse.urlencode({"name": name, "type": rtype})
    r = request(f"https://cloudflare-dns.com/dns-query?{q}", headers={"Accept": "application/dns-json"}, max_bytes=200_000, timeout=10)
    if r.status != 200:
        raise RuntimeError(f"DoH HTTP {r.status or r.error}")
    d = json.loads(r.body.decode("utf-8", "replace"))
    if d.get("Status") not in (0, 3):  # 3 = NXDOMAIN
        raise RuntimeError(f"DoH status {d.get('Status')}")
    want = {"A": 1, "AAAA": 28, "MX": 15, "NS": 2, "TXT": 16}[rtype]
    out = []
    for a in d.get("Answer") or []:
        if a.get("type") != want:
            continue
        v = str(a.get("data", "")).strip()
        if rtype == "MX":
            parts = v.split()
            v = parts[-1] if parts else v
        if rtype == "TXT":
            v = v.replace('" "', "").strip('"')
        out.append(v.rstrip(".").lower() if rtype != "TXT" else v)
    return sorted(set(out))


def dns_records(domain: str) -> dict:
    rec = {t: doh(domain, t) for t in ("A", "AAAA", "MX", "NS")}
    rec["SPF"] = [t for t in doh(domain, "TXT") if t.lower().startswith("v=spf1")]
    rec["DMARC"] = [t for t in doh("_dmarc." + domain, "TXT") if t.lower().startswith("v=dmarc1")]
    return rec


def _sys_a(name: str) -> list[str]:
    try:
        return sorted(set(socket.gethostbyname_ex(name)[2]))
    except socket.gaierror as e:
        if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
            return []
        raise


def bl_classify(ips: list[str], is_listed, is_blocked) -> str:
    """'listed' | 'clean' | 'unknown' (zablokovaný/podivný resolver)."""
    if any(is_blocked(i) for i in ips):
        return "unknown"
    if any(is_listed(i) for i in ips):
        return "listed"
    if any(not i.startswith("127.") for i in ips):
        return "unknown"
    return "clean"


def blacklist(domain: str) -> dict:
    listed, unknown = [], []
    for name, (zone, test, is_listed, is_blocked) in DOMAIN_BL.items():
        res = "unknown"
        for resolve in (lambda n: doh(n, "A"), _sys_a):
            try:
                if bl_classify(resolve(f"{test}.{zone}"), is_listed, is_blocked) != "listed":
                    continue  # tento resolver zoznam nevie overiť
                res = bl_classify(resolve(f"{domain}.{zone}"), is_listed, is_blocked)
            except Exception:  # noqa: BLE001
                continue
            if res != "unknown":
                break
        if res == "listed":
            listed.append(name)
        elif res == "unknown":
            unknown.append(name)
    return {"checked": True, "listed": listed, "unknown": unknown, "all": list(DOMAIN_BL)}


# ---------------- TLS ----------------
def tls_info(host: str) -> dict:
    out = {"host": host, "ok": False}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
            c = ss.getpeercert()
        na = datetime.strptime(c["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        iss = dict(x[0] for x in c.get("issuer", ()))
        out.update(ok=True, days_left=int((na - datetime.now(timezone.utc)).total_seconds() // 86400),
                   not_after=na.strftime("%Y-%m-%d"), issuer=(iss.get("organizationName") or iss.get("commonName") or "")[:120])
    except ssl.SSLCertVerificationError as e:
        out["error"] = (e.verify_message or "overenie zlyhalo")[:150]
    except Exception as e:  # noqa: BLE001
        out["error"] = type(e).__name__ + (": " + str(e)[:100] if str(e) else "")
    return out


# ---------------- obsah ----------------
class _Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self._in_title = "", False
        self.scripts, self.iframes, self.links, self.generator = set(), set(), set(), ""
        self.text = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("src"):
            self.scripts.add(a["src"])
        elif tag == "iframe" and a.get("src"):
            self.iframes.add(a["src"])
        elif tag == "a" and a.get("href"):
            self.links.add(a["href"])
        elif tag == "meta" and a.get("name", "").lower() == "generator":
            self.generator = a.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data
        elif len(self.text) < 5000:
            self.text.append(data)


def _hosts(urls, base: str, own: set[str]) -> list[str]:
    out = set()
    for x in urls:
        h = (urllib.parse.urlsplit(urllib.parse.urljoin(base, x)).hostname or "").lower()
        if h and h not in own and not any(h.endswith("." + o) for o in own):
            out.add(h)
    return sorted(out)


def analyze_content(body: bytes, base: str, own: set[str], expect: str = "") -> tuple[dict, str]:
    text = body.decode("utf-8", "replace")
    p = _Page()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001
        pass
    visible = " ".join(p.text)
    spam = sorted({m.group(0).lower() for m in SPAM_RE.finditer(visible + " " + p.title)})
    # hash bez nonce/časových hodnôt – stabilnejší medzi behmi (slúži len ako indikátor)
    norm = re.sub(r"(nonce|ver|v|_wpnonce|ts|t)=[\w.-]+|\d{9,}", "", text)
    norm = re.sub(r"\s+", " ", norm)
    c = {
        "title": re.sub(r"\s+", " ", p.title).strip()[:200],
        "script_domains": _hosts(p.scripts, base, own)[:80],
        "iframe_domains": _hosts(p.iframes, base, own)[:40],
        "link_domains": _hosts(p.links, base, own)[:200],
        "spam_hits": spam[:20],
        "hash": hashlib.sha256(norm.encode()).hexdigest(),
    }
    if expect:
        c["expect_found"] = expect.lower() in text.lower()
    return c, p.generator


def sec_headers(h: Resp) -> dict:
    csp = h.h("content-security-policy")
    return {
        "hsts": bool(h.h("strict-transport-security")),
        "xcto": "nosniff" in h.h("x-content-type-options").lower(),
        "xfo": bool(h.h("x-frame-options")),
        "csp": bool(csp),
        "csp_frame_ancestors": "frame-ancestors" in csp.lower(),
        "referrer": bool(h.h("referrer-policy")),
        "x_powered_by": h.h("x-powered-by")[:80],
    }


def exposed(base: str) -> dict:
    found = []
    for path, why, test in EXPOSED:
        r = request(urllib.parse.urljoin(base, path), headers={"Range": "bytes=0-8191"}, max_bytes=8192, timeout=10)
        if r.status in (200, 206) and r.body and test(r.body, r.h("content-type")):
            found.append({"path": path, "status": r.status, "why": why})
    return {"checked": True, "count": len(EXPOSED), "found": found}


_LATEST_WP: list = []


def latest_wp() -> str:
    if not _LATEST_WP:
        v = ""
        r = request("https://api.wordpress.org/core/version-check/1.7/", max_bytes=500_000)
        try:
            v = json.loads(r.body)["offers"][0]["version"]
        except Exception:  # noqa: BLE001
            pass
        _LATEST_WP.append(v)
    return _LATEST_WP[0]


def wp_probe(base: str, generator: str, body: bytes) -> dict:
    w = {"detected": True}
    m = re.search(r"WordPress\s+([\d.]+)", generator or "")
    if not m:
        f = request(urllib.parse.urljoin(base, "/feed/"), max_bytes=20000)
        m = re.search(rb"wordpress\.org/\?v=([\d.]+)", f.body or b"")
        w["version"] = m.group(1).decode() if m else ""
    else:
        w["version"] = m.group(1)
    w["latest"] = latest_wp()
    u = request(urllib.parse.urljoin(base, "/wp-json/wp/v2/users"), max_bytes=100_000)
    try:
        j = json.loads(u.body) if u.status == 200 else None
        w["rest_users"] = isinstance(j, list) and any(isinstance(x, dict) and x.get("slug") for x in j)
    except Exception:  # noqa: BLE001
        w["rest_users"] = False
    a = request(urllib.parse.urljoin(base, "/?author=1"), max_bytes=2000)
    w["author_slug_leak"] = a.status in (301, 302) and "/author/" in a.h("location")
    x = request(urllib.parse.urljoin(base, "/xmlrpc.php"), max_bytes=2000)
    w["xmlrpc"] = x.status == 405 and b"XML-RPC" in x.body
    rd = request(urllib.parse.urljoin(base, "/readme.html"), max_bytes=4000)
    w["readme"] = rd.status == 200 and b"WordPress" in rd.body
    return w


def is_wp(body: bytes, generator: str) -> bool:
    return "wordpress" in (generator or "").lower() or b"/wp-content/" in body or b"/wp-includes/" in body


# ---------------- jeden web ----------------
def scan_site(site: dict, full: bool) -> dict:
    cfg = site.get("cfg") or {}
    skip = set(cfg.get("skip") or [])
    kind = site.get("kind", "static")
    url = site["url"]
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    hosts = [h.lower() for h in (cfg.get("hosts") or [host]) if h]
    domain = cfg.get("domain") or ""
    own = set(hosts) | ({domain} if domain else set())
    obs: dict = {"slug": site["slug"]}
    stats = {"challenged": 0}

    if "uptime" not in skip or "content" not in skip or "headers" not in skip:
        first, last = fetch_follow(url)
        if kind == "redirect" or cfg.get("expect_redirect"):
            obs["http"] = {"status": first.status, "ms": first.ms, "error": first.error, "location": first.h("location")[:300]}
            obs["final_url"] = last.url
        elif "challenge" in last.h("cf-mitigated").lower():
            stats["challenged"] = 1  # Cloudflare výzva pre runner → nehlásiť ako výpadok (HQ cron má záložnú kontrolu)
        else:
            obs["http"] = {"status": last.status, "ms": first.ms + (last.ms if last is not first else 0), "error": last.error,
                           "location": first.h("location")[:300]}
            obs["final_url"] = last.url
            if last.status and last.status < 400:
                if "headers" not in skip:
                    obs["headers"] = sec_headers(last)
                if "content" not in skip:
                    obs["content"], gen = analyze_content(last.body, last.url, own, cfg.get("expect_text", ""))
                else:
                    gen = ""
                if full and "wp" not in skip and (kind == "wordpress" or is_wp(last.body, gen)):
                    obs["wp"] = wp_probe(last.url, gen, last.body)
    if "ssl" not in skip:
        obs["tls"] = [tls_info(h) for h in hosts[:4]]
    if "dns" not in skip and domain:
        try:
            obs["dns"] = dns_records(domain)
        except Exception as e:  # noqa: BLE001
            obs["dns"], obs["dns_error"] = {}, str(e)[:150]
    if full and "exposed" not in skip and kind != "redirect":
        base = obs.get("final_url") or url
        obs["exposed"] = exposed(base)
    if full and "blacklist" not in skip and domain:
        obs["blacklist"] = blacklist(domain)
    obs["_stats"] = stats
    return obs


# ---------------- HQ ----------------
def hq_call(hq: str, secret: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | None]:
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    hd = signed_headers(secret, method, path, body)
    hd["Content-Type"] = "application/json"
    r = request(hq.rstrip("/") + path, method=method, body=body if method == "POST" else None, headers=hd, timeout=30)
    try:
        return r.status, json.loads(r.body) if r.body else None
    except Exception:  # noqa: BLE001
        return r.status, None


def load_sites(hq: str, secret: str) -> tuple[list, dict, str]:
    if secret:
        st, d = hq_call(hq, secret, "GET", "/api/sec/scanner/sites")
        if st == 200 and isinstance(d, dict) and isinstance(d.get("sites"), list) and d["sites"]:
            return d["sites"], d.get("opts") or {}, "hq"
    with open(os.path.join(HERE, "sites.default.json"), encoding="utf-8") as f:
        return json.load(f)["sites"], {}, "default"


def is_full_run(now: time.struct_time) -> bool:
    return os.environ.get("SEC_FULL") == "1" or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or now.tm_min < 15


def main() -> int:
    t0 = time.monotonic()
    secret = os.environ.get("SEC_SCANNER_SECRET", "").strip()
    hq = os.environ.get("SEC_HQ_URL", "").strip() or "https://hq.smartgaraz.sk"
    dry = os.environ.get("SEC_DRY") == "1"
    if not secret and not dry:
        print("security-monitor: SEC_SCANNER_SECRET nie je nastavený – preskakujem (0 webov).")
        return 0
    now = time.gmtime()
    full = is_full_run(now)
    sites, _opts, src = load_sites(hq, secret)
    sites = [s for s in sites if s.get("slug") and s.get("url")][:30]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda s: scan_site(s, full), sites))
    challenged = sum(r.pop("_stats", {}).get("challenged", 0) for r in results)
    down = sum(1 for r in results if "http" in r and not 0 < r["http"]["status"] < 400)
    exp_found = sum(len(r.get("exposed", {}).get("found", [])) for r in results)
    ms = int((time.monotonic() - t0) * 1000)
    report = {"sites": results, "run": {"id": os.environ.get("GITHUB_RUN_ID", "local"), "version": VERSION, "ms": ms, "full": full, "sites_src": src}}
    # len počty – verejný log
    print(f"security-monitor: {len(results)} webov (zoznam: {src}), plná kontrola: {'áno' if full else 'nie'}, "
          f"nedostupných/chybových: {down}, CF výziev: {challenged}, nálezov súborov: {exp_found}, {ms} ms")
    if dry:
        return 0
    st, d = hq_call(hq, secret, "POST", "/api/sec/scanner/report", report)
    if st == 200 and isinstance(d, dict) and d.get("ok"):
        tot = {"checks": 0, "events": 0, "opened": 0}
        for v in (d.get("sites") or {}).values():
            for k in tot:
                tot[k] += int(v.get(k, 0)) if isinstance(v, dict) else 0
        print(f"security-monitor: HQ prijalo – kontrol {tot['checks']}, udalostí {tot['events']}, otvorených/aktualizovaných incidentov {tot['opened']}")
        return 0
    print(f"security-monitor: HQ report zlyhal (HTTP {st}).")
    # zlyhanie hlásime (červený beh → e-mail od GitHubu) len raz za hodinu, aby nechodilo 96 mailov denne
    return 1 if full else 0


if __name__ == "__main__":
    sys.exit(main())
