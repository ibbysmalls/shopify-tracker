#!/usr/bin/env python3
"""Unit tests for restock detection and collection-aware polling."""

import unittest
import copy
import io
import sys
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import patch

import tracker


def variant(vid, title, available=True, inventory_quantity=None):
    v = {"id": vid, "title": title, "available": available, "price": "100.00"}
    if inventory_quantity is not None:
        v["inventory_quantity"] = inventory_quantity
    return v


def product(pid, title="Jeans", variants=None, handle="jeans"):
    return {
        "id": pid,
        "title": title,
        "handle": handle,
        "vendor": "OD",
        "product_type": "Jeans",
        "variants": variants or [variant(1, "32", True)],
    }


class ProductsUrlTests(unittest.TestCase):
    def test_store_catalog(self):
        self.assertEqual(
            tracker.products_url("www.okayamadenim.com", 20),
            "https://www.okayamadenim.com/products.json?limit=20")

    def test_collection(self):
        self.assertEqual(
            tracker.products_url("www.okayamadenim.com", 20, "new-restocks"),
            "https://www.okayamadenim.com/collections/new-restocks/products.json?limit=20")

    def test_collection_strips_slashes(self):
        self.assertEqual(
            tracker.products_url("ex.com", 5, "/new-restocks/"),
            "https://ex.com/collections/new-restocks/products.json?limit=5")


class StoreCollectionsTests(unittest.TestCase):
    def test_none(self):
        self.assertEqual(tracker.store_collections({"domain": "x.com"}), [])

    def test_singular_and_list_deduped(self):
        store = {"collection": "new-restocks",
                 "collections": ["new-restocks", "denim"]}
        self.assertEqual(tracker.store_collections(store),
                         ["new-restocks", "denim"])


class VariantInStockTests(unittest.TestCase):
    def test_available_flag(self):
        self.assertTrue(tracker.variant_in_stock({"available": True}))
        self.assertFalse(tracker.variant_in_stock({"available": False}))

    def test_inventory_zero_wins(self):
        self.assertFalse(tracker.variant_in_stock(
            {"available": True, "inventory_quantity": 0}))

    def test_inventory_positive_without_flag(self):
        self.assertTrue(tracker.variant_in_stock({"inventory_quantity": 3}))


