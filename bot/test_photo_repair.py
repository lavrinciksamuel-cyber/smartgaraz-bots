#!/usr/bin/env python3
"""Decision logic for removing a broken product image."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from photo_repair import (
    DELETE_CAP,
    accept_attachment,
    classify_for_delete,
    pick_media_id,
    placeholder_id_from_setting,
    plan_gallery,
    ratio_exceeded,
    within_cap,
)


class ClassifyTests(unittest.TestCase):
    def test_high_confidence_can_delete(self):
        for reason in ("jpeg_eoi", "png_iend", "decode", "gray_fill"):
            self.assertEqual(classify_for_delete(reason), "delete", reason)

    def test_block_and_timeout_never_delete(self):
        self.assertEqual(classify_for_delete("http_403"), "stop")
        self.assertEqual(classify_for_delete("http_429"), "stop")
        self.assertEqual(classify_for_delete("ReadTimeout"), "skip")
        self.assertEqual(classify_for_delete("ConnectTimeout"), "skip")
        self.assertEqual(classify_for_delete("Timeout"), "skip")

    def test_weak_signals_skip(self):
        for reason in ("", "content_length", "empty", "http_404", "http_500", None):
            self.assertEqual(classify_for_delete(reason), "skip", reason)


class RatioTests(unittest.TestCase):
    def test_threshold_is_strictly_above_twenty_percent(self):
        self.assertFalse(ratio_exceeded(10, 2))
        self.assertTrue(ratio_exceeded(10, 3))
        self.assertFalse(ratio_exceeded(0, 5))
        self.assertTrue(ratio_exceeded(1, 1))


class GalleryTests(unittest.TestCase):
    def test_first_good_image_becomes_featured(self):
        plan = plan_gallery([{"id": 1}, {"id": 2}, {"id": 3}], {1, 3}, 9)
        self.assertEqual(plan["images"], [{"id": 2}])
        self.assertEqual(plan["delete_ids"], [1, 3])
        self.assertFalse(plan["placeholder_set"])
        self.assertFalse(plan["cleared"])

    def test_placeholder_when_nothing_good_remains(self):
        plan = plan_gallery([{"id": 4}, {"id": 5}], {4, 5}, 9)
        self.assertEqual(plan["images"], [{"id": 9}])
        self.assertEqual(plan["delete_ids"], [4, 5])
        self.assertTrue(plan["placeholder_set"])
        self.assertNotIn(9, plan["delete_ids"])

    def test_clear_when_placeholder_unknown(self):
        plan = plan_gallery([{"id": 4}], {4}, None)
        self.assertEqual(plan["images"], [])
        self.assertTrue(plan["cleared"])
        self.assertFalse(plan["placeholder_set"])

    def test_placeholder_attachment_is_never_deleted(self):
        plan = plan_gallery([{"id": 9}, {"id": 4}], {9, 4}, 9)
        self.assertNotIn(9, plan["delete_ids"])
        self.assertEqual(plan["images"], [{"id": 9}])
        self.assertFalse(plan["placeholder_set"])

    def test_cap_stops_before_the_next_product(self):
        self.assertTrue(within_cap(2, 198))
        self.assertFalse(within_cap(2, 199, DELETE_CAP))
        self.assertFalse(within_cap(0, 0))


class PlaceholderPickTests(unittest.TestCase):
    def test_setting_id(self):
        self.assertEqual(placeholder_id_from_setting({"value": "42"}), 42)
        self.assertIsNone(placeholder_id_from_setting({"value": "0"}))
        self.assertIsNone(placeholder_id_from_setting({}))

    def test_rejects_generic_file_and_picks_named_media(self):
        self.assertFalse(accept_attachment(
            src="https://cdn.example/woocommerce-placeholder.webp",
            title="Placeholder",
            slug="woocommerce-placeholder",
            from_setting=True,
        ))
        self.assertTrue(accept_attachment(
            src="https://cdn.example/uploads/intro.jpg",
            title="ignored",
            slug="intro",
            from_setting=True,
        ))
        rows = [
            {"id": 7, "slug": "woocommerce-placeholder", "source_url": "https://cdn.example/woocommerce-placeholder.webp",
             "title": {"rendered": "Foto sa pripravuje"}},
            {"id": 15, "slug": "foto-sa-pripravuje-2", "source_url": "https://cdn.example/a.jpg",
             "title": {"rendered": "Foto sa pripravuje"}},
            {"id": 11, "slug": "other", "source_url": "https://cdn.example/b.jpg",
             "title": {"rendered": "Foto sa pripravuje"}},
        ]
        self.assertEqual(pick_media_id(rows), 11)

    def test_slug_match(self):
        rows = [{"id": 3, "slug": "foto-sa-pripravuje", "source_url": "https://cdn.example/a.jpg", "title": "x"}]
        self.assertEqual(pick_media_id(rows), 3)


if __name__ == "__main__":
    unittest.main()
