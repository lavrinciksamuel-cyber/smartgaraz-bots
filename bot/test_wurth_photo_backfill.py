#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fotky Würth: zhoda podľa artiklu, placeholder sa zahodí, cena/sklad sa nezapisujú."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wurth_photo_backfill as photo
from media_licensing import is_wurth_media_url

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

BA_PAGE = """
<html>
<meta property="og:image" content="https://eshop.wurth.ba/media/logo/stores/1/wurth-logo-social.png"/>
<div data-product-code="07201711"></div>
<div data-product-code="0720&#x20;1711"></div>
<img src="https://eshop.wurth.ba/media/catalog/product/cache/9cfbf8d3c0998887947b1f707c80a6e2/0/7/07201711x.jpg"/>
<img src="https://eshop.wurth.ba/media/catalog/product/cache/9cfbf8d3c0998887947b1f707c80a6e2/0/7/072017110.jpg"/>
<img src="https://eshop.wurth.ba/media/resized/85x85/product/0/7/07201711x.jpg"/>
<img src="https://eshop.wurth.ba/media/catalog/product/cache/aaa/0/8/08925101.jpg"/>
</html>
"""

SIBLING_PAGE = """
<html>
<div data-product-code="08925104"></div>
<img src="https://eshop.wurth.ba/media/catalog/product/cache/hash/0/8/08925101.jpg"/>
<img src="https://eshop.wurth.ba/media/catalog/product/cache/hash/0/8/08925101_3.jpg"/>
</html>
"""

KOS_PAGE = """
<html>
<img src="https://eshop.wurth-kosova.com/media/6e/8d/2b/1783626722/nav-08931066-1-b8114a9b.jpg?ts=1783626722"/>
<img src="https://eshop.wurth-kosova.com/thumbnail/6e/8d/2b/1783626722/nav-08931066-1-b8114a9b_236x280.jpg"/>
<img src="https://eshop.wurth-kosova.com/media/bf/79/ab/1783626722/nav-08931066-2-b8114a9b.jpg"/>
<img src="https://eshop.wurth-kosova.com/media/c0/f9/1d/1790081026/nav-08901003-1-f91f0e4c.jpg"/>
<p>HHS-DRYLUBE</p>
</html>
"""


def _probe(url: str, *, status=200, ctype="image/jpeg", size=30000, magic=JPEG, final=None):
    return photo.ImageProbe(status, ctype, size, magic, final or url)


class FakeHttp:
    def __init__(self):
        self.pages = {}
        self.images = {}
        self.calls = []

    def get_text(self, url, timeout=30):
        self.calls.append(("text", url))
        return self.pages.get(url, "")

    def inspect_image(self, url):
        self.calls.append(("image", url))
        if url not in self.images:
            raise AssertionError(url)
        return self.images[url]


