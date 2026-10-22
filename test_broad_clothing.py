"""Broad feed regression tests. Every private-context fixture is synthetic."""
import copy
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import clothing_policy as policy
import tracker
from test_delivery import fresh_outbox
from test_clothing_policy import CFG, POLICY, STORE, SYNTHETIC_PROFILE, chart, product


class BroadClothingTests(unittest.TestCase):
    def test_all_brands_and_ordinary_garments_at_every_store(self):
        for store in CFG["stores"]:
            for brand in ["Iron Heart", "Levi's", "Nike", "Unknown Brand", "", "PBJ"]:
                for title, ptype in [("Plain Jeans", "Jeans"), ("Plain Trucker Jacket", "Jacket"),
                                     ("Generic Hoodie", "Hoodie"), ("Youth Team Jersey", "Jersey"),
                                     ("Ordinary Tee", "T-Shirt")]:
                    with self.subTest(store=store["name"], brand=brand, title=title):
                        d = tracker.alert_decision(product(title, brand, product_type=ptype), store, CFG, "new")
                        self.assertTrue(d["keep"])
                        self.assertTrue(d["applies"])

    def test_additional_garment_categories_are_eligible(self):
        for ptype in ["Blouse", "Joggers", "Leggings", "Parka", "Kimono", "Underwear",
                      "Sleepwear", "Swimwear", "Boxers", "Tank", "Robe", "Suit", "Apron"]:
            self.assertTrue(tracker.alert_decision(product("Ordinary Item", "Outside", product_type=ptype),
                                                  STORE, CFG, "new")["keep"], ptype)

    def test_former_whitelist_and_interest_settings_cannot_reintroduce_a_gate(self):
        stale = dict(POLICY, core_brands=["PBJ"], min_interest_score=999999,
                     interest={"textures": ["extreme slub"]})
        p = product("Plain Skinny Jeans", "Outside")
        self.assertTrue(policy.evaluate(p, stale, "new")["keep"])
        self.assertNotIn("score", policy.evaluate(p, stale, "new"))

    def test_socks_and_all_footwear_are_excluded_by_identity(self):
        for word in ["Socks", "Sock", "Hosiery", "Shoes", "Footwear", "Sneakers", "Trainers",
                     "Boots", "Sandals", "Slippers", "Loafers", "Espadrilles", "Clogs", "Mules", "Flip-Flops"]:
            for title, ptype in [("Denim " + word, "Clothing"), ("Denim " + word, "Accessories"), ("Generic Item", word)]:
                for store in CFG["stores"]:
                    with self.subTest(word=word, store=store["name"], ptype=ptype):
                        p = product(title, "PBJ", product_type=ptype)
                        self.assertEqual(policy.category(p), "socks_footwear")
                        self.assertFalse(tracker.alert_decision(p, store, CFG, "new")["keep"])
                        self.assertEqual(tracker.clothing_price_events([p], None, POLICY), ([], {}))

    def test_description_recommendations_never_change_identity(self):
        body = "Wear with socks, shoes, jeans, a denim jacket and a New Era cap."
        cases = [("Plain Shirt", "Shirt", True, "clothing"),
                 ("49ers Cap", "Hat", True, "hat"),
                 ("Cotton Socks", "Socks", False, "socks_footwear"),
                 ("Plain Shoes", "Footwear", False, "socks_footwear"),
                 ("Leather Wallet", "Accessories", False, "other")]
        for title, ptype, kept, category in cases:
            d = tracker.alert_decision(product(title, "Outside", product_type=ptype, body_html=body), STORE, CFG, "new")
            self.assertEqual((d["keep"], d["category"]), (kept, category))

    def test_unrelated_merchandise_cannot_enter_via_fabric_or_clothing_copy(self):
        for title, ptype in [("Denim Tote", "Bags"), ("Boro Denim Cushion", "Homeware"),
                             ("Patchwork Denim Belt", "Accessories"), ("Raw Denim Yardage", "Fabric"),
                             ("Leather Wallet", "Wallet"), ("Monitor", "Electronics"),
                             ("Gift Card", "Gift Card"), ("Mystery Item", "")]:
            p = product(title, "Kapital", product_type=ptype, body_html="Rare sashiko denim clothing")
            self.assertFalse(tracker.alert_decision(p, STORE, CFG, "new")["keep"], title)
            self.assertEqual(tracker.clothing_price_events([p], None, POLICY), ([], {}))

    def test_boot_cut_and_fabric_descriptors_do_not_exclude_garments(self):
        for title in ["Boot Cut Jeans", "Bootcut Jeans", "Shoe Cut Wide Flare Jeans", "Mixed Fabric Shirt",
                      "Bandana Print Shirt", "Socks Graphic Shirt", "Sockless Summer Shirt"]:
            p = product(title, "Outside", product_type="Jeans" if "Jeans" in title else "Shirt")
            self.assertTrue(tracker.alert_decision(p, STORE, CFG, "new")["keep"])

    def test_size_and_measured_fit_never_gate_an_event(self):
        p = product("Ordinary Skinny Jeans", "Outside", body_html=chart(thigh=2, rise=2, knee=2, hem=30))
        for kind, extra in [("new", None), ("restock", ["Q2"]),
                            ("price_drop", [{"variant": "Q2", "from": "250", "to": "200"}])]:
            with_profile = tracker.alert_decision(p, STORE, CFG, kind, extra, SYNTHETIC_PROFILE)
            absent = tracker.alert_decision(p, STORE, CFG, kind, extra, {})
            self.assertTrue(with_profile["keep"])
            self.assertEqual(with_profile["keep"], absent["keep"])
            self.assertTrue(with_profile["fit"]["narrow"])
            self.assertIsNone(with_profile["size_available"])

    def test_hat_path_ignores_the_clothing_policy(self):
        disabled = dict(CFG, clothing_policy={})
        for store in CFG["stores"]:
            for vendor in ["Outside", "New Era", "NEWERA"]:
                for title in ["49ers Cap", "UCLA Fitted Cap", "Yankees Cap", "Generic Hat", "Youth 49ers Cap"]:
                    p = product(title, vendor, product_type="Hat", body_html="Pair with socks and shoes")
                    for kind, extra in [("new", None), ("restock", ["Q2"])]:
                        current = tracker.alert_decision(p, store, CFG, kind, extra, SYNTHETIC_PROFILE)
                        reference = tracker.alert_decision(p, store, disabled, kind, extra)
                        self.assertEqual(current, reference)
                        self.assertFalse(current["applies"])

    def test_stock_requirement_still_applies_to_every_brand(self):
        p = product("Plain Hoodie", "Outside", product_type="Hoodie", variants=[{"id": 11, "available": False}])
        for store in CFG["stores"]:
            self.assertFalse(tracker.alert_decision(p, store, CFG, "new")["keep"])

    def test_unknown_brands_do_not_share_unrelated_private_brand_context(self):
        profile = {"brand_size_references": {"Unknown A": {"pants": ["Q2"]}},
                   "owned_models": [{"brand": "Unknown A", "tokens": ["Synthetic-Owned"]}]}
        self.assertEqual(policy.reference_labels(profile, "pants", None), set())
        p = product("Synthetic-Owned Plain Jeans", "Unknown B")
        d = tracker.alert_decision(p, STORE, CFG, "new", context=profile)
        self.assertTrue(d["keep"])
        self.assertFalse(d["owned"])

    def run_mocked(self, products, state):
        cfg = dict(CFG, stores=[dict(STORE, verified=True)])
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(tracker.delivery, "open_outbox", side_effect=fresh_outbox))
            stack.enter_context(patch.object(tracker, "load_json", return_value=state))
            stack.enter_context(patch.object(tracker, "fetch_products", return_value=(products, products)))
            stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
            stack.enter_context(patch.object(tracker, "clothing_context", return_value=SYNTHETIC_PROFILE))
            stack.enter_context(patch.object(tracker, "report_health"))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            stack.enter_context(patch.object(tracker, "save_json"))
            network = stack.enter_context(patch.object(tracker, "http_get_json"))
            telegram = stack.enter_context(patch.object(tracker, "telegram_api"))
            send = stack.enter_context(patch.object(tracker, "send_telegram", return_value=1))
            stack.enter_context(redirect_stdout(output))
            tracker.cmd_run(cfg)
        network.assert_not_called()
        telegram.assert_not_called()
        return send, output.getvalue()

    def test_previously_rejected_unchanged_ids_do_not_become_a_backlog(self):
        products = [product("Ordinary Jeans", "Outside"),
                    product("49ers Hoodie", "Outside", id=2, product_type="Hoodie"),
                    product("Cotton Socks", "Outside", id=3, product_type="Socks")]
        state = {"example.com": ["1", "2", "3"], "_stock": {"example.com": {
                 str(p["id"]): tracker.snapshot_variants(p) for p in products}}, "_last_poll": {}}
        before = copy.deepcopy(state)
        send, output = self.run_mocked(products, state)
        send.assert_not_called()
        self.assertEqual(state["example.com"], before["example.com"])
        self.assertEqual(state["_stock"], before["_stock"])
        self.assertEqual({pid: prices["11"] for pid, prices in state["_clothing_prices"]["example.com"].items()}, {"1": "250", "2": "250"})
        self.assertIn("event_eligible=0, event_rejected=0", output)

    def test_counts_are_aggregate_integers_and_rejected_ids_still_dedupe(self):
        products = [product("Known Jeans", "Outside"),
                    product("Synthetic-Private-Canary Hoodie", "Outside", id=2, product_type="Hoodie"),
                    product("Cotton Socks", "Outside", id=3, product_type="Socks"),
                    product("Shoes", "Outside", id=4, product_type="Footwear"),
                    product("Yankees Cap", "New Era", id=5, product_type="Hat"),
                    product("Leather Wallet", "Outside", id=6, product_type="Accessories")]
        state = {"example.com": ["1"], "_stock": {"example.com": {"1": tracker.snapshot_variants(products[0])}}, "_last_poll": {}}
        send, output = self.run_mocked(products, state)
        self.assertEqual(send.call_count, 2)  # garment plus now-unrestricted New Era hat
        line = next(line for line in output.splitlines() if line.startswith("Feed counts:"))
        self.assertEqual(line, "Feed counts: garments=2, hats=1, socks_footwear=2, other=1, event_eligible=2, event_rejected=3")
        self.assertRegex(line, r"^Feed counts: [a-z_]+=\d+(, [a-z_]+=\d+){5}$")
        for token in ["Synthetic-Private-Canary", "size_references", "waist_range", "owned_models"]:
            self.assertNotIn(token, output)
        state["_last_poll"] = {}
        send_again, _ = self.run_mocked(products, state)
        send_again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
