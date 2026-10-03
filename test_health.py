"""Offline regression tests: health noise must not suppress product alerts."""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import tracker


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.now = 100000
        self.cfg = {"digest_every_days": 0}

    def poll(self, ok=(), failed=(), skipped=(), empty=(), deferred=(),
             dry_run=False, send_error=None):
        with patch.object(tracker.time, "time", return_value=self.now), \
             patch.object(tracker.time, "sleep"), \
             patch.object(tracker, "send_telegram", side_effect=send_error) as send, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            messages = tracker.report_health(
                self.state, list(ok), list(failed), list(skipped), list(empty),
                {}, dry_run, self.cfg, list(deferred))
        # Each scheduled execution reloads JSON; no in-memory-only cooldowns.
        self.state = json.loads(json.dumps(self.state))
        return messages, send.call_count

    def sustained(self, name="A"):
        self.poll(ok=["B", "C"], failed=[name])
        self.now += 1800
        self.poll(ok=["B", "C"], failed=[name])
        self.now += 1800
        return self.poll(ok=["B", "C"], failed=[name])

    def test_transient_failures_and_recovery_are_silent(self):
        for _ in range(3):
            self.assertEqual(self.poll(ok=["B", "C"], failed=["A"])[1], 0)
            self.now += 900
        self.assertEqual(self.poll(ok=["A", "B", "C"])[1], 0)
        self.assertNotIn("A", self.state["_health"]["fail_since"])

    def test_summary_repeats_only_after_day(self):
        self.assertEqual(self.sustained()[1], 1)
        last = self.now
        for elapsed in [900, 3600, 86399]:
            self.now = last + elapsed
            self.assertEqual(self.poll(ok=["B", "C"], failed=["A"])[1], 0)
        self.now = last + 86400
        self.assertEqual(self.poll(ok=["B", "C"], failed=["A"])[1], 1)

    def test_flap_does_not_reset_cooldown(self):
        self.sustained()
        last = self.state["_health"]["last_alert"]
        self.poll(ok=["A", "B", "C"])
        self.assertIn("A", self.state["_health"]["alerted"])
        self.now += 900
        self.assertEqual(self.poll(ok=["B", "C"], failed=["A"])[1], 0)
        for _ in range(3):
            self.assertEqual(self.poll(ok=["A", "B", "C"])[1], 0)
        self.assertNotIn("A", self.state["_health"]["alerted"])
        self.assertEqual(self.state["_health"]["last_alert"], last)
        self.assertEqual(self.sustained()[1], 0)

    def test_majority_outage_escalates_during_cooldown_once(self):
        self.sustained()
        for _ in range(2):
            self.now += 900
            self.assertEqual(self.poll(ok=["C"], failed=["A", "B"])[1], 0)
        self.now += 900
        messages, count = self.poll(ok=["C"], failed=["A", "B"])
        self.assertEqual(count, 1)
        self.assertIn("widespread", messages[0])
        self.assertIn("2 store(s)", messages[0])
        self.poll(ok=["A", "B", "C"])
        for _ in range(5):
            self.now += 900
            self.assertEqual(self.poll(ok=["C"], failed=["A", "B"])[1], 0)

    def test_many_failures_are_one_bounded_summary(self):
        failed = [f"Store {n:02}" for n in range(48)]
        for _ in range(2):
            self.assertEqual(self.poll(failed=failed)[1], 0)
            self.now += 900
        messages, count = self.poll(failed=failed)
        self.assertEqual(count, 1)
        self.assertIn("48 store(s)", messages[0])
        self.assertIn("and 33 more", messages[0])
        self.assertLess(len(messages[0]), 4096)

    def test_deferred_stores_preserve_history_not_false_majority(self):
        for _ in range(3):
            self.assertEqual(self.poll(failed=["A"], deferred=["B", "C"])[1], 0)
            self.now += 900
        before = copy.deepcopy(self.state["_health"])
        self.poll(deferred=["A", "B", "C"])
        self.assertEqual(self.state["_health"]["fails"], before["fails"])
        self.assertEqual(self.state["_health"]["fail_since"], before["fail_since"])

    def test_empty_and_skipped_are_grouped(self):
        self.poll(ok=["A", "B"], empty=["A", "B"])
        self.now += 86400
        messages, count = self.poll(ok=["A", "B"], empty=["A", "B"], skipped=["C"])
        self.assertEqual(count, 1)
        self.assertIn("3 store(s)", messages[0])
        self.assertIn("empty catalogue", messages[0])
        self.assertIn("verified:false", messages[0])

    def test_old_state_migrates_without_recovery_burst(self):
        self.state = {"_health": {"alerted": ["A", "B"], "fails": {"A": 20}}}
        messages, count = self.poll(ok=["B"], failed=["A"])
        self.assertEqual(count, 1)
        self.assertNotIn("is back", " ".join(messages))
        for _ in range(3):
            self.assertEqual(self.poll(ok=["B"], failed=["A"])[1], 0)

    def test_removed_stores_pruned_without_stale_alerts(self):
        self.sustained()
        self.now += 86400
        self.assertEqual(self.poll(ok=["B", "C"])[1], 0)
        for field in ["alerted", "fails", "fail_since", "successes", "last_new"]:
            self.assertNotIn("A", self.state["_health"][field])

    def test_failed_delivery_does_not_consume_cooldown(self):
        self.poll(skipped=["A"], send_error=RuntimeError("offline"))
        self.assertNotIn("last_alert", self.state["_health"])
        self.assertEqual(self.poll(skipped=["A"])[1], 1)
        self.assertEqual(self.poll(skipped=["A"])[1], 0)

    def test_dry_run_does_not_send_or_consume_cooldown(self):
        self.assertEqual(self.poll(skipped=["A"], dry_run=True)[1], 0)
        self.assertNotIn("last_alert", self.state["_health"])
        self.assertEqual(self.poll(skipped=["A"])[1], 1)

    def test_malformed_health_state_does_not_touch_products(self):
        self.state = {"example.com": ["123"], "_stock": {"example.com": {}},
                      "_health": {"fails": None, "alerted": [[], "A"],
                                  "last_alert": "bad", "widespread_fails": None}}
        self.poll(ok=["A"])
        self.assertEqual(self.state["example.com"], ["123"])
        self.assertEqual(self.state["_stock"], {"example.com": {}})
        self.state["_health"] = None
        self.poll(ok=["A"])
        self.assertIsInstance(self.state["_health"], dict)

    def test_recovering_only_does_not_send_summary(self):
        self.state = {"_health": {"alerted": ["A", "B"]}}
        self.assertEqual(self.poll(ok=["A", "B"])[1], 0)

    def test_product_alerts_ignore_health_cooldown(self):
        from test_tracker import AdaptiveRunTests
        helper = AdaptiveRunTests()
        state = {"example.com": ["1"], "_health": {
            "alerted": ["Test Store"], "last_alert": self.now}}
        with patch.object(tracker.time, "time", return_value=self.now):
            _, send, save, result, _, _ = helper.run_poll(
                helper.batch([2, 1]), state=state)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.args[0].startswith("🆕"))
        self.assertEqual(result["_health"]["last_alert"], self.now)
        save.assert_called_once()

    def test_invalid_noise_settings_rejected(self):
        for key in ["fail_alerts_after", "recovery_polls", "fail_alert_hours",
                    "alert_cooldown_hours", "empty_alert_hours"]:
            for value in [0, -1, True, "bad", float("nan"), float("inf")]:
                with self.subTest(key=key, value=value):
                    self.cfg[key] = value
                    with self.assertRaises(ValueError):
                        self.poll()
                    del self.cfg[key]


if __name__ == "__main__":
    unittest.main()
