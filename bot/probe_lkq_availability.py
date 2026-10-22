"""Jednorazová sonda: odkiaľ LKQ načítava reálnu dostupnosť (LoadDisponibility) — bez zápisu do WC."""
import json, os, re, sys
from collections import Counter
from urllib.parse import urljoin
sys.path.insert(0, os.path.dirname(__file__))
from suppliers import get_supplier

sup = get_supplier("lkq")
urls = sup.discover_product_urls(limit=int(os.environ.get("PROBE_N", "60")))
items = list(sup._items_cache.values())
print("items", len(items))
cat = items[0].get("_sg_category_url")
html = sup.http.get(cat).text
scripts = re.findall(r'src="([^"]+\.js[^"]*)"', html)
found = Counter()
for chunk in [html] + [sup.http.get(urljoin(sup.base_url, s)).text for s in scripts if "lkq" in s or s.startswith("/")][:25]:
    for m in re.finditer(r'["\'](/[A-Za-z]+/[A-Za-z/]*(?:Dispon|Storage|Stock|Availab|Delivery|TimeAvail)[A-Za-z/]*)["\']', chunk):
        found[m.group(1)] += 1
    for m in re.finditer(r'.{0,160}(?:LoadDisponibility|Disponibility\b).{0,200}', chunk):
        found["CTX:" + m.group(0)[:360]] += 1
for k, v in found.most_common(60):
    print("EP", v, k)
ids = [it["Id"] for it in items[:20]]
for ep in [p for p in found if not p.startswith("CTX:")][:8]:
    for payload in ({"ids": ids}, ids, {"productIds": ids}):
        try:
            r = sup.http.post(sup.base_url + ep, json=payload, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Referer": cat})
            print("TRY", ep, type(payload).__name__, r.status_code, r.text[:1500].replace("\n", " "))
            if r.status_code == 200 and r.text.strip()[:1] in "[{":
                break
        except Exception as e:
            print("TRY", ep, "ERR", e)
