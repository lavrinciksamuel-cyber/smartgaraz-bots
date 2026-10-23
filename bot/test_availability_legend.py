"""Dostupnosť = presne 3 stavy legendy (Skladom v SR / Sklad v EÚ / Nedostupné)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import availability as av  # noqa: E402


def raw(src, st="instock", **extra):
    return {"source": src, "stock_status": st, "raw_extra": extra}


def test_labels_only_three():
    assert set(av.LABELS) == {"sr", "eu", "na"}
    assert av.LABELS["sr"] == ("Skladom v SR", "2–3 pracovné dni")
    assert av.LABELS["eu"] == ("Sklad v EÚ", "5–10 pracovných dní")
    assert av.LABELS["na"][0] == "Nedostupné"


def test_tss_texts():
    assert av.classify_raw(raw("tss", stock_text="Skladom"))["avail"] == "sr"
    assert av.classify_raw(raw("tss", "onbackorder", stock_text="Na ceste"))["avail"] == "eu"
    assert av.classify_raw(raw("tss", "onbackorder", stock_text="Na objednávku"))["avail"] == "na"
    assert av.classify_raw(raw("tss", "outofstock", stock_text="Nie je skladom"))["avail"] == "na"
    assert av.classify_raw(raw("tss", "onbackorder", stock_text="Na objednávku do 7 dní"))["avail"] == "eu"


def test_simple_suppliers():
    assert av.classify_raw(raw("pro-tec"))["avail"] == "sr"
    assert av.classify_raw(raw("jlm"))["avail"] == "sr"
    assert av.classify_raw(raw("pro-tec", "outofstock"))["avail"] == "na"
    assert av.classify_raw(raw("ptak-mopar"))["avail"] == "eu"


def test_unknown_never_shown():
    p = {"meta_data": []}
    assert av.apply_to_payload(p, raw("wurth")) == "eu"
    assert av.apply_to_payload({}, raw("szekop")) == "eu"
    assert av.apply_to_payload({}, {"source": "lkq", "stock_status": "instock", "raw_extra": {}}) == "eu"


def test_na_not_orderable():
    p = {}
    av.stock_payload(p, "na")
    assert p["stock_status"] == "outofstock"


def test_existing_mapping():
    def ex(src, st="instock", **m):
        return av.to_legend(av.classify_existing({"stock_status": st}, dict(_supplier_source=src, **m)))["avail"]
    assert ex("wurth") == "eu"
    assert ex("pro-tec") == "sr"
    assert ex("metabond") == "sr"
    assert ex("ptak-mopar") == "eu"
    assert ex("wc-audit") == "eu"
    assert ex("pro-tec", "outofstock") == "na"
    assert ex("lkq", _delivery_text="na tel. otázku", _supplier_stock="") in ("eu", "na")
