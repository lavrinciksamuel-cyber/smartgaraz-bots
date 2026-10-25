"""Testy skenera:  cd security && python -m unittest test_monitor"""
import hashlib
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor  # noqa: E402

VECTOR_SECRET = "test-secret-0123456789abcdef0123456789"  # gitleaks:allow (testovací vektor, nie tajomstvo)


class Signing(unittest.TestCase):
    def test_canon_format(self):
        c = monitor.canon("post", "/api/sec/scanner/report", "1800000000", "a" * 32, b'{"x":1}')
        self.assertEqual(c, "SGSEC1\nPOST\n/api/sec/scanner/report\n1800000000\n" + "a" * 32 + "\n" + hashlib.sha256(b'{"x":1}').hexdigest())

    def test_fixed_vector_matches_php(self):
        # rovnaký vektor je v smartgaraz-web/hq/tests/sec_core_test.php
        c = monitor.canon("post", "/api/sec/scanner/report", "1800000000", "a" * 32, b'{"x":1}')
        self.assertEqual(monitor.sign(VECTOR_SECRET, c), "69f8660c6bc4394d3066332ead00773d405e60ed08eda84cf71cfd322404da9b")

    def test_signed_headers(self):
        h = monitor.signed_headers("s" * 64, "GET", "/api/sec/scanner/sites", b"")
        self.assertEqual(h["X-SG-Key"], "scanner")
        self.assertRegex(h["X-SG-Nonce"], r"^[a-f0-9]{32}$")
        self.assertLess(abs(int(h["X-SG-Ts"]) - time.time()), 5)
        self.assertEqual(h["X-SG-Sig"], monitor.sign("s" * 64, monitor.canon("GET", "/api/sec/scanner/sites", h["X-SG-Ts"], h["X-SG-Nonce"], b"")))


class Content(unittest.TestCase):
    HTML = b"""<html><head><title> Moja  stranka </title><meta name="generator" content="WordPress 6.5.2">
    <script src="https://www.googletagmanager.com/gtag.js"></script><script src="/wp-includes/x.js"></script>
    <script src="https://cdn.smartgaraz.sk/a.js"></script></head><body>Vitajte SmartGaraz
    <iframe src="https://evil.example/x"></iframe><a href="https://partner.sk/">p</a><a href="/kontakt">k</a>
    <p>best casino bonus</p></body></html>"""

    def test_analyze(self):
        c, gen = monitor.analyze_content(self.HTML, "https://www.smartgaraz.sk/", {"www.smartgaraz.sk", "smartgaraz.sk"}, "smartgaraz")
        self.assertEqual(c["title"], "Moja stranka")
        self.assertEqual(c["script_domains"], ["www.googletagmanager.com"])  # vlastné domény a subdomény sa ignorujú
        self.assertEqual(c["iframe_domains"], ["evil.example"])
        self.assertEqual(c["link_domains"], ["partner.sk"])
        self.assertEqual(c["spam_hits"], ["casino"])
        self.assertTrue(c["expect_found"])
        self.assertEqual(gen, "WordPress 6.5.2")
        self.assertTrue(monitor.is_wp(self.HTML, gen))

    def test_hash_stable_across_nonces(self):
        a, _ = monitor.analyze_content(b"<p>x</p><script src='/a.js?ver=123'></script>_wpnonce=abc123", "https://a.sk/", {"a.sk"})
        b, _ = monitor.analyze_content(b"<p>x</p><script src='/a.js?ver=456'></script>_wpnonce=def456", "https://a.sk/", {"a.sk"})
        self.assertEqual(a["hash"], b["hash"])

    def test_headers(self):
        r = monitor.Resp(200, {"strict-transport-security": "max-age=1", "x-content-type-options": "nosniff",
                               "content-security-policy": "frame-ancestors 'self'", "x-powered-by": "PHP/8.1"})
        h = monitor.sec_headers(r)
        self.assertTrue(h["hsts"] and h["xcto"] and h["csp_frame_ancestors"])
        self.assertFalse(h["xfo"] or h["referrer"])
        self.assertEqual(h["x_powered_by"], "PHP/8.1")


class Exposed(unittest.TestCase):
    def t(self, path):
        return next(x[2] for x in monitor.EXPOSED if x[0] == path)

    def test_validators_reject_soft_404(self):
        html = b"<!doctype html><html><head><title>Stranka nenajdena</title></head><body>404</body></html>"
        for path, _why, test in monitor.EXPOSED:
            self.assertFalse(test(html, "text/html"), path)

    def test_validators_accept_real(self):
        self.assertTrue(self.t("/.env")(b"APP_KEY=base64:xyz\nDB_PASSWORD=x\n", "text/plain"))
        self.assertTrue(self.t("/.git/config")(b"[core]\n\trepositoryformatversion = 0\n", ""))
        self.assertTrue(self.t("/wp-config.php.bak")(b"<?php define('DB_NAME','x');", ""))
        self.assertTrue(self.t("/wp-content/debug.log")(b"[01-Jan-2026] PHP Warning: x", ""))
        self.assertTrue(self.t("/.DS_Store")(b"\x00\x00\x00\x01Bud1\x00", ""))


class Blacklist(unittest.TestCase):
    def test_classify(self):
        _z, _t, li, bl = monitor.DOMAIN_BL["spamhaus-dbl"]
        self.assertEqual(monitor.bl_classify([], li, bl), "clean")
        self.assertEqual(monitor.bl_classify(["127.0.1.2"], li, bl), "listed")
        self.assertEqual(monitor.bl_classify(["127.255.255.254"], li, bl), "unknown")  # verejný resolver zablokovaný
        self.assertEqual(monitor.bl_classify(["198.18.0.1"], li, bl), "unknown")       # falošný/sinkhole resolver
        _z, _t, li, bl = monitor.DOMAIN_BL["surbl"]
        self.assertEqual(monitor.bl_classify(["127.0.0.1"], li, bl), "unknown")
        self.assertEqual(monitor.bl_classify(["127.0.0.8"], li, bl), "listed")


class Misc(unittest.TestCase):
    def test_default_sites(self):
        sites, _o, src = monitor.load_sites("https://invalid.invalid", "")
        self.assertEqual(src, "default")
        slugs = {s["slug"] for s in sites}
        self.assertIn("smartgaraz", slugs)
        for s in sites:
            self.assertTrue(s["url"].startswith("https://"))

    def test_full_run(self):
        os.environ.pop("SEC_FULL", None)
        os.environ.pop("GITHUB_EVENT_NAME", None)
        self.assertTrue(monitor.is_full_run(time.struct_time((2026, 1, 1, 10, 7, 0, 0, 1, 0))))
        self.assertFalse(monitor.is_full_run(time.struct_time((2026, 1, 1, 10, 22, 0, 0, 1, 0))))

    def test_no_secret_skips(self):
        os.environ.pop("SEC_SCANNER_SECRET", None)
        os.environ.pop("SEC_DRY", None)
        self.assertEqual(monitor.main(), 0)


if __name__ == "__main__":
    unittest.main()
