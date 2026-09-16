#!/usr/bin/env python3
"""Unit tests for restock detection and collection-aware polling."""

import io
import sys
import unittest

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

    def test_expanded_catalog_only_seeds_configured_window(self):
        """A 250-item adaptive catalog must not seed past products_per_store."""
        catalog = [product(i) for i in range(250)]
        self.assertEqual(tracker.window_seed_ids(catalog, 50, 50), set())
        growth = tracker.window_seed_ids(catalog, 20, 50)
        self.assertEqual(len(growth), 30)
        self.assertEqual(growth, {str(i) for i in range(20, 50)})
        self.assertNotIn("50", growth)
        self.assertNotIn("60", growth)


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

    def test_lowest_price_is_numeric_not_lexical(self):
        """'9.00' vs '10.00' must pick 9.00; string sort would pick 10.00."""
        p = product(10, title="Cap", variants=[
            variant(1, "S", True),
            variant(2, "M", True),
        ])
        p["variants"][0]["price"] = "10.00"
        p["variants"][1]["price"] = "9.00"
        msg = tracker.format_message("US", "us.com", p, currency="USD")
        self.assertIn("$9.00", msg)
        self.assertNotIn("$10.00", msg)


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


class AdaptiveFetchTests(unittest.TestCase):
    """50-window / 60-new: extra IDs must notify, first run must not expand."""

    DOMAIN = "adaptive.test"
    STORE = {
        "name": "Adaptive",
        "domain": "adaptive.test",
        "verified": True,
        "platform": "shopify",
    }

    def _cfg(self):
        return {
            "stores": [dict(self.STORE)],
            "poll": {"products_per_store": 50},
            "filters": {},
            "health": {"digest_every_days": 0},
        }

    def _run(self, state, catalog_for_limit):
        """Poll once with mocked fetch/state so seen.json is never touched."""
        fetch_limits = []
        saved = {}

        def fake_fetch(domain, limit, platform="shopify", collections=None,
                       catalog=True):
            fetch_limits.append(limit)
            items = list(catalog_for_limit(limit))
            return items, items

        def fake_load(path, default=None):
            if path == tracker.STATE_PATH:
                return state
            return default

        def fake_save(path, data):
            saved["path"] = path
            saved["data"] = data

        orig_fetch = tracker.fetch_products
        orig_load = tracker.load_json
        orig_save = tracker.save_json
        orig_sleep = tracker.time.sleep
        tracker.fetch_products = fake_fetch
        tracker.load_json = fake_load
        tracker.save_json = fake_save
        tracker.time.sleep = lambda *a, **k: None
        buf = io.StringIO()
        old_out = sys.stdout
        try:
            sys.stdout = buf
            tracker.cmd_run(self._cfg(), dry_run=True, poll_all=True)
        finally:
            sys.stdout = old_out
            tracker.fetch_products = orig_fetch
            tracker.load_json = orig_load
            tracker.save_json = orig_save
            tracker.time.sleep = orig_sleep
        return fetch_limits, saved.get("data") or {}, buf.getvalue()

    def test_sixty_new_products_notify_beyond_fifty_window(self):
        """All 50 in-window IDs are new → expand to 250 → extra 10 notify.

        Passing used_limit=250 into window_seed_ids used to seed catalog[50:]
        silently, persist those IDs as seen, and permanently suppress them.
        """
        new_products = [product(101 + i, title=f"Drop {i + 1}")
                        for i in range(60)]
        old_products = [product(i, title=f"Old {i}") for i in range(1, 51)]
        seen = [str(i) for i in range(1, 51)]
        prev_stock = {str(i): {"1": {"available": True, "title": "32"}}
                      for i in range(1, 51)}
        state = {
            self.DOMAIN: seen,
            "_poll_limit": 50,
            "_stock": {self.DOMAIN: prev_stock},
            "_last_poll": {},
        }

        def catalog_for_limit(limit):
            catalog = new_products + old_products
            return catalog[:limit]

        fetch_limits, saved, out = self._run(state, catalog_for_limit)
        self.assertEqual(fetch_limits, [50, 250])

        # Every new title is printed; the 10 past the 50-window must appear.
        for i in range(1, 61):
            self.assertIn(f"Drop {i}", out)
        extra_ids = [str(i) for i in range(151, 161)]
        persisted = saved.get(self.DOMAIN, [])
        for pid in extra_ids:
            self.assertIn(pid, persisted)

        # Reproducing the bug: extras were seeded and would never notify later.
        # They must have been treated as new on this poll (🆕 lines), not only
        # persisted.
        self.assertGreaterEqual(out.count("🆕 Adaptive"), 60)

    def test_first_run_does_not_expand(self):
        catalog = [product(i, title=f"Seed {i}") for i in range(1, 61)]
        state = {"_poll_limit": 50, "_last_poll": {}}

        def catalog_for_limit(limit):
            return catalog[:limit]

        fetch_limits, saved, out = self._run(state, catalog_for_limit)
        self.assertEqual(fetch_limits, [50])
        self.assertNotIn("🆕", out)
        self.assertIn("Seeded 1 store", out)
        self.assertEqual(saved.get(self.DOMAIN), [str(i) for i in range(1, 51)])


if __name__ == "__main__":
    unittest.main()