class DiffStoreTests(unittest.TestCase):
    def test_first_run_seeds_silently(self):
        p = product(10, variants=[variant(1, "32", True)])
        events, ids, stock = tracker.diff_store([p], [], None)
        self.assertEqual(events, [])
        self.assertEqual(ids, ["10"])
        self.assertTrue(stock["10"]["1"]["available"])

    def test_new_id_still_notifies(self):
        old = product(10, variants=[variant(1, "32", True)])
        new = product(11, title="New Jacket",
                      variants=[variant(2, "M", True)])
        prev_stock = {"10": {"1": {"available": True, "title": "32"}}}
        events, ids, stock = tracker.diff_store(
            [old, new], ["10"], prev_stock)
        kinds = [(k, str(p["id"]), extra) for k, p, extra in events]
        self.assertEqual(kinds, [("new", "11", None)])
        self.assertIn("11", ids)
        self.assertIn("11", stock)

    def test_restock_unavailable_to_available(self):
        p = product(10, variants=[
            variant(1, "32", False),
            variant(2, "34", True),
        ])
        prev_stock = {"10": {
            "1": {"available": False, "title": "32"},
            "2": {"available": False, "title": "34"},
        }}
        events, _, next_stock = tracker.diff_store([p], ["10"], prev_stock)
        self.assertEqual(len(events), 1)
        kind, prod, titles = events[0]
        self.assertEqual(kind, "restock")
        self.assertEqual(prod["id"], 10)
        self.assertEqual(titles, ["34"])
        self.assertTrue(next_stock["10"]["2"]["available"])
        self.assertFalse(next_stock["10"]["1"]["available"])

    def test_missing_variant_coming_back(self):
        p = product(10, variants=[
            variant(1, "32", True),
            variant(2, "36", True),
        ])
        prev_stock = {"10": {
            "1": {"available": True, "title": "32"},
        }}
        events, _, _ = tracker.diff_store([p], ["10"], prev_stock)
        self.assertEqual(events[0][0], "restock")
        self.assertEqual(events[0][2], ["36"])

    def test_no_spam_when_still_available(self):
        p = product(10, variants=[variant(1, "32", True)])
        prev_stock = {"10": {"1": {"available": True, "title": "32"}}}
        events, _, _ = tracker.diff_store([p], ["10"], prev_stock)
        self.assertEqual(events, [])

    def test_restock_can_fire_again_after_oos(self):
        p = product(10, variants=[variant(1, "32", True)])
        prev_stock = {"10": {"1": {"available": False, "title": "32"}}}
        events, _, next_stock = tracker.diff_store([p], ["10"], prev_stock)
        self.assertEqual(events[0][0], "restock")

        events2, _, _ = tracker.diff_store([p], ["10"], next_stock)
        self.assertEqual(events2, [])

        oos = product(10, variants=[variant(1, "32", False)])
        _, _, after_oos = tracker.diff_store([oos], ["10"], next_stock)
        events3, _, _ = tracker.diff_store([p], ["10"], after_oos)
        self.assertEqual(events3[0][0], "restock")

    def test_upgrade_seeds_stock_without_restock_spam(self):
        """Existing seen IDs but no _stock yet: seed, still report new IDs."""
        known = product(10, variants=[variant(1, "32", True)])
        fresh = product(11, title="New", variants=[variant(2, "M", True)])
        events, _, stock = tracker.diff_store(
            [known, fresh], ["10"], None)
        kinds = [k for k, _, _ in events]
        self.assertEqual(kinds, ["new"])
        self.assertEqual(events[0][1]["id"], 11)
        self.assertIn("10", stock)
        self.assertIn("11", stock)

    def test_first_sight_of_known_product_variants_does_not_restock(self):
        p = product(10, variants=[variant(1, "32", True)])
        events, _, stock = tracker.diff_store([p], ["10"], {})
        self.assertEqual(events, [])
        self.assertTrue(stock["10"]["1"]["available"])

    def test_window_growth_seeds_older_ids_silently(self):
        """Raising 20 → 50 must not treat catalog[20:] as new drops."""
        catalog = [product(i, title=f"P{i}") for i in range(1, 51)]
        seen = [str(i) for i in range(1, 21)]
        prev_stock = {str(i): {"1": {"available": True, "title": "32"}}
                      for i in range(1, 21)}
        # A genuine new drop at the front, plus 30 older IDs now visible.
        catalog[0] = product(99, title="Brand new")
        seed = tracker.window_seed_ids(catalog, 20, 50)
        self.assertEqual(len(seed), 30)
        self.assertNotIn("99", seed)
        self.assertIn("21", seed)
        self.assertIn("50", seed)

        events, ids, stock = tracker.diff_store(
            catalog, seen, prev_stock, seed_ids=seed)
        kinds = [(k, str(p["id"])) for k, p, _ in events]
        self.assertEqual(kinds, [("new", "99")])
        self.assertIn("50", ids)
        self.assertIn("50", stock)
        self.assertNotIn(("new", "21"), kinds)
        self.assertNotIn(("restock", "21"), kinds)

    def test_window_seed_first_sight_does_not_restock(self):
        older = product(21, variants=[variant(1, "32", True)])
        events, _, stock = tracker.diff_store(
            [older], ["10"], {"10": {"1": {"available": True, "title": "32"}}},
            seed_ids=["21"])
        self.assertEqual(events, [])
        self.assertTrue(stock["21"]["1"]["available"])

    def test_window_seed_does_not_block_real_restock_of_known_id(self):
        p = product(10, variants=[variant(1, "32", True)])
        prev_stock = {"10": {"1": {"available": False, "title": "32"}}}
        events, _, _ = tracker.diff_store(
            [p], ["10"], prev_stock, seed_ids=["10"])
        self.assertEqual(events[0][0], "restock")


class WindowSeedIdsTests(unittest.TestCase):
    def test_same_or_smaller_limit_is_noop(self):
        catalog = [product(i) for i in range(50)]
        self.assertEqual(tracker.window_seed_ids(catalog, 50, 50), set())
        self.assertEqual(tracker.window_seed_ids(catalog, 50, 20), set())

    def test_missing_prev_limit_is_noop(self):
        catalog = [product(i) for i in range(50)]
        self.assertEqual(tracker.window_seed_ids(catalog, None, 50), set())


