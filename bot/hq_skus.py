"""Existujúce SKU (+ supplier_url) z HQ (/api/sync/existing-skus, gzip, cache 3 min).

HQ zrkadlí wc_products takmer v reálnom čase (tracker ping pri každom uložení), je
rýchle a nezaťažuje WC REST. Doplníme len produkty s ID > HQ max_id (1–2 stránky WC).
Fallback: pôvodné stránkovanie WC REST, ak HQ zlyhá.
"""
from __future__ import annotations
import json, logging, os, time
from typing import Optional, Set, Tuple

LOGGER = logging.getLogger("hq_skus")
_CACHE = "/tmp/sg_hq_skus.json"
_mem: Optional[Tuple[float, Set[str], Set[str]]] = None


def _wc_auth():
    return (os.environ.get("WC_CONSUMER_KEY", ""), os.environ.get("WC_CONSUMER_SECRET", ""))


def _wc_since(max_id: int, skus: Set[str], urls: Set[str]) -> None:
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    if not url:
        return
    for page in range(1, 6):
        try:
            r = requests.get(f"{url}/wp-json/wc/v3/products", auth=_wc_auth(), timeout=90,
                             params={"per_page": 100, "page": page, "status": "any", "orderby": "id", "order": "desc",
                                     "_fields": "id,sku,meta_data"})
            if not r.ok:
                return
            rows = r.json()
        except Exception:
            return
        for p in rows:
            if int(p.get("id") or 0) <= max_id:
                return
            if p.get("sku"):
                skus.add(str(p["sku"]).strip())
            for m in p.get("meta_data") or []:
                if isinstance(m, dict) and m.get("key") == "supplier_url" and m.get("value"):
                    urls.add(str(m["value"]))
        if len(rows) < 100:
            return


def _wc_full() -> Set[str]:
    import requests
    url = os.environ.get("WC_URL", "").rstrip("/")
    out, page = set(), 1
    while page < 2000:
        try:
            r = requests.get(f"{url}/wp-json/wc/v3/products", auth=_wc_auth(), timeout=90,
                             params={"per_page": 100, "page": page, "status": "any", "_fields": "sku"})
        except Exception:
            break
        if not r.ok:
            break
        rows = r.json()
        out |= {str(x.get("sku") or "").strip() for x in rows if x.get("sku")}
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.2)
    return out


def existing(max_age_s: float = 600) -> Tuple[Set[str], Set[str], str]:
    """(skus, supplier_urls, zdroj). Cache v pamäti aj /tmp (zdieľané medzi subprocesmi jobu)."""
    global _mem
    if _mem and time.time() - _mem[0] < max_age_s:
        return _mem[1], _mem[2], "cache"
    try:
        if time.time() - os.path.getmtime(_CACHE) < max_age_s:
            d = json.load(open(_CACHE))
            _mem = (os.path.getmtime(_CACHE), set(d["skus"]), set(d.get("urls") or []))
            return _mem[1], _mem[2], "cache"
    except Exception:
        pass
    import requests
    tok = os.environ.get("HQ_SYNC_TOKEN", "").strip()
    hq = os.environ.get("HQ_URL", "https://hq.smartgaraz.sk").rstrip("/")
    skus: Set[str] = set(); urls: Set[str] = set(); src = "wc"
    if tok:
        try:
            t = time.time()
            r = requests.get(f"{hq}/api/sync/existing-skus", params={"urls": 1}, timeout=60,
                             headers={"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip", "User-Agent": "smartgaraz-bot"})
            if r.ok:
                d = r.json()
                skus = set(d.get("skus") or []); urls = set(d.get("urls") or [])
                _wc_since(int(d.get("max_id") or 0), skus, urls)
                src = "hq"
                LOGGER.info("HQ existujúce SKU: %d (+URL %d) za %.1fs", len(skus), len(urls), time.time() - t)
            else:
                LOGGER.warning("HQ existing-skus HTTP %s → fallback WC", r.status_code)
        except Exception as exc:
            LOGGER.warning("HQ existing-skus zlyhalo (%s) → fallback WC", type(exc).__name__)
    if src != "hq":
        skus = _wc_full()
        LOGGER.info("WC existujúce SKU (fallback): %d", len(skus))
    try:
        json.dump({"skus": sorted(skus), "urls": sorted(urls)}, open(_CACHE + ".tmp", "w"))
        os.replace(_CACHE + ".tmp", _CACHE)
    except Exception:
        pass
    _mem = (time.time(), skus, urls)
    return skus, urls, src
