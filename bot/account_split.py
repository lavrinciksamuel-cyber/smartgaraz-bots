"""Rozdelenie práce medzi GitHub účty (repo A = index 0, repo B = index 1).

SG_ACCOUNT_INDEX / SG_ACCOUNT_COUNT (repo variables). Bez nich = 0/1 → robí všetko.
Kľúč (SKU, ID produktu, URL, job key) patrí účtu podľa crc32(kľúč) % COUNT,
takže dva účty nikdy nespracúvajú ten istý produkt/úlohu naraz.
"""
from __future__ import annotations
import os, zlib

def index() -> int:
    try: return int(os.environ.get("SG_ACCOUNT_INDEX", "0") or 0)
    except ValueError: return 0

def count() -> int:
    try: return max(1, int(os.environ.get("SG_ACCOUNT_COUNT", "1") or 1))
    except ValueError: return 1

def mine(key) -> bool:
    n = count()
    if n <= 1:
        return True
    return zlib.crc32(str(key).encode("utf-8")) % n == index() % n

def product_key(p: dict) -> str:
    """Stabilný kľúč produktu: SKU, inak WC ID."""
    return str((p or {}).get("sku") or (p or {}).get("id") or "")

def filter_products(rows):
    return [p for p in rows if mine(product_key(p))]
