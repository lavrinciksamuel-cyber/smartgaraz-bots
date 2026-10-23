#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verejný katalóg Würth: dva výpadky → Nedostupné, koncept bez ceny, slovenský názov ostáva."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import availability as av
import wurth_catalog_sync as sync

ROBOTS = """
User-agent:*
Disallow: /catalog/
Disallow: /*?q=
Disallow: /*?p=
Allow: /
"""

PAGE = """
<html><h1>Zaptivna masa za vjetrobranska stakla, Crna, 310ml</h1>
<div id="description" class="pdp-tab-content-box"><div class="ulcontent">
<span><strong>Masa za vanjsko zaptivanje vjetrobranskih stakala na automobilima.</strong></span>
<ul><li><span>Temperaturno postojan od -20°C do +80°C</span></li>
<li><span>Cijena 12,90 KM</span></li></ul>
</div></div>
<div class="wcb-field">Veličina pakovanja 12 kom</div>
<input data-product-code="0890100043" />
</html>
"""


class CatalogSyncTest(unittest.TestCase):
    def test_robots_allow_product_and_block_search(self):
        self.assertTrue(sync.robots_allows(ROBOTS, "/sitemap.xml"))
        self.assertTrue(sync.robots_allows(ROBOTS, "/0890100043/zaptivna-masa"))
        self.assertFalse(sync.robots_allows(ROBOTS, "/search?q=0890100043"))
        self.assertFalse(sync.robots_allows(ROBOTS, "/catalog/product/view/"))

    def test_buckets_skip_construction_and_keep_workshop(self):
        self.assertEqual(sync.bucket_for_path("/hemijsko-tehnicki-proizvodi/maziva-i-odstranjivaci-hrde/suhi"), "chemistry")
        self.assertIsNone(sync.bucket_for_path("/hemijsko-tehnicki-proizvodi/gradevinska-hemija/pena"))
        self.assertEqual(sync.bucket_for_path("/elementi-za-spajanje-opsti-metricki-colni/vijci/m6"), "fasteners")
        self.assertEqual(sync.bucket_for_path("/rucni-alati/kljucevi-za-vijke/vilasti"), "tools")
        self.assertIsNone(sync.bucket_for_path("/rucni-alati/gradevinski-zanatski-alati/lopatka"))
        self.assertEqual(
            sync.bucket_for_path("/masinska-obrada-materijala/sredstva-za-brusenje-i-poliranje/brusni-listovi"),
            "consumables",
        )
        self.assertIsNone(sync.bucket_for_path("/masinska-obrada-materijala/alati-za-glodanje-i-struganje/nozevi"))
        self.assertEqual(sync.bucket_for_path("/elektroinstalacije/kablovske-spojnice-za-elektroinstalacije/cu/y"), "electrical")
        self.assertEqual(sync.bucket_for_path("/dijelovi-za-vozila/elektrika-za-motorna-vozila/zarulje/h7"), "vehicle")
        self.assertEqual(sync.bucket_for_path("/crijeva-spojnice-obujmice-za-crijeva/crijeva/gorivo/8mm"), "consumables")
        self.assertEqual(sync.bucket_for_path("/radionicka-oprema/radionicka-oprema-za-motorna-vozila/zdvihac/x"), "workshop")
        self.assertEqual(sync.bucket_for_path("/zastita-na-radu/licna-zastitna-oprema/rukavice/xl"), "consumables")
        self.assertIsNone(sync.bucket_for_path("/elektroinstalacije/oprema-za-pametne-kuce-smart-home/rele/x"))
        self.assertIsNone(sync.bucket_for_path("/sanitarije-grijanje-vodoinstalacije-klima-sgk/armature/slavina"))
        self.assertIsNone(sync.bucket_for_path("/hahn-kolb-cnc-alati-i-oprema/rezni-alati/glodalo"))
        self.assertIsNone(sync.bucket_for_path("/elementi-za-spajanje-opsti-metricki-colni/vijci-za-drvo/4x40"))
        self.assertIsNone(sync.bucket_for_path("/crijeva-spojnice-obujmice-za-crijeva/oprema-za-navodnjavanje-connex/hadica"))

    def test_shop_category_uses_existing_tree_and_leaves_fasteners_unclear(self):
        self.assertEqual(
            sync.slug_category("/hemijsko-tehnicki-proizvodi/ljepila/pu/kartusa"),
            ["Dielenská chémia & Údržba", "Tesnenia & Lepidlá"],
        )
        self.assertEqual(
            sync.slug_category("/rucni-alati/program-nasadnih-kljuceva/1-2/gedore"),
            ["Náradie & Vybavenie", "Kľúče & račne"],
        )
        self.assertEqual(
            sync.slug_category("/dijelovi-za-vozila/klima-tehnologija-za-motorna-vozila/kompresor/x"),
            ["Náhradné diely", "Chladenie & kúrenie", "Diely klimatizácie"],
        )
        self.assertIsNone(sync.slug_category("/elementi-za-spajanje-opsti-metricki-colni/vijci-metricki-colni/m6/x"))
        self.assertIsNone(sync.slug_category("/zastita-na-radu/licna-zastitna-oprema/rukavice/xl"))
        self.assertEqual(
            sync.shop_category_for(
                "/hemijsko-tehnicki-proizvodi/sredstva-za-ciscenje-odrzavanje-i-njegu/auto/sampon",
                "Würth Šampón na karosériu",
            )[0],
            "Autokozmetika & Detail",
        )
        self.assertEqual(
            sync.shop_category_for(
                "/elementi-za-spajanje-opsti-metricki-colni/vijci-metricki-colni/koleso/m14",
                "Würth Skrutka kolesa M14",
            ),
            ["Náhradné diely", "Náprava & podvozok", "Ložiská kolies"],
        )
        rows = [
            {"id": 1, "name": "Náradie & Vybavenie", "parent": 0},
            {"id": 2, "name": "Kľúče & račne", "parent": 1},
            {"id": 9, "name": "Kľúče & račne", "parent": 0},
        ]
        self.assertEqual(sync.existing_category_leaf(rows, ["Náradie & Vybavenie", "Kľúče & račne"]), 2)
        self.assertIsNone(sync.existing_category_leaf(rows, ["Náradie & Vybavenie", "Neexistuje"]))

    def test_leaves_are_pages_without_children(self):
        leaves = sync.leaf_paths([
            "hemijsko/a/b/c",
            "hemijsko/a/b/c/produkt",
            "hemijsko/a/b/d",
        ])
        self.assertEqual(leaves, ["hemijsko/a/b/c/produkt", "hemijsko/a/b/d"])

    def test_two_misses_mark_unavailable_and_a_return_clears_it(self):
        public = {"111"}
        state, mark, restore, seen = sync.next_absence({}, [], public, ["111", "222"])
        self.assertEqual(mark, [])
        self.assertEqual(state, {})
        self.assertEqual(restore, [])
        self.assertEqual(seen, ["111"])
        state, mark, restore, seen = sync.next_absence({}, ["111", "222"], public, ["111", "222"])
        self.assertEqual(mark, [])
        self.assertEqual(state, {"222": 1})
        self.assertEqual(seen, ["111", "222"])
        state2, mark2, restore2, seen2 = sync.next_absence(state, seen, public, ["111", "222"])
        self.assertEqual(mark2, ["222"])
        self.assertEqual(restore2, [])
        state3, mark3, restore3, seen3 = sync.next_absence(state2, seen2, {"111", "222"}, ["111", "222"])
        self.assertEqual(mark3, [])
        self.assertEqual(restore3, ["222"])
        self.assertEqual(state3, {})
        self.assertEqual(seen3, ["111", "222"])

    def test_canonical_page_fills_only_missing_fields(self):
        thin = {"article": "0890100043", "name": "Zaptivna masa", "description": "", "bullets": [], "pack_qty": None}
        full = sync.parse_public_product(PAGE)
        merged = sync.merge_public(thin, full)
        self.assertEqual(merged["name"], "Zaptivna masa")
        self.assertIn("vjetrobranskih", merged["description"])
        self.assertEqual(merged["pack_qty"], 12)
        self.assertEqual(sync.merge_public(full, {"article": "999", "name": "Iné", "description": "x", "pack_qty": 1}), full)

    def test_unavailable_payload_has_no_price_or_quantity(self):
        payload = sync.plan_unavailable(9)
        self.assertEqual(payload["stock_status"], "outofstock")
        self.assertNotIn("stock_quantity", payload)
        self.assertFalse(sync.PRICE_KEYS.intersection(payload))
        self.assertTrue(any(row["key"] == "_sg_wurth_public_absent" and row["value"] == "1" for row in payload["meta_data"]))
        back = sync.plan_return(9, "DE")
        self.assertEqual(back["stock_status"], "instock")
        self.assertNotIn("stock_quantity", back)
        self.assertFalse(sync.PRICE_KEYS.intersection(back))
        self.assertNotIn("status", back)

    def test_existing_slovak_name_stays_and_missing_description_is_translated(self):
        parsed = sync.parse_public_product(PAGE)
        self.assertEqual(parsed["article"], "0890100043")
        self.assertEqual(parsed["pack_qty"], 12)
        product = {
            "id": 4,
            "sku": "0890100043",
            "name": "Würth Tesniaca hmota na čelné sklo",
            "description": "",
            "images": [{"src": "https://smartgaraz.sk/wp-content/uploads/2026/real.jpg"}],
            "meta_data": [],
        }
        filled = sync.plan_content_fill(product, parsed, media_ok=True)
        self.assertNotIn("name", filled)
        self.assertIn("čelných skiel", filled["description"])
        self.assertNotIn("KM", filled["description"])
        self.assertNotIn("cijena", filled["description"].lower())
        self.assertIn("12", filled["description"])
        self.assertFalse(sync.PRICE_KEYS.intersection(filled))
        self.assertNotIn("stock_status", filled)
        self.assertNotIn("status", filled)
        self.assertTrue(any(row["key"] == "_sg_pack_qty" and row["value"] == "12" for row in filled["meta_data"]))

    def test_draft_has_no_price_and_albanian_description_is_not_published(self):
        draft = sync.plan_draft(
            "0890100043",
            {"name": "Zaptivna masa", "description": "Masa za zaptivanje.", "bullets": [], "pack_qty": 12},
            bucket="chemistry",
            source_url="https://eshop.wurth.ba/x",
        )
        self.assertEqual(draft["status"], "draft")
        self.assertNotIn("categories", draft)
        self.assertTrue(any(row["key"] == "_sg_category_status" and row["value"] == "unclear" for row in draft["meta_data"]))
        self.assertFalse(sync.PRICE_KEYS.intersection(draft))
        self.assertNotIn("stock_status", draft)
        self.assertNotIn("stock_quantity", draft)
        mapped = sync.plan_draft(
            "0890100044",
            {"name": "Nasadni kljuc", "description": "", "bullets": [], "pack_qty": None},
            bucket="tools",
            source_url="https://eshop.wurth.ba/x",
            category_path=["Náradie & Vybavenie", "Kľúče & račne"],
            category_id=2,
        )
        self.assertEqual(mapped["status"], "draft")
        self.assertEqual(mapped["categories"], [{"id": 2}])
        self.assertFalse(sync.PRICE_KEYS.intersection(mapped))
        self.assertTrue(draft["name"].startswith("Würth "))
        self.assertEqual(sync.translate_bcs_to_sk("Solucion për larje dhe motorit"), "")

    def test_public_absence_survives_the_availability_backfill(self):
        product = {"stock_status": "outofstock", "meta_data": []}
        meta = {
            "_supplier_source": "wurth",
            "_sg_origin": "DE",
            "_sg_avail_oos": "1",
            "_sg_wurth_public_absent": "1",
            "_sg_avail_src": sync.ABSENT_SRC,
        }
        self.assertEqual(av.classify_existing(product, meta)["avail"], "na")


if __name__ == "__main__":
    unittest.main()