class ResolveFiltersTests(unittest.TestCase):
    def test_store_overrides_key_by_key(self):
        global_f = {
            "include_keywords": [],
            "exclude_keywords": ["youth"],
            "include_product_types": [],
            "notify_only_available": True,
        }
        store = {"filters": {
            "include_keywords": ["49ers"],
            "exclude_keywords": ["infant"],
        }}
        resolved = tracker.resolve_filters(store, global_f)
        self.assertEqual(resolved["include_keywords"], ["49ers"])
        self.assertEqual(resolved["exclude_keywords"], ["infant"])
        self.assertEqual(resolved["include_product_types"], [])
        self.assertTrue(resolved["notify_only_available"])

    def test_omitted_keys_fall_through(self):
        global_f = {"include_keywords": [], "notify_only_available": True}
        store = {"filters": {"include_keywords": ["lakers"]}}
        resolved = tracker.resolve_filters(store, global_f)
        self.assertEqual(resolved["include_keywords"], ["lakers"])
        self.assertTrue(resolved["notify_only_available"])

    def test_no_store_filters_uses_global(self):
        global_f = {"include_keywords": ["x"]}
        self.assertEqual(
            tracker.resolve_filters({}, global_f),
            {"include_keywords": ["x"]})

    def test_does_not_mutate_global(self):
        global_f = {"include_keywords": ["x"]}
        tracker.resolve_filters(
            {"filters": {"include_keywords": ["y"]}}, global_f)
        self.assertEqual(global_f["include_keywords"], ["x"])


class FormatMessageTests(unittest.TestCase):
    def test_new_product_keeps_badge_and_url(self):
        msg = tracker.format_message(
            "Okayamadenim", "www.okayamadenim.com",
            product(10, title="Selvedge", handle="selvedge"))
        self.assertIn("🆕 Okayamadenim", msg)
        self.assertIn("https://www.okayamadenim.com/products/selvedge", msg)
        self.assertNotIn("Restocked", msg)

    def test_restock_lists_sizes(self):
        msg = tracker.format_message(
            "Okayamadenim", "www.okayamadenim.com",
            product(10, title="Selvedge", handle="selvedge"),
            restocked=["32", "34"])
        self.assertIn("♻️ Okayamadenim", msg)
        self.assertIn("Restocked: 32, 34", msg)
        self.assertIn("https://www.okayamadenim.com/products/selvedge", msg)

    def test_uses_woo_permalink(self):
        p = product(10, title="Tee", handle="tee")
        p["_url"] = "https://example.com/product/tee/"
        msg = tracker.format_message("Woo", "example.com", p)
        self.assertIn("https://example.com/product/tee/", msg)
        self.assertNotIn("/products/tee", msg)

    def test_currency_symbol(self):
        msg = tracker.format_message(
            "EU", "eu.com", product(10, title="Cap"), currency="EUR")
        self.assertIn("€100.00", msg)
        self.assertNotIn("$100", msg)

    def test_default_currency_is_dollar(self):
        msg = tracker.format_message("US", "us.com", product(10, title="Cap"))
        self.assertIn("$100.00", msg)

    def test_no_price_omits_price_line(self):
        p = product(10, title="Cap", variants=[{"id": 1, "title": "32",
                                               "available": True}])
        msg = tracker.format_message("EU", "eu.com", p, currency="EUR")
        self.assertNotIn("€", msg)
        self.assertNotIn("$", msg)


