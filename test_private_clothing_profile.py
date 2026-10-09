"""Privacy, Actions entry-point and team-apparel tests using only synthetic data."""
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import patch

import clothing_policy
import private_clothing_profile as private
import tracker
from test_clothing_policy import CFG, POLICY, STORE, SYNTHETIC_PROFILE, chart, product


class ProfileTests(unittest.TestCase):
    def test_present_and_multiple_category_numeric_brand_labels(self):
        loaded = private.load_profile({"CLOTHING_FIT_PROFILE_JSON": json.dumps(SYNTHETIC_PROFILE)})
        self.assertEqual(loaded["fit"], SYNTHETIC_PROFILE["fit"])
        for category, label, brand in [("pants", "S2", "PBJ"), ("pants", "31", "PBJ"),
                                      ("jacket", "J2", "FDMTL"), ("jacket", "7", "Kapital"),
                                      ("pants", "8", "Kapital"), ("clothing", "9", "Kapital")]:
            ptype = {"pants": "Jeans", "jacket": "Jacket", "clothing": "Shirt"}[category]
            p = product(f"Synthetic {brand} Boro {ptype}", brand, product_type=ptype)
            self.assertTrue(tracker.alert_decision(p, STORE, CFG, "restock", [label], loaded)["keep"])
        p = product("Synthetic Kapital Boro Jacket", "Kapital", product_type="Jacket")
        self.assertFalse(tracker.alert_decision(p, STORE, CFG, "restock", ["8"], loaded)["keep"])
        aliased = copy.deepcopy(SYNTHETIC_PROFILE)
        aliased["brand_size_references"] = {"PBJ": {"pants": ["S9"]}}
        self.assertTrue(tracker.alert_decision(product(), STORE, CFG, "restock", ["S9"], aliased)["keep"])

    def test_absent_empty_malformed_nonobject_and_unusable_are_silent(self):
        cases = [None, "", "{", "null", "[]", "true", "42", "{}", '{"unknown":"private-marker"}',
                 '{"fit":null}', '{"fit":{"waist_range":[32,30]}}',
                 '{"fit":{"min_thigh":true}}', '{"fit":{"min_thigh":NaN}}',
                 '{"fit":{"min_thigh":-1}}', '{"fit":{"unsupported":"private-marker"}}',
                 '{"owned_models":"private-marker"}', '{"size_references":{"pants":[]}}',
                 '{"size_references":{"pants":[{}]}}', '{"brand_size_references":[]}']
        for raw in cases:
            with self.subTest(raw_type=type(raw).__name__):
                env = {} if raw is None else {"CLOTHING_FIT_PROFILE_JSON": raw}
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(private.load_profile(env), {})
                self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_environment_precedence_and_readonly_local_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "synthetic.json"
            raw = json.dumps(SYNTHETIC_PROFILE)
            path.write_text(raw)
            env = {"CLOTHING_CONTEXT_PATH": str(path)}
            self.assertEqual(private.load_profile(env)["fit"], SYNTHETIC_PROFILE["fit"])
            other = {"owned_models": ["Synthetic-Other-Owned-Token"]}
            env["CLOTHING_FIT_PROFILE_JSON"] = json.dumps(other)
            self.assertEqual(private.load_profile(env), other)
            for invalid in ["", "{"]:
                env["CLOTHING_FIT_PROFILE_JSON"] = invalid
                self.assertEqual(private.load_profile(env), {})
            self.assertEqual(path.read_text(), raw)
        self.assertEqual(private.load_profile({"CLOTHING_CONTEXT_PATH": "/nonexistent/synthetic-profile"}), {})

    def test_absent_profile_has_no_size_gating_or_measurement_bonus_penalty(self):
        for kind, extra in [("restock", ["Q2"]),
                            ("price_drop", [{"variant": "Q2", "from": "250", "to": "200"}])]:
            p = product(body_html=chart(thigh=2, rise=2, knee=2, hem=30))
            without = tracker.alert_decision(p, STORE, CFG, kind, extra, {})
            no_chart = tracker.alert_decision(product(), STORE, CFG, kind, extra, {})
            self.assertTrue(without["keep"])
            self.assertEqual(without["score"], no_chart["score"])
            self.assertFalse(without["fit"]["roomy"])
            self.assertFalse(without["fit"]["narrow"])
            self.assertEqual(without["fit"]["selected"], {})
            self.assertIsNone(without["size_available"])
            msg = tracker.format_clothing_message(STORE, p, kind, extra, without)
            self.assertIn("Live product-page inventory and personal size availability unverified.", msg)

    def test_profile_ownership_is_context_and_not_state(self):
        profile = dict(SYNTHETIC_PROFILE, owned_models=["Synthetic-Owned-Token"])
        p = product("Synthetic PBJ Synthetic-Owned-Token Extreme Slub Jeans")
        before = copy.deepcopy(profile)
        d = tracker.alert_decision(p, STORE, CFG, "new", context=profile)
        self.assertTrue(d["keep"])
        self.assertTrue(d["owned"])
        self.assertEqual(profile, before)
        self.assertNotIn("owned_models", d)
        self.assertNotIn("size_references", d)
        self.assertNotIn("waist_range", json.dumps(d))

    def test_owned_brand_alias_and_tokens_allow_title_variations_without_overmatching(self):
        profile = {"owned_models": [{"brand": "PBJ", "tokens": ["Synthetic-Own-A", "Synthetic-Dye-B"]}]}
        p = product("Synthetic Pure Blue Japan Synthetic-Own-A Extreme Slub Synthetic-Dye-B Jeans", "Pure Blue Japan")
        owned = tracker.alert_decision(p, STORE, CFG, "new", context=profile)
        plain = tracker.alert_decision(p, STORE, CFG, "new", context={})
        self.assertTrue(owned["owned"])
        self.assertEqual((owned["keep"], owned["score"]), (plain["keep"], plain["score"]))
        p["vendor"] = "ONI"
        self.assertFalse(tracker.alert_decision(p, STORE, CFG, "new", context=profile)["owned"])
        self.assertEqual(private.validate_profile({"owned_models": [{"brand": "PBJ", "tokens": []}]}), {})

    def test_actions_environment_secret_same_production_entrypoint_no_logging_or_storage(self):
        # Actual workflow command enters main -> cmd_run -> clothing_context.
        # All external effects are replaced with mocks; this is no live run.
        p = product(variants=[{"id": 11, "title": "Q2", "available": True, "price": "250"}])
        cfg = dict(CFG, stores=[dict(STORE, verified=True)])
        old = copy.deepcopy(p)
        old["variants"][0]["available"] = False
        state = {"example.com": ["1"], "_stock": {"example.com": {"1": tracker.snapshot_variants(old)}},
                 "_last_poll": {}}
        secret = dict(SYNTHETIC_PROFILE, owned_models=["Synthetic-Secret-Marker-Never-Echo"])
        raw = json.dumps(secret)
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"CLOTHING_FIT_PROFILE_JSON": raw}, clear=True))
            stack.enter_context(patch.object(sys, "argv", ["tracker.py", "run"]))
            stack.enter_context(patch.object(tracker, "load_json", side_effect=lambda path, default: cfg if path == tracker.CONFIG_PATH else state))
            stack.enter_context(patch.object(tracker, "fetch_products", return_value=([p], [p])))
            stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
            stack.enter_context(patch.object(tracker, "report_health"))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            save = stack.enter_context(patch.object(tracker, "save_json"))
            send = stack.enter_context(patch.object(tracker, "send_telegram"))
            network = stack.enter_context(patch.object(tracker, "http_get_json"))
            telegram = stack.enter_context(patch.object(tracker, "telegram_api"))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            tracker.main()
        # Q2 is suppressed only when the environment profile was consumed.
        send.assert_not_called()
        network.assert_not_called()
        telegram.assert_not_called()
        save.assert_called_once()
        self.assertTrue(output.getvalue().startswith("Clothing fit profile: loaded\n"))
        self.assertEqual(output.getvalue().count("Clothing fit profile:"), 1)
        self.assertEqual(errors.getvalue(), "")
        all_outputs = output.getvalue() + errors.getvalue() + json.dumps(save.call_args.args) + json.dumps(cfg)
        for key in [raw, "Synthetic-Secret-Marker-Never-Echo", "size_references", "owned_models", "waist_range"]:
            self.assertNotIn(key, all_outputs)

    def test_workflow_secret_is_only_passed_to_tracker_step(self):
        source = (pathlib.Path(__file__).parent / ".github/workflows/track.yml").read_text()
        self.assertEqual(source.count("CLOTHING_FIT_PROFILE_JSON:"), 1)
        self.assertIn("CLOTHING_FIT_PROFILE_JSON: ${{ secrets.CLOTHING_FIT_PROFILE_JSON }}", source)
        self.assertNotIn("echo", source)
        self.assertNotIn("CLOTHING_CONTEXT_PATH", source)

    def test_actions_entrypoint_absent_empty_malformed_emit_unverified_fallback_restock(self):
        for raw in [None, "", '{"Synthetic-Malformed-Marker":']:
            with self.subTest(secret_state="absent" if raw is None else "invalid"):
                p = product(variants=[{"id": 11, "title": "Q2", "available": True, "price": "250"}])
                old = copy.deepcopy(p)
                old["variants"][0]["available"] = False
                state = {"example.com": ["1"], "_stock": {"example.com": {"1": tracker.snapshot_variants(old)}},
                         "_last_poll": {}}
                cfg = dict(CFG, stores=[dict(STORE, verified=True)])
                env = {} if raw is None else {"CLOTHING_FIT_PROFILE_JSON": raw}
                output, errors = io.StringIO(), io.StringIO()
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, env, clear=True))
                    stack.enter_context(patch.object(sys, "argv", ["tracker.py", "run"]))
                    stack.enter_context(patch.object(tracker, "load_json", side_effect=lambda path, default: cfg if path == tracker.CONFIG_PATH else state))
                    stack.enter_context(patch.object(tracker, "fetch_products", return_value=([p], [p])))
                    stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
                    stack.enter_context(patch.object(tracker, "report_health"))
                    stack.enter_context(patch.object(tracker.time, "sleep"))
                    stack.enter_context(patch.object(tracker, "save_json"))
                    send = stack.enter_context(patch.object(tracker, "send_telegram"))
                    network = stack.enter_context(patch.object(tracker, "http_get_json"))
                    stack.enter_context(redirect_stdout(output))
                    stack.enter_context(redirect_stderr(errors))
                    tracker.main()
                send.assert_called_once()
                network.assert_not_called()
                self.assertIn("Catalogue reports a restock", send.call_args.args[0])
                self.assertIn("Live product-page inventory and personal size availability unverified.", send.call_args.args[0])
                self.assertNotIn("Synthetic-Malformed-Marker", output.getvalue() + errors.getvalue() + json.dumps(state))
                self.assertNotIn("owned_models", json.dumps(state))
                expected = "absent" if raw is None or raw == "" else "invalid, using fallback"
                self.assertTrue(output.getvalue().startswith("Clothing fit profile: " + expected + "\n"))
                self.assertEqual(output.getvalue().count("Clothing fit profile:"), 1)
                self.assertEqual(errors.getvalue(), "")


