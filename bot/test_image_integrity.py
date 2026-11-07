#!/usr/bin/env python3
"""Detector: truncated JPEG, flat gray fill, and a good image."""
from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image

import image_integrity as integ
import photo_integrity as photo


def _jpeg(size, paint) -> bytes:
    img = Image.new("RGB", size, (255, 255, 255))
    px = img.load()
    paint(px, size[0], size[1])
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95, subsampling=0)
    return buf.getvalue()


def _paint_good(px, w, h):
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 3) % 256, (y * 5) % 200, (x + y) % 180)


def _paint_gray_bottom(px, w, h):
    split = h // 2
    for y in range(split):
        for x in range(w):
            px[x, y] = ((x * 7) % 220, (y * 3) % 180, 30 + (x % 40))
    for y in range(split, h):
        for x in range(w):
            px[x, y] = (128, 128, 128)


def _paint_white_bottom(px, w, h):
    for y in range(h // 2):
        for x in range(w):
            px[x, y] = ((x * 5) % 180, 40, (y * 2) % 120)
    for y in range(h // 2, h):
        for x in range(w):
            px[x, y] = (255, 255, 255)


def _paint_light_gray_bottom(px, w, h):
    for y in range(h // 2):
        for x in range(w):
            px[x, y] = (20, (x * 4) % 200, 60)
    for y in range(h // 2, h):
        for x in range(w):
            px[x, y] = (200, 200, 200)


def _paint_thin_gray(px, w, h):
    _paint_good(px, w, h)
    for y in range(h - 8, h):
        for x in range(w):
            px[x, y] = (128, 128, 128)


class InspectTests(unittest.TestCase):
    def test_good_jpeg(self):
        data = _jpeg((96, 96), _paint_good)
        self.assertTrue(data.endswith(b"\xff\xd9"))
        self.assertIsNone(integ.inspect_bytes(data, declared_length=len(data)))

    def test_truncated_jpeg_missing_eoi(self):
        data = _jpeg((96, 96), _paint_good)
        cut = data[:-12]
        self.assertFalse(cut.endswith(b"\xff\xd9"))
        self.assertNotIn(b"\xff\xd9", cut[-32:])
        self.assertEqual(integ.inspect_bytes(cut), "jpeg_eoi")

    def test_decoder_error(self):
        garbage = b"\xff\xd8" + b"\x00" * 80 + b"\xff\xd9"
        self.assertEqual(integ.inspect_bytes(garbage), "decode")

    def test_gray_fill(self):
        data = _jpeg((80, 120), _paint_gray_bottom)
        self.assertEqual(integ.inspect_bytes(data), "gray_fill")

    def test_white_and_light_gray_background_not_flagged(self):
        white = _jpeg((80, 120), _paint_white_bottom)
        light = _jpeg((80, 120), _paint_light_gray_bottom)
        thin = _jpeg((80, 120), _paint_thin_gray)
        self.assertIsNone(integ.inspect_bytes(white))
        self.assertIsNone(integ.inspect_bytes(light))
        self.assertIsNone(integ.inspect_bytes(thin))

    def test_content_length_mismatch(self):
        data = _jpeg((64, 64), _paint_good)
        self.assertEqual(integ.inspect_bytes(data, declared_length=len(data) + 10), "content_length")

    def test_png_missing_iend(self):
        img = Image.new("RGB", (32, 32), (10, 20, 30))
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        self.assertIsNone(integ.inspect_bytes(data))
        chopped = data[:-16]
        self.assertEqual(integ.inspect_bytes(chopped), "png_iend")


class _Resp:
    def __init__(self, status, body, length=None, encoding=""):
        self.status_code = status
        self.content = body
        self.headers = {}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        if encoding:
            self.headers["Content-Encoding"] = encoding


class _Seq:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None, verify=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FetchTests(unittest.TestCase):
    def test_retry_once_then_accept(self):
        good = _jpeg((64, 64), _paint_good)
        cut = good[:-12]
        session = _Seq([
            _Resp(200, cut, length=len(good)),
            _Resp(200, good, length=len(good)),
        ])
        data, reason = integ.fetch_verified(session, "https://example.test/a.jpg", pause=0, verify=True)
        self.assertEqual(session.calls, 2)
        self.assertEqual(reason, "")
        self.assertEqual(data, good)

    def test_stop_on_403_without_retry(self):
        session = _Seq([_Resp(403, b"no")])
        data, reason = integ.fetch_verified(session, "https://example.test/a.jpg", pause=0, verify=True)
        self.assertIsNone(data)
        self.assertEqual(reason, "http_403")
        self.assertEqual(session.calls, 1)

    def test_gray_skipped_after_one_retry(self):
        bad = _jpeg((80, 120), _paint_gray_bottom)
        session = _Seq([
            _Resp(200, bad, length=len(bad)),
            _Resp(200, bad, length=len(bad)),
        ])
        _data, reason = integ.fetch_verified(session, "https://example.test/a.jpg", pause=0, verify=True)
        self.assertEqual(reason, "gray_fill")
        self.assertEqual(session.calls, 2)


class SiteTests(unittest.TestCase):
    def test_same_site_ignores_www_only(self):
        self.assertTrue(photo.same_site("https://www.example.test/a.jpg", "https://example.test"))
        self.assertTrue(photo.same_site("https://example.test/a.jpg", "https://www.example.test/shop"))
        self.assertFalse(photo.same_site("https://cdn.example.test/a.jpg", "https://example.test"))
        self.assertFalse(photo.same_site("https://example.test.evil.test/a.jpg", "https://example.test"))


if __name__ == "__main__":
    unittest.main()
