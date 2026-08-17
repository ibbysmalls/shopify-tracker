#!/usr/bin/env python3
"""Unit tests for restock detection and collection-aware polling."""

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
            products = tracker.fetch_shopify_products(
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

    def test_catalog_false_skips_storewide(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            return {"products": [product(10)]}

        orig = tracker.http_get_json
        tracker.http_get_json = fake_get
        try:
            tracker.fetch_shopify_products(
                "www.okayamadenim.com", 20,
                collections=["new-restocks"], catalog=False)
        finally:
            tracker.http_get_json = orig

        self.assertEqual(len(calls), 1)
        self.assertIn("/collections/new-restocks/", calls[0])


if __name__ == "__main__":
    unittest.main()
