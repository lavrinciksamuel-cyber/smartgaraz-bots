#!/usr/bin/env python3
"""Progress line, bar, and HQ count payload."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from progress_report import Progress, format_progress


class FormatTests(unittest.TestCase):
    def test_bar_and_percent_when_total_known(self):
        line = format_progress("lkq", 1200, 2857, 10, 2, 1)
        self.assertIn("bot=lkq", line)
        self.assertIn("42%", line)
        self.assertIn("1200/2857", line)
        self.assertIn("ok=10", line)
        self.assertIn("changed=2", line)
        self.assertIn("errors=1", line)
        self.assertIn("[", line)
        self.assertIn("#", line)
        self.assertNotIn("http", line)

    def test_count_only_when_total_unknown(self):
        line = format_progress("promote", 40, None, 3, 3, 0)
        self.assertEqual(line, "progress bot=promote done=40 ok=3 changed=3 errors=0")
        self.assertNotIn("%", line)
        self.assertNotIn("[", line)


class EmitTests(unittest.TestCase):
    def test_emits_every_200_and_posts_percent(self):
        posts = []
        now = {"t": 0.0}
        prog = Progress("wurth", total=400, every_n=200, interval_s=120, post=lambda k, c: posts.append((k, c)), clock=lambda: now["t"])
        prog.set(done=10, ok=10, changed=1, errors=0)
        self.assertEqual(posts, [])
        prog.set(done=200, ok=180, changed=20, errors=0)
        self.assertEqual(posts[0][0], "progress")
        self.assertEqual(posts[0][1]["percent"], 50)
        self.assertEqual(posts[0][1]["done"], 200)
        self.assertEqual(posts[0][1]["total"], 400)
        now["t"] = 10
        prog.set(done=201, ok=181, changed=20, errors=0)
        self.assertEqual(len(posts), 1)
        now["t"] = 200
        prog.set(done=210, ok=190, changed=20, errors=0)
        self.assertEqual(posts[-1][0], "progress")
        prog.finish()
        self.assertEqual(posts[-1][0], "done")
        self.assertEqual(posts[-1][1]["done"], 210)

    def test_unknown_total_sends_null_percent(self):
        posts = []
        prog = Progress("photo-integrity", every_n=1, interval_s=9999, post=lambda k, c: posts.append((k, c)))
        prog.set(done=1, ok=1)
        self.assertIsNone(posts[0][1]["total"])
        self.assertIsNone(posts[0][1]["percent"])


if __name__ == "__main__":
    unittest.main()
