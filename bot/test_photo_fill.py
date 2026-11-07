#!/usr/bin/env python3
"""Queue and gallery decisions for per-supplier photo fill."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from photo_fill import (
    empty_state,
    featured_gallery,
    lkq_image_url,
    mark,
    needs_fill,
    next_ids,
    product_supplier,
    sync_candidates,
    szekop_image_url,
)


class QueueTests(unittest.TestCase):
    def test_sync_keeps_order_and_appends_new(self):
        state = empty_state()
        state["todo"] = [3, 1]
        state["done"] = [9]
        state["failed"] = {"4": {"reason": "no_image", "last_checked": "2026-11-01T00:00:00Z"}}
        merged = sync_candidates(state, [3, 1, 4, 8, 9])
        self.assertEqual(merged["todo"], [3, 1, 8, 9])
        self.assertEqual(merged["done"], [])
        self.assertIn("4", merged["failed"])

    def test_next_ids_continues_todo_then_older_failures(self):
        state = {
            "todo": [5, 6, 7],
            "done": [],
            "failed": {
                "2": {"reason": "no_public", "last_checked": "2026-11-01T00:00:00Z"},
                "3": {"reason": "integrity", "last_checked": "2026-11-07T01:00:00Z"},
            },
            "last_checked": "",
        }
        self.assertEqual(next_ids(state, 2, "2026-11-07"), [5, 6])
        self.assertEqual(next_ids(state, 4, "2026-11-07"), [5, 6, 7, 2])

    def test_mark_moves_between_lists_and_sets_last_checked(self):
        state = sync_candidates(empty_state(), [1, 2])
        mark(state, 1, "done", "", "2026-11-07T03:00:00Z")
        mark(state, 2, "failed", "integrity", "2026-11-07T03:01:00Z")
        self.assertEqual(state["todo"], [])
        self.assertEqual(state["done"], [1])
        self.assertEqual(state["failed"]["2"]["reason"], "integrity")
        self.assertEqual(state["last_checked"], "2026-11-07T03:01:00Z")
        self.assertEqual(state["failed"]["2"]["last_checked"], "2026-11-07T03:01:00Z")


class FillDecisionTests(unittest.TestCase):
    def test_needs_fill_only_for_empty_or_named_placeholder(self):
        self.assertTrue(needs_fill([], 9))
        self.assertTrue(needs_fill([{"id": 9, "src": "https://cdn.example/a.jpg"}], 9))
        self.assertTrue(needs_fill(
            [{"id": 4, "name": "Foto sa pripravuje", "src": "https://cdn.example/intro.jpg"}],
            None,
        ))
        self.assertFalse(needs_fill([{"id": 4, "name": "pad", "src": "https://cdn.example/pad.jpg"}], 9))

    def test_gallery_features_new_image_and_drops_placeholder_only(self):
        gallery = featured_gallery(
            [{"id": 9, "name": "Foto sa pripravuje", "src": "https://cdn.example/intro.jpg"}, {"id": 4}],
            new_id=15,
            placeholder_id=9,
        )
        self.assertEqual(gallery, [{"id": 15}, {"id": 4}])
        self.assertNotIn(9, [row.get("id") for row in gallery])

    def test_missing_ref_does_not_fetch(self):
        self.assertEqual(szekop_image_url(""), ("", "no_ref"))
        self.assertEqual(lkq_image_url(""), ("", "no_ref"))

    def test_supplier_meta(self):
        self.assertEqual(product_supplier("LKQ"), "lkq")
        self.assertEqual(product_supplier("szekop"), "szekop")
        self.assertEqual(product_supplier("Würth"), "wurth")
        self.assertEqual(product_supplier("other"), "")


if __name__ == "__main__":
    unittest.main()
