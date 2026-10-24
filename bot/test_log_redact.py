#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public-log redaction (bot/log_redact.py + public_ci/sitecustomize.py)."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from log_redact import MASK, redact  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_purchase_line(self):
        line = ("2026-10-24 03:31:02 | INFO | 💰 PRO-TEC Set DPF | nákup 12.34€ + doprava 3.50€ "
                "→ MOC 24.90€ (zisk 8.06€) | sklad=5 | dodanie=do 24 hodín")
        out = redact(line)
        for amount in ("12.34", "3.50", "24.90", "8.06"):
            self.assertNotIn(amount, out)
        self.assertIn("sklad=5", out)

    def test_margin_config(self):
        out = redact("⚙️  Cenotvorba: marža=30% doprava=3.5€ ending=.90")
        self.assertNotIn("30%", out)
        self.assertNotIn("3.5", out)

    def test_json_cost_keys(self):
        out = redact('SG_PRICE {"id": 77, "sku": "123", "before": 10.9, "after": 12.9, "net_cost": 7}')
        self.assertNotIn(": 7}", out)
        self.assertNotIn("10.9", out)
        self.assertIn(MASK, out)
        out2 = redact("cost_price=15 retail=20")
        self.assertNotIn("15", out2)

    def test_supplier_url_hidden_shop_url_kept(self):
        out = redact("- https://www.lkq.sk/Catalog/dacia-logan-filtre/39849642;39850140 ok")
        self.assertNotIn("dacia-logan", out)
        self.assertIn("www.lkq.sk", out)
        keep = "https://www.smartgaraz.sk/obchod/?x=1"
        self.assertIn(keep, redact(f"home {keep} 200"))

    def test_search_terms(self):
        out = redact('top query="brzdove platnicky octavia" count=4')
        self.assertNotIn("octavia", out)

    def test_workflow_commands_untouched(self):
        line = "::add-mask::sec12.34ret"
        self.assertEqual(redact(line), line)

    def test_plain_lines_untouched(self):
        line = "📦 Nájdených produktov: 120 [FAST+CHEAP+IN-STOCK] id #76175 sklad=3"
        self.assertEqual(redact(line), line)

    def test_sitecustomize_wraps_child_process(self):
        env = dict(os.environ, SG_PUBLIC_LOGS="1", PYTHONPATH=str(ROOT / "public_ci"))
        code = (
            "import logging,sys;logging.basicConfig(level=logging.INFO);"
            "logging.getLogger('x').info('nákup %.2f€ https://szekop.example.hu/p/123', 9.99);"
            "print('cost=11.5', flush=True)"
        )
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        both = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, both)
        self.assertNotIn("9.99", both)
        self.assertNotIn("11.5", both)
        self.assertNotIn("/p/123", both)

    def test_no_wrap_without_flag(self):
        env = {k: v for k, v in os.environ.items() if k != "SG_PUBLIC_LOGS"}
        env["PYTHONPATH"] = str(ROOT / "public_ci")
        r = subprocess.run([sys.executable, "-c", "print('cost=11.5')"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertIn("11.5", r.stdout)



class PublicReportSummaryTest(unittest.TestCase):
    def test_drops_search_sections_and_urls(self):
        import tempfile
        from public_report_summary import md_summary, json_summary
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / "r.md"
            md.write_text(
                "# Report\n## Homepage\n- HTTP: 200\n- link: https://www.lkq.sk/Catalog/x\n"
                "## Search (30 dní)\n### Zero results\n- brzdy octavia x5\n## Verdikt\n- ok: yes\n",
                encoding="utf-8",
            )
            text = "\n".join(md_summary(md))
            self.assertIn("HTTP: 200", text)
            self.assertIn("ok: yes", text)
            self.assertNotIn("octavia", text)
            self.assertNotIn("lkq.sk", text)
            js = Path(d) / "p.json"
            js.write_text('{"summary": {"ready": 3, "held": {"a": 1}}, "held_examples": [{"name": "X"}]}', encoding="utf-8")
            out = "\n".join(json_summary(js))
            self.assertIn("ready: 3", out)
            self.assertNotIn("X", out)

if __name__ == "__main__":
    unittest.main()