class StartupStatusTests(unittest.TestCase):
    """Exact stdout/stderr through the workflow CLI, with every effect mocked."""
    def run_cli(self, env, expected):
        cfg = dict(CFG, stores=[])
        state = {}
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(sys, "argv", ["tracker.py", "run"]))
            stack.enter_context(patch.object(tracker, "load_json", side_effect=lambda path, default: cfg if path == tracker.CONFIG_PATH else state))
            stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
            stack.enter_context(patch.object(tracker, "report_health"))
            stack.enter_context(patch.object(tracker.time, "time", return_value=0))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            save = stack.enter_context(patch.object(tracker, "save_json"))
            forbidden = [stack.enter_context(patch.object(tracker, name)) for name in
                         ("fetch_products", "http_get_json", "telegram_api", "send_telegram")]
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            tracker.main()
        expected_stdout = (
            "Clothing fit profile: " + expected + "\n"
            "Done in 0.0s. Seeded 0 store(s), sent 0 notification(s).\n"
            "Census: 0/0 polled ok, 0 failed, 0 returned empty, 0 not due, 0 skipped (unverified).\n"
            "All stores responded in under 2s.\n")
        self.assertEqual(output.getvalue(), expected_stdout)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(output.getvalue().count("Clothing fit profile:"), 1)
        self.assertNotIn("Synthetic-Secret-Startup-Canary", output.getvalue() + errors.getvalue() + json.dumps(state))
        save.assert_called_once()
        for call in forbidden:
            call.assert_not_called()

    def test_loaded_exact_stdout_and_no_secret_content(self):
        raw = json.dumps(dict(SYNTHETIC_PROFILE, owned_models=["Synthetic-Secret-Startup-Canary"]))
        self.run_cli({"CLOTHING_FIT_PROFILE_JSON": raw}, "loaded")

    def test_missing_secret_exact_absent_stdout(self):
        self.run_cli({}, "absent")

    def test_empty_secret_exact_absent_stdout(self):
        self.run_cli({"CLOTHING_FIT_PROFILE_JSON": ""}, "absent")
        self.run_cli({"CLOTHING_FIT_PROFILE_JSON": "  \n"}, "absent")

    def test_malformed_and_unusable_secret_exact_invalid_stdout(self):
        for raw in ['{"Synthetic-Secret-Startup-Canary":',
                    '{"unsupported":"Synthetic-Secret-Startup-Canary"}',
                    '{"fit":{"waist_range":"Synthetic-Secret-Startup-Canary"}}']:
            self.run_cli({"CLOTHING_FIT_PROFILE_JSON": raw}, "invalid, using fallback")

    def test_local_profile_status_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "Synthetic-Secret-Startup-Canary.json"
            path.write_text(json.dumps(SYNTHETIC_PROFILE))
            env = {"CLOTHING_CONTEXT_PATH": str(path)}
            self.run_cli(env, "loaded")
            self.run_cli(dict(env, CLOTHING_FIT_PROFILE_JSON=""), "absent")
            self.run_cli(dict(env, CLOTHING_FIT_PROFILE_JSON="{"), "invalid, using fallback")
            env["CLOTHING_CONTEXT_PATH"] = str(path.parent / "missing.json")
            self.run_cli(env, "absent")


