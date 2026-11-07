#!/usr/bin/env python3
"""Shop 403 logs: status and body kind only."""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_retry import note_wc_forbidden


class _Resp:
    def __init__(self, status, url, headers, body):
        self.status_code = status
        self.url = url
        self.headers = headers
        self.text = body

    def json(self):
        return json.loads(self.text)


class Shop403Test(unittest.TestCase):
    def _line(self, response) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            note_wc_forbidden(response)
        return buf.getvalue().strip()

    def test_html_challenge(self):
        line = self._line(_Resp(
            403,
            "https://shop.example/wp-json/wc/v3/products",
            {"cf-mitigated": "challenge", "server": "cloudflare", "content-type": "text/html; charset=UTF-8"},
            "<!DOCTYPE html><title>Just a moment</title>",
        ))
        self.assertIn("status=403", line)
        self.assertIn("cf-mitigated=challenge", line)
        self.assertIn("server=cloudflare", line)
        self.assertIn("content-type=text/html", line)
        self.assertIn("body=html-challenge", line)
        self.assertNotIn("shop.example", line)
        self.assertNotIn("wp-json", line)

    def test_woocommerce_code(self):
        line = self._line(_Resp(
            403,
            "https://shop.example/wp-json/wc/v3/products",
            {"server": "cloudflare", "content-type": "application/json"},
            '{"code":"woocommerce_rest_cannot_view","message":"sorry","data":{"status":403}}',
        ))
        self.assertIn("cf-mitigated=-", line)
        self.assertIn("body=woocommerce_rest_cannot_view", line)
        self.assertNotIn("sorry", line)
        self.assertNotIn("shop.example", line)

    def test_ignores_other_statuses_and_hosts(self):
        self.assertEqual(self._line(_Resp(401, "https://shop.example/wp-json/wc/v3/products", {}, "{}")), "")
        self.assertEqual(self._line(_Resp(403, "https://other.example/page", {"server": "cloudflare"}, "<html>")), "")


if __name__ == "__main__":
    unittest.main()
