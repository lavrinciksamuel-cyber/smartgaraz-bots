#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""20 % floor, Würth DPH, Pro-Tec parser, Würth SK súbor."""
from __future__ import annotations

import io
import os
import sys
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pricing import bad_cost_reason, calculate_retail  # noqa: E402
from reprice_min_profit import decide, summarize  # noqa: E402
from suppliers.protec import ProtecSupplier, select_tracked_product  # noqa: E402
from wurth_sk_feed import (  # noqa: E402
    FeedRow,
    interpret_stock,
    parse_csv,
    parse_feed,
    plan_content_update,
    plan_update,
)

ENV_KEYS = (
    "MIN_PROFIT_FACTOR", "MARGIN_PERCENT", "SHIPPING_FLAT", "PRICE_ENDING",
    "MIN_MARGIN_EUR", "COST_HAS_VAT", "VAT_RATE", "WURTH_VAT_FACTOR",
    "WURTH_ORDER_FEE", "WURTH_FREE_ORDER_NET", "MOPAR_VAT_FACTOR",
    "MOPAR_MARGIN_PERCENT", "MOPAR_SHIPPING_FLAT", "IDEO_MARGIN_PERCENT",
    "OIL_MARGIN_PERCENT", "COST_OUTLIER_RATIO", "PROTEC_APPLY_MIN_ORDER_TO_MOC",
)


def retail(cost, supplier="", **kw):
    defaults = dict(
        margin_percent=35.0, shipping_flat=0.60, price_ending=0.90,
        min_margin_eur=1.50, cost_has_vat=True, vat_rate=0.0,
    )
    defaults.update(kw)
    return calculate_retail(cost, supplier=supplier, **defaults).retail_price


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._old = {k: os.environ.pop(k, None) for k in ENV_KEYS}

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PricingFloor(EnvGuard):
    def test_wurth_large_net_uses_vat_then_margin(self):
        self.assertEqual(retail(1802.89, "wurth"), 2994.90)

    def test_wurth_small_basket_fee_floor(self):
        self.assertEqual(retail(11.90, "wurth"), 24.90)

    def test_wurth_fee_applies_through_99_99_and_stops_at_100(self):
        from pricing import wurth_price_floor
        self.assertEqual(wurth_price_floor(99.99), 132.90)
        self.assertEqual(wurth_price_floor(100.0), 124.90)

    def test_stored_cost_stays_net(self):
        result = calculate_retail(
            1802.89, supplier="wurth", margin_percent=35, shipping_flat=0.60,
            price_ending=0.90, min_margin_eur=1.50, cost_has_vat=True,
        )
        self.assertEqual(result.cost_price, 1802.89)

    def test_lkq_equal_to_cost_lifts_to_twenty(self):
        self.assertEqual(retail(8.41, "lkq", retail_hint=8.41), 10.90)

    def test_lkq_hint_above_floor_stays_unrounded(self):
        self.assertEqual(retail(20, "lkq", retail_hint=36.29), 36.29)

    def test_protec_equal_retail_lifts(self):
        self.assertEqual(retail(528.88, "pro-tec", retail_hint=528.88), 634.90)
        self.assertEqual(retail(453.32, "pro-tec", retail_hint=453.32), 544.90)

    def test_protec_dealer_below_public_keeps_public_when_higher(self):
        # 43201: dealerská 6,46 s DPH, verejná 9,23 je nad 20 %.
        self.assertEqual(retail(6.46, "pro-tec", retail_hint=9.23), 9.23)

    def test_ideo_mopar_tss_stay_on_their_higher_rule(self):
        self.assertEqual(retail(100, "ideo"), 130.90)
        self.assertEqual(retail(100, "tss"), 130.90)
        self.assertEqual(retail(8.63, "mopar"), 13.90)

    def test_generic_zero_margin_still_hits_floor(self):
        self.assertEqual(
            retail(10, "acme", margin_percent=0, shipping_flat=0, min_margin_eur=0),
            12.90,
        )

    def test_szekop_hint_above_floor_kept(self):
        self.assertEqual(retail(10, "szekop", retail_hint=36.29), 36.29)