class FetchShopifyMergeTests(unittest.TestCase):
    def test_merges_collection_and_catalog_by_id(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            if "/collections/new-restocks/" in url:
                return {"products": [product(10, title="From collection")]}
            return {"products": [product(10, title="From catalog"),
                                 product(11, title="Brand new")]}

        orig = tracker.http_get_json
        tracker.http_get_json = fake_get
        try:
            products, catalog = tracker.fetch_shopify_products(
                "www.okayamadenim.com", 20,
                collections=["new-restocks"], catalog=True)
        finally:
            tracker.http_get_json = orig

        self.assertEqual(
            calls,
            ["https://www.okayamadenim.com/collections/new-restocks/products.json?limit=20",
             "https://www.okayamadenim.com/products.json?limit=20"])
        by_id = {p["id"]: p for p in products}
        self.assertEqual(by_id[10]["title"], "From collection")
        self.assertEqual(by_id[11]["title"], "Brand new")
        self.assertEqual([p["id"] for p in catalog], [10, 11])

    def test_catalog_false_skips_storewide(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            return {"products": [product(10)]}

        orig = tracker.http_get_json
        tracker.http_get_json = fake_get
        try:
            products, catalog = tracker.fetch_shopify_products(
                "www.okayamadenim.com", 20,
                collections=["new-restocks"], catalog=False)
        finally:
            tracker.http_get_json = orig

        self.assertEqual(len(calls), 1)
        self.assertIn("/collections/new-restocks/", calls[0])
        self.assertEqual([p["id"] for p in products], [10])
        self.assertEqual(catalog, [])


class AdaptiveRunTests(unittest.TestCase):
    def run_poll(self, normal, expanded=None, state=None, poll=None,
                 store=None, dry_run=False, send_error=False):
        cfg = {"stores": [{"name": "Test Store", "domain": "example.com",
                           "verified": True, **(store or {})}],
               "poll": {"products_per_store": 50, **(poll or {})},
               "filters": {}}
        if state is None:
            state = {"example.com": [str(i) for i in range(1, 51)],
                     "_poll_limit": 50}
        state = copy.deepcopy(state)
        stdout, stderr = io.StringIO(), io.StringIO()
        responses = [normal]
        if expanded is not None:
            responses.append(expanded)
        with ExitStack() as stack:
            fetch = stack.enter_context(patch.object(
                tracker, "fetch_products", side_effect=responses))
            stack.enter_context(patch.object(tracker, "load_json", return_value=state))
            save = stack.enter_context(patch.object(tracker, "save_json"))
            send = stack.enter_context(patch.object(
                tracker, "send_telegram",
                side_effect=RuntimeError("delivery failed") if send_error else None))
            stack.enter_context(patch.object(tracker, "process_telegram_commands",
                                            return_value=False))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            tracker.cmd_run(cfg, dry_run=dry_run)
        return fetch, send, save, state, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def batch(ids):
        products = [product(i, title=f"Product {i}") for i in ids]
        return products, products

    def test_sixty_new_products(self):
        result = self.run_poll(self.batch(range(101, 151)),
                               self.batch(list(range(101, 161)) + list(range(1, 51))))
        fetch, send, _, state, _, _ = result
        self.assertEqual([c.args[1] for c in fetch.call_args_list], [50, 250])
        self.assertEqual(send.call_count, 60)
        self.assertTrue(all(c.args[0].startswith("🆕") for c in send.call_args_list))
        self.assertEqual(len(state["example.com"]), 110)
        print("60-new-products: fetch limits=[50, 250], new alerts=60, suppressed=0")

    def test_older_inventory_after_anchor_seeds_silently(self):
        fetch, send, _, state, _, _ = self.run_poll(
            self.batch(range(101, 151)),
            self.batch(list(range(101, 161)) + [1, 70, 71]))
        self.assertEqual(send.call_count, 60)
        self.assertTrue({"70", "71"}.issubset(state["example.com"]))

    def test_no_anchor_default_cap(self):
        _, send, _, state, _, err = self.run_poll(
            self.batch(range(101, 151)), self.batch(range(101, 161)))
        messages = [c.args[0] for c in send.call_args_list]
        self.assertEqual(sum(m.startswith("🆕") for m in messages), 25)
        self.assertEqual(len(messages), 26)
        self.assertIn("Test Store: suppressed 35", messages[-1])
        self.assertTrue(all(str(i) in state["example.com"] for i in range(101, 161)))
        self.assertIn("no known product ID", err)
        # Suppression is permanent, not deferred to the next poll.
        self.assertEqual(self.run_poll(self.batch(range(101, 151)),
                                      state=state)[1].call_count, 0)
        print("no-anchor: eligible=60, new alerts=25, suppressed=35, summaries=1")

    def test_custom_and_zero_caps(self):
        for cap in [0, 3]:
            with self.subTest(cap=cap):
                _, send, _, _, _, _ = self.run_poll(
                    self.batch(range(101, 151)), self.batch(range(101, 161)),
                    poll={"max_alerts_per_store": cap})
                self.assertEqual(send.call_count, cap + 1)
                self.assertIn(f"suppressed {60-cap}", send.call_args.args[0])

    def test_filtering_before_cap(self):
        _, send, _, _, _, _ = self.run_poll(
            self.batch(range(101, 151)), self.batch(range(101, 161)),
            poll={"max_alerts_per_store": 3},
            store={"filters": {"include_keywords": ["Product 15"]}})
        self.assertEqual(send.call_count, 4)
        self.assertIn("suppressed 7", send.call_args.args[0])
        self.assertIn("Product 150", send.call_args_list[0].args[0])

    def test_first_run(self):
        fetch, send, _, state, _, err = self.run_poll(
            self.batch(range(101, 151)), state={})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1], 50)
        send.assert_not_called()
        self.assertEqual(len(state["example.com"]), 50)
        self.assertNotIn("no known product ID", err)

    def test_expansion_failure_uses_original_results(self):
        _, send, _, state, _, err = self.run_poll(
            self.batch(range(101, 151)), RuntimeError("fetch failed"))
        self.assertEqual(send.call_count, 26)
        self.assertIn("suppressed 25", send.call_args.args[0])
        self.assertIn("expanded fetch failed", err)
        self.assertTrue(all(str(i) in state["example.com"] for i in range(101, 151)))

    def test_large_configured_window_does_not_expand(self):
        for limit in [250, 300]:
            with self.subTest(limit=limit):
                fetch, send, _, _, _, _ = self.run_poll(
                    self.batch(range(101, 161)), poll={"products_per_store": limit})
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.args[1], limit)
                self.assertEqual(send.call_count, 26)

    def test_collection_only_no_anchor(self):
        normal = self.batch(range(101, 151))[0]
        expanded = self.batch(range(101, 161))[0]
        fetch, send, _, _, _, _ = self.run_poll(
            (normal, []), (expanded, []),
            store={"collections": ["new-restocks"], "catalog": False})
        self.assertEqual(send.call_count, 26)
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs, {"collections": ["new-restocks"],
                                          "catalog": False})

    def test_collection_items_and_restocks_not_seeded_by_merged_position(self):
        known = product(1)
        old = {"example.com": ["1"], "_poll_limit": 50,
               "_stock": {"example.com": {
                   "1": {"1": {"available": False, "title": "32"}}}}}
        catalog = [product(101), known, product(70)]
        # Collection-only new product comes after the known ID in merged order.
        merged = catalog + [product(999)]
        _, send, _, _, _, _ = self.run_poll(
            ([product(101)], [product(101)]), (merged, catalog),
            state=old, store={"collections": ["new-restocks"]},
            poll={"max_alerts_per_store": 0})
        messages = [c.args[0] for c in send.call_args_list]
        self.assertEqual(len(messages), 3)  # 101, restock 1, collection-only 999
        self.assertEqual(sum(m.startswith("♻️") for m in messages), 1)

    def test_collection_only_anchor_preserves_normal_comparison(self):
        old = {"example.com": ["1"], "_poll_limit": 50,
               "_stock": {"example.com": {
                   "1": {"1": {"available": False, "title": "32"}}}}}
        _, send, _, _, _, _ = self.run_poll(
            ([product(101)], []), ([product(1), product(101), product(999)], []),
            state=old, store={"collections": ["new-restocks"], "catalog": False})
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sum(c.args[0].startswith("♻️")
                             for c in send.call_args_list), 1)

    def test_configured_growth_keeps_existing_seed_behavior(self):
        fetch, send, _, state, _, _ = self.run_poll(
            self.batch([101] + list(range(1, 100))),
            poll={"products_per_store": 100})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn("99", state["example.com"])

    def test_dry_run(self):
        _, send, _, _, out, _ = self.run_poll(
            self.batch(range(101, 151)), self.batch(range(101, 161)), dry_run=True)
        send.assert_not_called()
        self.assertEqual(out.count("🆕"), 25)
        self.assertEqual(out.count("Test Store: suppressed 35"), 1)

    def test_invalid_cap_rejected_before_side_effects(self):
        for cap in [-1, True, 1.5, "25", None]:
            with self.subTest(cap=cap), patch.object(tracker, "load_json") as load, \
                    patch.object(tracker, "fetch_products") as fetch, \
                    patch.object(tracker, "save_json") as save, \
                    patch.object(tracker, "process_telegram_commands") as commands:
                with self.assertRaisesRegex(ValueError, "nonnegative integer"):
                    tracker.cmd_run({"poll": {"max_alerts_per_store": cap}})
                for mock in [load, fetch, save, commands]:
                    mock.assert_not_called()

    def test_delivery_failures_do_not_increase_suppression(self):
        _, send, _, _, _, err = self.run_poll(
            self.batch(range(101, 151)), self.batch(range(101, 161)), send_error=True)
        self.assertEqual(send.call_count, 26)
        self.assertIn("suppressed 35", send.call_args.args[0])
        self.assertIn("delivery failed", err)

    def test_below_cap_logs_without_summary(self):
        _, send, _, _, _, err = self.run_poll(self.batch([101]), self.batch([101]))
        self.assertEqual(send.call_count, 1)
        self.assertIn("no known product ID", err)


