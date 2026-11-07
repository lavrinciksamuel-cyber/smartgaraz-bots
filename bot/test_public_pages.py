#!/usr/bin/env python3
"""Public page reads: robots, sitemap, JSON-LD. A block stops."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_pages import (
    classify_http,
    discover_public,
    product_links,
    products_from_html,
    robots_allows,
    robots_sitemaps,
    sitemap_urls,
)


ROBOTS = """
User-agent: *
Disallow: /Search/
Disallow: /Account/
Allow: /product/
Sitemap: https://shop.example/sitemap.xml
"""

PAGE = """
<html><head>
<meta property="og:title" content="Pad" />
<script type="application/ld+json">
{"@type":"Product","name":"Brake pad","sku":"BP1","image":"https://shop.example/a.jpg","url":"https://shop.example/product/bp1"}
</script>
</head>
<body>
<a href="/Search/secret">no</a>
<a href="/product/bp1">yes</a>
<a href="https://other.example/product/x">no</a>
</body></html>
"""


class ClassifyTests(unittest.TestCase):
    def test_block_statuses_and_challenge(self):
        self.assertEqual(classify_http(403, "no"), "blocked")
        self.assertEqual(classify_http(429, ""), "blocked")
        self.assertEqual(classify_http(468, ""), "blocked")
        self.assertEqual(classify_http(200, "<html>Just a moment</html>"), "blocked")
        self.assertEqual(classify_http(404, "missing"), "missing")
        self.assertEqual(classify_http(200, "<html>ok</html>"), "ok")

    def test_robots_and_sitemap(self):
        self.assertEqual(robots_sitemaps(ROBOTS), ["https://shop.example/sitemap.xml"])
        self.assertFalse(robots_allows(ROBOTS, "/Search/q"))
        self.assertTrue(robots_allows(ROBOTS, "/product/bp1"))
        xml = "<urlset><url><loc>https://shop.example/product/1</loc></url></urlset>"
        self.assertEqual(sitemap_urls(xml), ["https://shop.example/product/1"])

    def test_structured_data_and_links(self):
        rows = products_from_html(PAGE)
        self.assertEqual(rows[0]["sku"], "BP1")
        self.assertEqual(rows[0]["name"], "Brake pad")
        links = product_links(PAGE, "https://shop.example/", ROBOTS)
        self.assertEqual(links, ["https://shop.example/product/bp1"])


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        status, text = self.pages.get(url, (404, ""))
        return _Resp(status, text)


class DiscoverTests(unittest.TestCase):
    def test_block_on_robots_does_not_fetch_more(self):
        session = _Session({"https://shop.example/robots.txt": (403, "no")})
        found = discover_public(session, "https://shop.example", limit=5)
        self.assertEqual(found.status, "blocked")
        self.assertEqual(found.urls, [])
        self.assertEqual(session.calls, ["https://shop.example/robots.txt"])

    def test_sitemap_urls_when_robots_allows_them(self):
        xml = "<urlset><url><loc>https://shop.example/product/1</loc><loc>https://shop.example/Search/secret</loc></url></urlset>"
        session = _Session({
            "https://shop.example/robots.txt": (200, ROBOTS),
            "https://shop.example/sitemap.xml": (200, xml),
        })
        found = discover_public(session, "https://shop.example", limit=5)
        self.assertEqual(found.status, "ok")
        self.assertEqual(found.urls, ["https://shop.example/product/1"])


if __name__ == "__main__":
    unittest.main()