class PhotoBackfillTest(unittest.TestCase):
    def test_normalize_article_drops_spaces_and_entities(self):
        self.assertEqual(photo.normalize_article("0890 100043"), "0890100043")
        self.assertEqual(photo.normalize_article("0890&#x20;100043"), "0890100043")

    def test_ba_and_kosovo_urls_use_the_article_not_the_slug(self):
        url = "https://eshop.wurth.ba/0893%201066/suhi-sinteticki-vosak"
        self.assertEqual(photo.article_from_ba_product_url(url), "08931066")
        self.assertIsNone(photo.article_from_ba_product_url("https://eshop.wurth.ba/catalogsearch/result/?q=08931066"))
        kos = "https://eshop.wurth-kosova.com/HHS-DRYLUBE/08931066"
        self.assertEqual(photo.article_from_kosovo_url(kos), "08931066")
        self.assertIsNone(photo.article_from_kosovo_url("https://eshop.wurth-kosova.com/search?search=08931066"))

    def test_ba_page_confirmation_is_the_first_product_code(self):
        self.assertTrue(photo.ba_article_confirmed(BA_PAGE, "07201711"))
        self.assertFalse(photo.ba_article_confirmed(BA_PAGE, "08925101"))
        self.assertTrue(photo.ba_article_confirmed(SIBLING_PAGE, "08925104"))

    def test_ba_images_keep_this_article_and_drop_logo_thumb_and_neighbour(self):
        groups = photo.ordered_ba_candidates(BA_PAGE, "07201711")
        flat = [url for group in groups for url in group]
        self.assertIn("https://eshop.wurth.ba/media/catalog/product/0/7/07201711x.jpg", flat)
        self.assertFalse(any("logo" in url for url in flat))
        self.assertFalse(any("resized" in url for url in flat))
        self.assertFalse(any("072017110" in url for url in flat))
        self.assertFalse(any("08925101" in url for url in flat))
        self.assertEqual(photo.ordered_ba_candidates(SIBLING_PAGE, "08925104"), [])

    def test_kosovo_gallery_is_full_media_of_this_article_only(self):
        groups = photo.ordered_kosovo_candidates(KOS_PAGE, "08931066")
        firsts = [group[0] for group in groups]
        self.assertEqual(len(firsts), 2)
        self.assertTrue(all("nav-08931066-" in url for url in firsts))
        self.assertTrue(all("/thumbnail/" not in url for url in firsts))
        self.assertFalse(any("08901003" in url for url in firsts))
        self.assertTrue(firsts[0].endswith("nav-08931066-1-b8114a9b.jpg?ts=1783626722") or "nav-08931066-1" in firsts[0])

    def test_placeholder_png_is_not_a_photo_and_foreign_filename_is_rejected(self):
        sku = "046018"
        url = photo.ba_direct_url(sku)
        self.assertFalse(photo.image_is_real_photo(
            status=200, content_type="image/png", size=4511, magic=PNG, final_url=url, sku=sku,
        ))
        self.assertTrue(photo.image_is_real_photo(
            status=200, content_type="image/jpeg", size=304343, magic=JPEG, final_url=url, sku=sku,
        ))
        other = "https://eshop.wurth.ba/media/catalog/product/0/8/08925101.jpg"
        self.assertFalse(photo.image_is_real_photo(
            status=200, content_type="image/jpeg", size=20000, magic=JPEG, final_url=other, sku="08925104",
        ))
        self.assertFalse(photo.image_is_real_photo(
            status=200, content_type="image/jpeg", size=4000, magic=JPEG, final_url=url, sku=sku,
        ))

    def test_needs_photo_is_placeholder_or_missing_only(self):
        self.assertTrue(photo.needs_photo([]))
        self.assertTrue(photo.needs_photo(None))
        self.assertTrue(photo.needs_photo([
            {"src": "https://smartgaraz.sk/wp-content/uploads/smartgaraz-placeholders/wurth-046018.png"},
        ]))
        self.assertTrue(photo.needs_photo([
            {"src": "https://smartgaraz.sk/wp-content/uploads/smartgaraz-repair/x.jpg"},
        ]))
        self.assertFalse(photo.needs_photo([
            {"src": "https://smartgaraz.sk/wp-content/uploads/2026/10/08931066.jpg"},
        ]))
        self.assertFalse(photo.needs_photo([
            {"src": "https://smartgaraz.sk/wp-content/uploads/smartgaraz-placeholders/a.png"},
            {"src": "https://smartgaraz.sk/wp-content/uploads/2026/10/real.jpg"},
        ]))

    def test_woo_update_never_carries_price_stock_or_status(self):
        payload = photo.plan_image_update(
            15,
            [
                "https://eshop.wurth.ba/media/catalog/product/0/8/08931066.jpg",
                "https://eshop.wurth.ba/media/catalog/product/0/8/08931066_2.jpg",
            ],
            source=photo.SOURCE_BA,
            article="08931066",
            fetched_at="2026-10-23T00:00:00Z",
        )
        self.assertEqual(set(payload), {"id", "images", "meta_data"})
        self.assertEqual(payload["images"][0]["src"].rsplit("/", 1)[-1], "08931066.jpg")
        keys = {row["key"] for row in payload["meta_data"]}
        self.assertIn("_sg_image_article", keys)
        self.assertEqual(
            next(row["value"] for row in payload["meta_data"] if row["key"] == "_sg_image_article"),
            "08931066",
        )

    def test_page_match_uses_suffixed_file_and_ignores_sibling_photo(self):
        http = FakeHttp()
        page = "https://eshop.wurth.ba/0720%201711/sofitna"
        http.pages[page] = BA_PAGE
        good = "https://eshop.wurth.ba/media/catalog/product/0/7/07201711x.jpg"
        http.images[good] = _probe(good, size=26193)
        found = photo.match_article("07201711", ba_pages={"07201711": page}, kos_pages={}, http=http)
        self.assertIsNotNone(found)
        self.assertEqual(found.verified_by, "ba_page")
        self.assertEqual(found.urls, [good])
        self.assertEqual(found.source, photo.SOURCE_BA)

        http2 = FakeHttp()
        sibling = "https://eshop.wurth.ba/0892%205104/silikon"
        http2.pages[sibling] = SIBLING_PAGE
        direct = photo.ba_direct_url("08925104")
        http2.images[direct] = _probe(direct, ctype="image/png", size=4511, magic=PNG)
        missed = photo.match_article("08925104", ba_pages={"08925104": sibling}, kos_pages={}, http=http2)
        self.assertIsNone(missed)

    def test_direct_jpeg_counts_and_placeholder_does_not(self):
        http = FakeHttp()
        direct = photo.ba_direct_url("0893423")
        http.images[direct] = _probe(direct, size=38471)
        found = photo.match_article("0893423", ba_pages={}, kos_pages={}, http=http)
        self.assertEqual(found.verified_by, "ba_media_filename")
        self.assertEqual(found.urls, [direct])

        http.images[photo.ba_direct_url("046018")] = _probe(
            photo.ba_direct_url("046018"), ctype="image/png", size=4511, magic=PNG,
        )
        self.assertIsNone(photo.match_article("046018", ba_pages={}, kos_pages={}, http=http))

    def test_kosovo_is_fallback_when_bosnia_has_no_photo(self):
        sku = "08931066"
        page = "https://eshop.wurth-kosova.com/HHS-DRYLUBE/08931066"
        http = FakeHttp()
        http.pages[page] = KOS_PAGE
        http.images[photo.ba_direct_url(sku)] = _probe(
            photo.ba_direct_url(sku), ctype="image/png", size=4511, magic=PNG,
        )
        for group in photo.ordered_kosovo_candidates(KOS_PAGE, sku):
            http.images[group[0]] = _probe(group[0], size=80000)
        found = photo.match_article(sku, ba_pages={}, kos_pages={sku: page}, http=http)
        self.assertEqual(found.source, photo.SOURCE_KOS)
        self.assertEqual(found.verified_by, "kosovo_page")
        self.assertEqual(len(found.urls), 2)
        self.assertTrue(all("08931066" in url for url in found.urls))

    def test_sitemap_index_keeps_article_urls_only(self):
        xml = """
        <urlset>
          <url><loc>https://eshop.wurth.ba/0893%201066/suhi-vosak</loc></url>
          <url><loc>https://eshop.wurth.ba/zastita-na-radu</loc></url>
          <url><loc>https://eshop.wurth.ba/catalogsearch/result/?q=08931066</loc></url>
        </urlset>
        """
        indexed = photo.index_from_sitemap(xml, photo.article_from_ba_product_url)
        self.assertEqual(list(indexed), ["08931066"])

    def test_summary_match_rate(self):
        rows = [
            photo.PhotoMatch("1", photo.SOURCE_BA, ["https://eshop.wurth.ba/a.jpg"], "ba_page"),
            None,
            photo.PhotoMatch("3", photo.SOURCE_KOS, ["https://eshop.wurth-kosova.com/b.jpg"], "kosovo_page"),
            None,
        ]
        report = photo.summarize(rows, 4)
        self.assertEqual(report["matched"], 2)
        self.assertEqual(report["match_rate"], 0.5)
        self.assertEqual(report["by_source"][photo.SOURCE_BA], 1)

    def test_kosovo_host_is_wurth_media(self):
        self.assertTrue(is_wurth_media_url("https://eshop.wurth-kosova.com/media/a/nav-08931066-1.jpg"))
        self.assertTrue(is_wurth_media_url("https://eshop.wurth.ba/media/catalog/product/0/8/08931066.jpg"))

    def test_is_wurth_product_uses_supplier_meta_not_the_title(self):
        self.assertTrue(photo.is_wurth_product({
            "name": "Niečo iné",
            "meta_data": [{"key": "_supplier_source", "value": "wurth"}],
        }))
        self.assertFalse(photo.is_wurth_product({
            "name": "Würth hadica",
            "meta_data": [{"key": "_supplier_source", "value": "lkq"}],
        }))


if __name__ == "__main__":
    unittest.main()