class TeamApparelTests(unittest.TestCase):
    def test_existing_lists_retain_legacy_garment_decisions_at_all_three_stores(self):
        stores = [s for s in CFG["stores"] if s.get("filters", {}).get("include_keywords")]
        self.assertEqual({s["name"] for s in stores}, {"Hat Club", "Culture Kings US", "New Era Cap"})
        for store in stores:
            for title in ["Synthetic 49ers Work Jacket", "Synthetic SF Giants Linen Shirt",
                          "Synthetic UCLA Warmup Pants", "Synthetic Yankees Work Jacket",
                          "Synthetic Generic Fashion Jacket", "Synthetic Youth 49ers Jacket",
                          "Synthetic 49ers Hoodie", "Synthetic Athletics Department Jacket",
                          "Synthetic Boston Bruins Jacket", "Synthetic 49ers Jersey"]:
                for vendor in ["Outside Team Supplier", "New Era", "PBJ"]:
                    p = product(title, vendor, product_type="Clothing")
                    expected = tracker.passes_filters(p, tracker.resolve_filters(store, CFG["filters"]))
                    decision = tracker.alert_decision(p, store, CFG, "new", context=SYNTHETIC_PROFILE)
                    with self.subTest(store=store["name"], title=title, vendor=vendor):
                        self.assertEqual(decision["keep"], expected)
                        if decision["legacy_team_apparel"]:
                            self.assertFalse(decision["applies"])
            p = product("Synthetic 49ers Work Jacket", "Outside Team Supplier", product_type="Jacket",
                        variants=[{"id": 11, "title": "Q2", "available": True, "price": "200"}])
            self.assertTrue(tracker.alert_decision(p, store, CFG, "restock", ["Q2"], SYNTHETIC_PROFILE)["keep"])
            events, history = tracker.additional_price_events([p], [], {}, CFG, store, context=SYNTHETIC_PROFILE)
            self.assertEqual((events, history), ([], {}))
            p["vendor"] = "PBJ"
            self.assertEqual(tracker.additional_price_events([p], [], {}, CFG, store), ([], {}))

    def test_team_mention_does_not_bypass_non_team_store_whitelist(self):
        p = product("Synthetic 49ers Boro Work Jacket", "Outside Team Supplier", product_type="Jacket")
        d = tracker.alert_decision(p, STORE, CFG, "new")
        self.assertFalse(d["legacy_team_apparel"])
        self.assertFalse(d["keep"])


class DiscountThresholdTests(unittest.TestCase):
    def test_percent_and_currency_floor_are_both_required(self):
        def drops(anchor, price, currency="USD"):
            p = product(price=str(price))
            return tracker.clothing_price_events([p], {"1": {"11": str(anchor)}}, POLICY, currency)[0]
        self.assertEqual(drops(100, 85), [])  # percentage alone is insufficient
        self.assertEqual(drops(1000, 980), [])  # amount alone is insufficient
        self.assertTrue(drops(100, 80))
        for currency, floor in POLICY["price_drop_minimum"].items():
            anchor = floor * 5
            self.assertEqual(drops(anchor, anchor - floor + 1, currency), [])
            self.assertTrue(drops(anchor, anchor - floor, currency))


if __name__ == "__main__":
    unittest.main()