class BadCost(EnvGuard):
    def test_p2109_is_outlier(self):
        self.assertEqual(bad_cost_reason(24.48, 2398.50, supplier="pro-tec", sku="P2109"), "cost_outlier")

    def test_missing(self):
        self.assertEqual(bad_cost_reason(None, 10, supplier="lkq"), "missing_cost")
        self.assertEqual(bad_cost_reason(0, 10), "missing_cost")

    def test_cheap_wurth_fee_floor_is_not_outlier(self):
        price = retail(0.50, "wurth")
        self.assertIsNone(bad_cost_reason(0.50, price, supplier="wurth", sku="0890001"))

    def test_reprice_flags_p2109_and_does_not_drop_it(self):
        d = decide({
            "id": 9, "sku": "P2109", "name": "zle",
            "regular_price": "2398.50", "price": "2398.50",
            "meta_data": [
                {"key": "_supplier_source", "value": "pro-tec"},
                {"key": "_cost_price", "value": "24.48"},
            ],
        })
        self.assertEqual(d.action, "flag")
        self.assertEqual(d.reason, "cost_outlier")
        self.assertIsNone(d.update)

    def test_protec_without_parser_marker_is_not_repriced(self):
        d = decide({
            "id": 1, "sku": "26275", "regular_price": "8.41", "price": "8.41",
            "meta_data": [
                {"key": "_supplier_source", "value": "pro-tec"},
                {"key": "_cost_price", "value": "8.41"},
            ],
        })
        self.assertEqual(d.reason, "protec_needs_resync")
        self.assertIsNone(d.update)

    def test_lkq_below_floor_updates_price_only(self):
        d = decide({
            "id": 2, "sku": "REN8550504593", "regular_price": "8.41", "price": "8.41",
            "meta_data": [
                {"key": "_supplier_source", "value": "lkq"},
                {"key": "_cost_price", "value": "8.41"},
            ],
        })
        self.assertEqual(d.action, "reprice")
        self.assertEqual(d.update["regular_price"], "10.90")
        self.assertNotIn("status", d.update)

    def test_summary_counts(self):
        rows = [
            decide({"id": 2, "sku": "A", "regular_price": "8.41", "price": "8.41",
                    "meta_data": [{"key": "_supplier_source", "value": "lkq"}, {"key": "_cost_price", "value": "8.41"}]}),
            decide({"id": 9, "sku": "P2109", "regular_price": "2398.50", "price": "2398.50",
                    "meta_data": [{"key": "_supplier_source", "value": "pro-tec"}, {"key": "_cost_price", "value": "24.48"}]}),
        ]
        s = summarize(rows)
        self.assertEqual(s["repriced_by_supplier"].get("lkq"), 1)
        self.assertEqual(s["flagged"][0]["sku"], "P2109")
        self.assertEqual(s["protec_waiting_resync"], 0)


class ProtecParser(unittest.TestCase):
    def _supplier(self, html: str, logged_in: bool):
        class Resp:
            def __init__(self, text):
                self.text = text
            def raise_for_status(self):
                return None

        class Http:
            def get(self, url, **kwargs):
                return Resp(html)

        sup = ProtecSupplier(http=Http(), credentials={})
        sup._logged_in = logged_in
        sup._fetch_public_prices = lambda url: (12.40, 0.0)
        return sup

    def test_does_not_take_banner_first(self):
        html = (ROOT / "data" / "fixtures" / "protec_banner_page.html").read_text(encoding="utf-8")
        product = self._supplier(html, True).parse_product("https://www.pro-tec-slovakia.sk/xcleaner/")
        self.assertIsNotNone(product)
        self.assertEqual(product.sku, "26275")
        self.assertNotIn("P1241", product.sku)
        self.assertNotIn("DIESEL", product.name.upper())
        self.assertEqual(float(product.cost_price), 6.46)
        self.assertNotEqual(float(product.cost_price), 10.34)

    def test_banner_only_tracking_uses_page_code_and_price_block(self):
        html = (ROOT / "data" / "fixtures" / "protec_banner_only.html").read_text(encoding="utf-8")
        product = self._supplier(html, True).parse_product("https://www.pro-tec-slovakia.sk/xcleaner/")
        self.assertEqual(product.sku, "P2109")
        self.assertNotEqual(product.sku, "P1241")
        self.assertEqual(float(product.cost_price), 24.48)
        self.assertIn("Xcleaner", product.name)

    def test_select_helper_rejects_unmatched(self):
        products = {"b": {"content_ids": ["P1241"], "value": 10.34}}
        self.assertEqual(select_tracked_product(products, "26275"), {})