class NumericPriceTests(unittest.TestCase):
    def message(self, prices):
        return tracker.format_message("Store", "example.com",
                                      product(1, variants=[{"price": p} for p in prices]))

    def test_numeric_minimum(self):
        self.assertIn("\n$9.00\n", self.message(["9.00", "10.00"]))

    def test_invalid_and_valid(self):
        self.assertIn("\n$12.00\n", self.message(["abc", "12.00"]))

    def test_all_invalid_omits_line(self):
        msg = self.message(["abc", None, "", "NaN", "Infinity", "-Infinity", "12,50"])
        self.assertEqual(msg.splitlines(), ["🆕 Store", "Jeans", "OD",
                                           "https://example.com/products/jeans"])

    def test_grouping_and_original_display(self):
        self.assertIn("\n$1,234.5600\n", self.message(["1,234.5600", "2000"]))
        for symbol in ["$", "€", "¥", "£", "S$"]:
            with self.subTest(symbol=symbol):
                self.assertIn(f"\n{symbol}9.000\n",
                              self.message([symbol + "9.000", "10"]))

    def test_ambiguous_and_nonfinite_skipped(self):
        self.assertIn("\n$12.00\n",
                      self.message(["12,50", "NaN", "sNaN", "Infinity", "12.00"]))

    def test_numeric_zero_is_a_price(self):
        self.assertIn("\n$0\n", self.message([0, "12.00"]))


