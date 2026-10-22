"""Jednorazová sonda: aké polia dostupnosti/skladu vracia LKQ ProductList (bez zápisu do WC)."""
import json, os, re, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(__file__))
from suppliers import get_supplier

sup = get_supplier("lkq")
cats = os.environ.get("PROBE_CATS", "")
if cats:
    sup.credentials["category_url"] = cats
urls = sup.discover_product_urls(limit=int(os.environ.get("PROBE_N", "120")))
print("items", len(urls))
keys = Counter(); disp = Counter(); combos = Counter()
for u, it in list(sup._items_cache.items()):
    for k, v in it.items():
        if re.search(r"stock|storage|deliver|avail|dispon|warehouse|sklad|orderable|country|origin|central", k, re.I):
            keys[k] += 1
    d = it.get("Disponibility")
    disp[json.dumps(d, ensure_ascii=False, sort_keys=True)[:300]] += 1
    combos[(str(it.get("StorageStockAmount")), str(it.get("IsOrderable")), str((d or {}).get("Text") if isinstance(d, dict) else d))] += 1
print("KEYS", keys.most_common())
print("DISPONIBILITY:")
for k, v in disp.most_common(40): print(v, k)
print("COMBOS (stock, orderable, text):")
for k, v in combos.most_common(40): print(v, k)
sample = next(iter(sup._items_cache.values()), {})
print("SAMPLE", json.dumps({k: v for k, v in sample.items() if not isinstance(v, (list, dict)) or k in ("Disponibility",)}, ensure_ascii=False)[:3000])
