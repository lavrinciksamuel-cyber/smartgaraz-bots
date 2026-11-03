#!/usr/bin/env python3
"""Unit tests for pacing, run outcome, and host checks. No network."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from catalog_licensing import CatalogLicensingError, assert_allowed_catalog_url, is_wurth_sk_shop  # noqa: E402
from catalog_shard import prepare_szekop_phase1  # noqa: E402
from supplier_pace import (  # noqa: E402
    DailyCapReached,
    Guard,
    decide_outcome,
    is_challenge,
    live_fetch_skipped,
    restrict_price_stock_payload,
)
from suppliers.lkq import lkq_html_logged_in  # noqa: E402
from watchdog import evaluate_stuck_job  # noqa: E402


class LkqLoginHtmlTest(unittest.TestCase):
    def test_logout_link_is_logged_in(self):
        self.assertTrue(lkq_html_logged_in("<a href='/account/logoff'>Odhlásiť</a>"))
        self.assertTrue(lkq_html_logged_in("ODHLAS"))

    def test_anonymous_shell_is_not_logged_in(self):
        html = "<html><title>AutoKelly LKQ</title><div id='app'>katalog</div></html>"
        self.assertFalse(lkq_html_logged_in(html))
        self.assertFalse(lkq_html_logged_in('<input name="UserNameM">'))
        self.assertFalse(lkq_html_logged_in(""))


class ChallengeTest(unittest.TestCase):
    def test_status_and_marker(self):
        self.assertTrue(is_challenge(403, ""))
        self.assertTrue(is_challenge(429, "ok"))
        self.assertTrue(is_challenge(401, ""))
        self.assertTrue(is_challenge(200, "please wait /cdn-cgi/challenge-platform"))
        self.assertFalse(is_challenge(200, "<h1>Brzdový kotúč LKQ</h1>"))
        self.assertFalse(is_challenge(200, "návod spomína slovo captcha v texte výrobku"))


class OutcomeTest(unittest.TestCase):
    def test_empty_and_login_fail(self):
        self.assertEqual(decide_outcome(login_ok=False, checked=3), (False, "login"))
        self.assertEqual(decide_outcome(blocked=True, seen=5), (False, "blocked"))
        self.assertEqual(decide_outcome(paused=True), (False, "paused"))
        self.assertEqual(decide_outcome(), (False, "empty"))

    def test_real_work_is_ok_even_if_nothing_changed(self):
        self.assertEqual(decide_outcome(checked=4, updated=0, login_ok=True)[0], True)
        self.assertEqual(decide_outcome(seen=8, new=0)[0], True)
        self.assertEqual(decide_outcome(exhausted=True)[0], True)
        self.assertEqual(decide_outcome(cap=True, login_ok=None)[0], True)
        self.assertEqual(decide_outcome(cap=True, login_ok=False)[0], False)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SG_POLITE_MIN"] = "0"
        os.environ["SG_POLITE_MAX"] = "0"
        os.environ["SG_DAILY_CAP"] = "2"
        os.environ["SG_RUN_CAP"] = "2"
        os.environ["SG_GUARD_FAILS"] = "2"
        os.environ["GITHUB_RUN_ID"] = "test-run"

    def test_two_failures_pause_until_resume(self):
        g = Guard("lkq", Path(self.tmp.name))
        g.note_failure("login")
        self.assertFalse(g.snapshot()["paused"])
        g.note_failure("blocked")
        state = g.begin(resume=False)
        self.assertTrue(state["paused"])
        self.assertEqual(state["failures"], 2)
        again = g.begin(resume=False)
        self.assertTrue(again["paused"])
        self.assertEqual(again["failures"], 2)
        cleared = g.begin(resume=True)
        self.assertFalse(cleared["paused"])
        self.assertEqual(cleared["failures"], 0)

    def test_daily_cap_stops_before_the_extra_request(self):
        g = Guard("szekop-b2b", Path(self.tmp.name))
        g.before_request()
        g.before_request()
        with self.assertRaises(DailyCapReached):
            g.before_request()
        self.assertEqual(g.snapshot()["requests"], 2)

    def test_success_clears_the_streak(self):
        g = Guard("szekop-public", Path(self.tmp.name))
        g.note_failure("blocked")
        g.note_success()
        self.assertEqual(g.snapshot()["failures"], 0)
        self.assertFalse(g.snapshot()["paused"])


class PayloadTest(unittest.TestCase):
    def test_fitment_and_copy_are_dropped(self):
        payload = restrict_price_stock_payload({
            "regular_price": "12.90",
            "name": "nesmie sa zmeniť",
            "description": "ani popis",
            "images": [{"src": "http://example.test/a.jpg"}],
            "meta_data": [
                {"key": "_cost_price", "value": "8"},
                {"key": "_sg_fit_make", "value": "Škoda"},
                {"key": "_sg_oe_numbers", "value": "1K0698151"},
                {"key": "_sg_avail", "value": "sr"},
            ],
        }, 55)
        self.assertEqual(payload["id"], 55)
        self.assertNotIn("name", payload)
        self.assertNotIn("description", payload)
        self.assertNotIn("images", payload)
        keys = {row["key"] for row in payload["meta_data"]}
        self.assertEqual(keys, {"_cost_price", "_sg_avail"})


class WatchdogTest(unittest.TestCase):
    def test_fresh_heartbeat_is_not_cancelled(self):
        cancel, _why = evaluate_stuck_job(
            job_age_min=200, timeout_min=330, heartbeat_age_min=5, expect_heartbeat=True,
        )
        self.assertFalse(cancel)

    def test_stale_heartbeat_is_cancelled(self):
        cancel, why = evaluate_stuck_job(
            job_age_min=50, timeout_min=330, heartbeat_age_min=45, expect_heartbeat=True,
        )
        self.assertTrue(cancel)
        self.assertEqual(why, "heartbeat")

    def test_unknown_heartbeat_waits_for_the_job_timeout(self):
        cancel, _why = evaluate_stuck_job(
            job_age_min=200, timeout_min=330, heartbeat_age_min=None, expect_heartbeat=True,
        )
        self.assertFalse(cancel)
        cancel, why = evaluate_stuck_job(
            job_age_min=400, timeout_min=330, heartbeat_age_min=None, expect_heartbeat=False,
        )
        self.assertTrue(cancel)
        self.assertEqual(why, "timeout")


class WurthAndSzekopSplitTest(unittest.TestCase):
    def test_slovak_shop_is_blocked_public_shops_are_not(self):
        self.assertTrue(is_wurth_sk_shop("https://eshop.wurth.sk/sk/SK/EUR/"))
        self.assertTrue(is_wurth_sk_shop("eshop.wurth.sk"))
        self.assertFalse(is_wurth_sk_shop("https://eshop.wurth.ba/"))
        self.assertFalse(is_wurth_sk_shop("https://eshop.wurth-kosova.com/"))
        with self.assertRaises(CatalogLicensingError):
            assert_allowed_catalog_url("https://eshop.wurth.sk/sk/SK/EUR/login")

    def test_phase1_forces_public_mode(self):
        os.environ["SZEKOP_B2B"] = "1"
        prepare_szekop_phase1()
        self.assertEqual(os.environ["SZEKOP_B2B"], "0")
        self.assertEqual(os.environ["SG_SZEKOP_PHASE"], "1")

    def test_clicked_refresh_does_not_live_fetch_logged_in_suppliers(self):
        self.assertTrue(live_fetch_skipped("https://www.lkq.sk/p/1", "lkq"))
        self.assertTrue(live_fetch_skipped("https://szekopsk.nextis.cz/sk/p/1", "szekop"))
        self.assertTrue(live_fetch_skipped("https://eshop.wurth.sk/p/1", "wurth"))
        self.assertFalse(live_fetch_skipped("https://www.pro-tec.sk/p/1", "pro-tec"))


if __name__ == "__main__":
    unittest.main()