class StoreFilterConfigTests(unittest.TestCase):
    SHARED_FILTER_DOMAINS = (
        "www.neweracap.com",
        "hatclub.com",
        "culturekings.com",
    )

    def test_culture_kings_shares_new_era_and_hat_club_filters(self):
        cfg = tracker.load_json(tracker.CONFIG_PATH, None)
        by_domain = {store["domain"]: store for store in cfg["stores"]}
        expected = by_domain["www.neweracap.com"]["filters"]
        for domain in self.SHARED_FILTER_DOMAINS:
            self.assertEqual(
                by_domain[domain]["filters"],
                expected,
                f"{domain} should use the same filters as www.neweracap.com")

    def test_all_stores_exclude_socks(self):
        cfg = tracker.load_json(tracker.CONFIG_PATH, None)
        for store in cfg["stores"]:
            filters = tracker.resolve_filters(store, cfg["filters"])
            self.assertIn(
                "socks",
                filters.get("exclude_keywords", []),
                f"{store['domain']} should exclude socks")


class FilterRegressionTests(unittest.TestCase):
    def test_nonempty_globals_empty_and_false_overrides(self):
        global_filters = {"include_keywords": ["global"], "exclude_keywords": ["old"],
                          "include_product_types": ["Jeans"],
                          "notify_only_available": True}
        resolved = tracker.resolve_filters(
            {"filters": {"include_keywords": ["local"], "exclude_keywords": [],
                         "notify_only_available": False}}, global_filters)
        self.assertEqual(resolved, {"include_keywords": ["local"], "exclude_keywords": [],
                                    "include_product_types": ["Jeans"],
                                    "notify_only_available": False})
        self.assertEqual(global_filters["include_keywords"], ["global"])

    def test_preview_has_no_state_or_telegram_side_effects(self):
        cfg = {"stores": [{"name": "Store", "domain": "example.com"}]}
        for failed in [False, True]:
            with self.subTest(failed=failed), ExitStack() as stack:
                load = stack.enter_context(patch.object(tracker, "load_json", return_value=cfg))
                stack.enter_context(patch.object(
                    tracker, "fetch_products", return_value=([product(1)], []),
                    side_effect=RuntimeError("blocked") if failed else None))
                forbidden = [stack.enter_context(patch.object(tracker, name))
                             for name in ["save_json", "send_telegram", "telegram_api",
                                          "process_telegram_commands"]]
                stack.enter_context(patch.object(sys, "argv", ["tracker.py", "filters"]))
                stack.enter_context(patch.object(tracker.time, "sleep"))
                stack.enter_context(redirect_stdout(io.StringIO()))
                tracker.main()
                load.assert_called_once_with(tracker.CONFIG_PATH, None)
                for mock in forbidden:
                    mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