class WurthFeed(EnvGuard):
    def test_csv_flexible_headers(self):
        raw = "Číslo artiklu;Cena bez DPH;MJ/balenie;EAN;Sklad\n0893964500;1 802,89;1;4001234567890;Skladom v SR\n".encode("utf-8")
        rows = parse_csv(raw)
        self.assertEqual(rows[0].sku, "0893964500")
        self.assertAlmostEqual(rows[0].net_price, 1802.89, places=2)
        self.assertEqual(rows[0].pack_qty, 1)
        self.assertEqual(rows[0].ean, "4001234567890")
        stock = interpret_stock(rows[0])
        self.assertEqual(stock["avail"], "sr")
        self.assertEqual(stock["origin"], "SK")

    def test_bmecat_and_missing_fields_do_not_guess(self):
        xml = b"""<BMECAT><T_NEW_CATALOG><ARTICLE>
          <SUPPLIER_AID>08931065</SUPPLIER_AID>
          <ARTICLE_DETAILS><DESCRIPTION_SHORT>Cistic</DESCRIPTION_SHORT><EAN>111</EAN></ARTICLE_DETAILS>
          <ARTICLE_PRICE_DETAILS><ARTICLE_PRICE price_type="net_customer"><PRICE_AMOUNT>11.90</PRICE_AMOUNT></ARTICLE_PRICE></ARTICLE_PRICE_DETAILS>
        </ARTICLE></T_NEW_CATALOG></BMECAT>"""
        row = parse_feed(xml, "cennik.xml")[0]
        self.assertEqual(row.sku, "08931065")
        self.assertAlmostEqual(row.net_price, 11.90)
        plan = plan_update(row, {"id": 5, "name": "stary", "sku": "08931065"})
        self.assertEqual(plan["payload"]["regular_price"], "24.90")
        self.assertNotIn("status", plan["payload"])
        self.assertNotIn("stock_status", plan["payload"])
        self.assertIn("no_fresh_stock", plan["notes"])

    def test_no_price_keeps_current_price(self):
        plan = plan_update(FeedRow(sku="1", stock_text="Nedostupné"), {"id": 3, "name": "x", "regular_price": "10.90"})
        self.assertNotIn("regular_price", plan["payload"])
        self.assertEqual(plan["payload"]["stock_status"], "outofstock")
        self.assertNotIn("status", plan["payload"])

    def test_new_sku_is_draft(self):
        plan = plan_update(FeedRow(sku="NEW1", net_price=11.90, name="Novy"))
        self.assertEqual(plan["payload"]["status"], "draft")
        self.assertEqual(plan["payload"]["regular_price"], "24.90")

    def test_keep_price_sku(self):
        plan = plan_update(FeedRow(sku="0500300002", net_price=1.0), {"id": 8, "name": "kus"})
        self.assertNotIn("regular_price", plan["payload"])
        self.assertIn("keep_price", plan["notes"])

    def test_xlsx_roundtrip(self):
        data = _xlsx(["Artikel", "Cena"], [["07201111", "4,50"]])
        row = parse_feed(data, "a.xlsx")[0]
        self.assertEqual(row.sku, "07201111")
        self.assertAlmostEqual(row.net_price, 4.50)

    def test_content_update_has_no_price_or_stock(self):
        payload = plan_content_update(4, description="text", image_url="https://eshop.wurth.ba/a.jpg", pack_qty=10)
        for key in ("regular_price", "sale_price", "stock_status", "stock_quantity", "status"):
            self.assertNotIn(key, payload)


def _xlsx(headers, data_rows) -> bytes:
    strings = list(headers) + [c for row in data_rows for c in row]
    shared = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{shared}</sst>"
    )
    rows_xml = []
    all_rows = [headers] + data_rows
    cursor = 0
    for r, row in enumerate(all_rows, start=1):
        cells = []
        for c, _val in enumerate(row):
            col = chr(ord("A") + c)
            cells.append(f'<c r="{col}{r}" t="s"><v>{cursor}</v></c>')
            cursor += 1
        rows_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
