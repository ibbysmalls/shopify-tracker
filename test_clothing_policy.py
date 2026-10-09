"""Offline policy and regression tests. All fit labels/values and owned tokens are synthetic."""
import copy
import io
import json
import pathlib
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import patch

import clothing_policy as policy
import tracker
import replay_policy


ROOT = pathlib.Path(__file__).resolve().parent
CFG = json.loads((ROOT / "stores.json").read_text())
POLICY = CFG["clothing_policy"]
STORE = {"name": "Example Clothing", "domain": "example.com"}
SYNTHETIC_PROFILE = {
    "fit": {"waist_range": [30, 32], "min_front_rise": 9.9, "min_thigh": 12.0,
            "min_knee": 7.0, "preferred_max_hem": 6.4,
            "min_acceptable_thigh": 11.2, "min_acceptable_front_rise": 8.9},
    "size_references": {"pants": ["S2", "31"], "jacket": ["J2"], "clothing": ["C2"]},
    "brand_size_references": {"Kapital": {"pants": [8], "jacket": [7], "clothing": [9]}},
}


def product(title="PBJ Extreme Slub Relaxed Straight Jeans", vendor="PBJ", price="250", **kwargs):
    p = {"id": 1, "title": title, "vendor": vendor, "product_type": "Jeans",
         "handle": "synthetic-item", "variants": [{"id": 11, "title": "S2", "available": True, "price": price}]}
    p.update(kwargs)
    return p


def chart(hem=7.5, thigh=12.2, rise=10.1, knee=7.3, size="S2", waist=31):
    return (f'<table><tr><th>Size (inches)</th><th>Waist</th><th>Front rise</th>'
            f'<th>Back rise</th><th>Thigh</th><th>Knee</th><th>Hem</th><th>Inseam</th></tr>'
            f'<tr><td>{size}</td><td>{waist}</td><td>{rise}</td><td>8.8</td><td>{thigh}</td>'
            f'<td>{knee}</td><td>{hem}</td><td>29</td></tr></table>')


class AliasTests(unittest.TestCase):
    def test_all_thirteen_brands_and_all_common_aliases(self):
        self.assertEqual(len(POLICY["core_brands"]), 13)
        self.assertEqual(set(POLICY["core_brands"]), set(policy.BRAND_ALIASES))
        for canonical, aliases in policy.BRAND_ALIASES.items():
            for alias in aliases:
                with self.subTest(brand=canonical, alias=alias):
                    self.assertEqual(policy.canonical_brand(alias.upper()), canonical)
                    self.assertEqual(policy.canonical_brand(alias.replace(" ", "-")), canonical)
                    p = product(title="Extreme Slub Relaxed Straight Jeans", vendor=alias)
                    self.assertTrue(tracker.alert_decision(p, STORE, CFG, "new")["keep"])

    def test_apostrophes_and_retailer_variations(self):
        for value in ["Studio D’Artisan", "Studio D'Artisan", "STUDIO D'ARTISAN & CO.", "Studio Dartisan"]:
            self.assertEqual(policy.canonical_brand(value), "Studio D'Artisan")
        for value in ["Graphzero", "GRAPH-ZERO", "graph zero"]:
            self.assertEqual(policy.canonical_brand(value), "Graph Zero")
        self.assertEqual(policy.canonical_brand("Pure Blue Japan (PBJ)"), "Pure Blue Japan")

    def test_aliases_are_tokens_not_substrings(self):
        for name in ["Levi's", "Iron Heart", "Target", "Monitaly", "Amazonia", "MOMOTAROSE"]:
            self.assertIsNone(policy.canonical_brand(name))

    def test_description_mentions_cannot_override_manufacturer(self):
        p = product("Generic Slub Jeans", vendor="Outside Brand", body_html="Similar to Pure Blue Japan")
        self.assertFalse(tracker.alert_decision(p, STORE, CFG, "new")["keep"])

    def test_title_inspiration_cannot_override_explicit_outside_vendor(self):
        p=product("Levi's Kapital-inspired Patchwork Jeans", vendor="Levi's")
        self.assertFalse(tracker.alert_decision(p, STORE, CFG, 'new')['keep'])

    def test_explicit_retailer_vendor_can_resolve_manufacturer_from_title(self):
        for vendor in ['OD','Okayama Denim','Okayamadenim']:
            self.assertTrue(tracker.alert_decision(product(vendor=vendor),STORE,CFG,'new')['keep'])

    def test_new_era_variants(self):
        for value in ["New Era", "NEWERA", "New-Era", "New Era Cap", "New Era®"]:
            self.assertEqual(policy.canonical_brand(value), "New Era")


class RelevanceTests(unittest.TestCase):
    def decision(self, p, kind="new", extra=None, context=None):
        return tracker.alert_decision(p, STORE, CFG, kind, extra, SYNTHETIC_PROFILE if context is None else context)

    def test_outside_brands_suppressed_even_when_special(self):
        for brand in ["Iron Heart", "Levi's", "The Flat Head", "Warehouse", "Unbranded"]:
            self.assertFalse(self.decision(product("Natural Indigo Patchwork Jeans", brand))["keep"])

    def test_brand_new_rare_low_stock_not_enough(self):
        for text in ["PBJ Standard Straight Jeans", "PBJ New Release Straight Jeans",
                     "PBJ Rare Limited Edition Jeans", "PBJ Low Stock Jeans", "PBJ Sale Jeans"]:
            self.assertFalse(self.decision(product(text))["keep"])

    def test_special_fabric_with_trigger_kept(self):
        for text in ["PBJ Extreme Slub Relaxed Straight Jeans", "Kapital Boro Haori",
                     "FDMTL Jacquard Coverall", "Graphzero Dobby Work Jacket"]:
            self.assertTrue(self.decision(product(text, vendor="", product_type=""))["keep"])

    def test_named_collaboration_and_dye_pair_distinguished(self):
        self.assertTrue(policy.title_collaboration(product("RGT x Example Shop Sashiko Type I Jacket")))
        self.assertTrue(policy.title_collaboration(product("OD+PBJ Indigo Jeans")))
        self.assertFalse(policy.title_collaboration(product("PBJ Indigo x Sumi Jeans")))
        self.assertFalse(policy.title_collaboration(product("Samurai Black x Black Jeans")))
        self.assertTrue(self.decision(product("RGT x Example Shop Sashiko Type I Jacket", "RGT", product_type="Jacket"))["keep"])

    def test_marketplace_uses_same_bar_and_preserves_listing_url(self):
        p = product("Kapital Rare Plain Jeans", "Kapital", _url="https://example-market.invalid/listing/1")
        self.assertFalse(self.decision(p)["keep"])
        p["title"] = "Kapital Discontinued Boro Patchwork Jeans"
        d = self.decision(p)
        self.assertTrue(d["keep"])
        self.assertIn(p["_url"], tracker.format_clothing_message(STORE, p, "new", None, d))

    def test_standard_trucker_more_selective_but_not_excluded(self):
        for title in ["RGT Indigo Sashiko Type I Jacket", "RGT Standard Type II Jacket", "RGT Black Sashiko Trucker Jacket"]:
            self.assertFalse(self.decision(product(title, "RGT", product_type="Jacket"))["keep"])
        for title in ["RGT Kakishibu Sashiko Type I Jacket", "Kapital Patchwork Type II Jacket", "RGT Sashiko Type I Collaboration Jacket"]:
            self.assertTrue(self.decision(product(title, "RGT", product_type="Jacket"))["keep"])

    def test_overlapping_owned_interesting_jacket_can_qualify_on_material_deal(self):
        p = product("RGT Synthetic-Owned-Model Indigo Sashiko Type I Jacket", "RGT", product_type="Jacket")
        d = self.decision(p, "price_drop", [{"variant": "J2", "from": "400", "to": "300"}],
                          {"owned_models": ["Synthetic-Owned-Model"]})
        self.assertTrue(d["keep"])
        self.assertTrue(d["owned"])

    def test_fitted_shirts_are_clothing_not_hats(self):
        p = product("Outside Brand Fitted Linen Shirt", "Outside", product_type="Shirt")
        d = self.decision(p)
        self.assertTrue(d["applies"])
        self.assertFalse(d["keep"])

    def test_cap_sleeve_apparel_does_not_bypass_clothing_gate(self):
        for ptype in ['T-Shirt','']:
            p=product('Outside Brand Cap Sleeve T-Shirt','Outside',product_type=ptype)
            d=self.decision(p)
            self.assertTrue(d['applies'])
            self.assertFalse(d['keep'])

    def test_hat_words_in_garment_types_do_not_bypass_clothing_gate(self):
        for ptype in ['Fitted Shirt','Cap Sleeve Tops']:
            d=self.decision(product('Outside Brand Cotton Shirt','Outside',product_type=ptype))
            self.assertTrue(d['applies'])
            self.assertFalse(d['keep'])

    def test_sweats_apparel_type_is_filtered(self):
        self.assertFalse(self.decision(product('Outside Brand Fleece Set','Outside',product_type='Sweats'))['keep'])

    def test_tops_pullovers_and_henleys_cannot_bypass_brand_gate(self):
        for title, ptype in [("Ordinary Crewneck Pullover", "Pullover"), ("Cotton Henley", "Tops")]:
            self.assertFalse(self.decision(product(title, "Outside", product_type=ptype))["keep"])

    def test_black_denim_needs_more_than_normal_black(self):
        self.assertFalse(self.decision(product("Samurai Black Straight Jeans", "Samurai"))["keep"])
        self.assertTrue(self.decision(product("Samurai Extreme Slub Black Straight Jeans", "Samurai"))["keep"])

    def test_slim_penalty_does_not_create_absolute_exclusion(self):
        self.assertFalse(self.decision(product("PBJ Slub Skinny Jeans"))["keep"])
        self.assertTrue(self.decision(product("PBJ Natural Indigo Extreme Slub Skinny Jeans"))["keep"])

    def test_pants_use_actual_measurements_over_model_numbers(self):
        p = product("PBJ 019 Slub Jeans", body_html=chart())
        d = self.decision(p)
        self.assertTrue(d["keep"])
        self.assertTrue(d["fit"]["roomy"])

    def test_pbj_002_wide_hem_allowed_as_tailoring_candidate(self):
        p = product("PBJ XX-002 Slub Wide Straight Jeans", body_html=chart(hem=8.5))
        d = self.decision(p)
        self.assertTrue(d["keep"])
        self.assertTrue(any("tapering candidate" in reason for reason in d["reasons"]))

    def test_known_narrow_thigh_vetoes_roomy_and_tailoring_claim(self):
        p=product('PBJ 002 Slub Skinny Jeans',body_html=chart(thigh=10.5,rise=10.5,knee=7.6,hem=8.5))
        d=self.decision(p)
        self.assertFalse(d['fit']['roomy'])
        self.assertTrue(d['fit']['narrow'])
        self.assertFalse(d['keep'])
        self.assertNotIn('tapering candidate',' '.join(d['reasons']))

    def test_wide_model_no_chart_stays_discovery_with_fit_uncertain(self):
        d = self.decision(product("PBJ 002 Extreme Slub Wide Straight Jeans"))
        self.assertTrue(d["keep"])
        self.assertFalse(d["fit"]["roomy"])
        self.assertIn("model number is not fit proof", " ".join(d["reasons"]))

    def test_owned_same_eligibility_and_lower_purchase_context(self):
        p = product("PBJ Synthetic-Owned-Model Extreme Slub Jeans")
        plain = self.decision(p)
        owned = self.decision(p, context={"owned_models": ["Synthetic-Owned-Model"]})
        self.assertEqual(plain["keep"], owned["keep"])
        self.assertEqual(plain["score"], owned["score"])
        self.assertTrue(owned["owned"])
        self.assertIn("lower purchase priority", " ".join(owned["reasons"]))

    def test_ownership_is_not_unchanged_listing_dedupe(self):
        p = product("PBJ Synthetic-Owned-Model Extreme Slub Jeans")
        events, _, state = tracker.diff_store([p], ["1"], {"1": tracker.snapshot_variants(p)})
        self.assertEqual(events, [])
        events, _, _ = tracker.diff_store([dict(p, id=2)], ["1"], state)
        self.assertEqual(events[0][0], "new")
        self.assertTrue(self.decision(events[0][1], context={"owned_models": ["Synthetic-Owned-Model"]})["keep"])

    def test_catalogue_update_is_not_a_trigger(self):
        self.assertFalse(self.decision(product(), "page_updated")["keep"])
        self.assertFalse(self.decision(product(), "exists")["keep"])

    def test_restock_of_only_wrong_size_suppressed(self):
        self.assertFalse(self.decision(product(), "restock", ["30", "32"])["keep"])
        self.assertTrue(self.decision(product(), "restock", ["S2 / Indigo"])["keep"])

    def test_other_tagged_size_allowed_with_actual_matching_measurements(self):
        p = product(body_html=chart(size="S3", waist=31))
        self.assertTrue(self.decision(p, "restock", ["S3"])["keep"])

    def test_jacket_reference_and_synthetic_numeric_brand_label(self):
        for brand, sizes in [("Kapital", ["7"]), ("FDMTL", ["J2"])]:
            p = product(f"{brand} Boro Haori", brand, product_type="Jacket")
            self.assertTrue(self.decision(p, "restock", sizes)["keep"])
            self.assertFalse(self.decision(p, "restock", ["S"])["keep"])

    def test_reference_size_label_variations(self):
        self.assertTrue(self.decision(product(), "restock", ["Indigo / W31"])["keep"])
        p = product("FDMTL Boro Haori", "FDMTL", product_type="Jacket")
        for label in ["J2", "J2 / Indigo", "Indigo / J2"]:
            self.assertTrue(self.decision(p, "restock", [label])["keep"])

    def test_no_inventory_claim_from_api_or_chart(self):
        p = product(body_html=chart())
        d = self.decision(p, "restock", ["S2"])
        self.assertIsNone(d["size_available"])
        msg = tracker.format_clothing_message(STORE, p, "restock", ["S2"], d)
        self.assertIn("Live product-page inventory and personal size availability unverified", msg)
        self.assertNotIn("Restocked: S2", msg)
        self.assertIn("source https://example.com/products/synthetic-item", msg)

    def test_measurement_details_do_not_overflow_alert_delivery(self):
        p = product(body_html="".join(chart(size=i, waist=31) for i in range(30, 60)))
        d = self.decision(p)
        msg = tracker.format_clothing_message(STORE, p, "new", None, d)
        self.assertLess(len(msg), 4096)
        self.assertTrue(msg.endswith("https://example.com/products/synthetic-item"))

    def test_inventory_warning_survives_long_discount_details_and_title(self):
        p=product(title='PBJ Extreme Slub Jeans '+('Long description '*400))
        extra=[{'variant':str(i),'from':'250','to':'200'} for i in range(100)]
        d=self.decision(p,'price_drop',extra)
        msg=tracker.format_clothing_message(STORE,p,'price_drop',extra,d)
        self.assertLess(len(msg),4096)
        self.assertIn('Live product-page inventory and personal size availability unverified.',msg)

    def test_hats_and_unrelated_products_bypass_clothing_policy(self):
        for p in [product("Kapital Boro Cap", product_type="Cap"),
                  product("Monitor", "Electronics", product_type="Electronics"),
                  product("Leather Wallet", "Outside", product_type="Accessories")]:
            self.assertFalse(self.decision(p)["applies"])

    def test_existing_stock_and_exclude_safeguards_still_win(self):
        p = product(variants=[{"id": 11, "title": "S2", "available": False}])
        self.assertFalse(self.decision(p)["keep"])
        self.assertFalse(self.decision(product("PBJ Extreme Slub Socks", product_type="Clothing"))["keep"])


class MeasurementTests(unittest.TestCase):
    def test_empty_promotional_table_rows_do_not_abort_policy(self):
        for body in ['<table><tr></tr><tr><td>Promotional text</td></tr></table>',
                     '<table><tr><td>Details</td></tr><tr></tr></table>']:
            d=tracker.alert_decision(product(body_html=body),STORE,CFG,'new')
            self.assertTrue(d['keep'])
            self.assertEqual(d['measurements'],[])
    def test_both_orientations_and_correct_labels(self):
        result = policy.measurements(product(body_html=chart()), "source")
        self.assertEqual(result[0]["inches"]["rear_rise"], 8.8)
        self.assertEqual(result[0]["inches"]["front_rise"], 10.1)
        body = '<table><tr><td>Size (inches)</td><td>J2</td></tr>' + ''.join(
            f'<tr><td>{label}</td><td>{value}</td></tr>' for label, value in
            [('Chest', 25), ('Shoulder', 20), ('Body length', 28), ('Sleeve length', 25)]) + '</table>'
        result = policy.measurements(product(body_html=body), "source")
        self.assertEqual(set(result[0]["inches"]), {"chest", "shoulder", "body_length", "sleeve"})

    def test_centimetres_and_flat_waist_recorded(self):
        result = policy.measurements(product(body_html=chart(waist=15.5)))
        self.assertEqual(result[0]["inches"]["waist"], 31)
        self.assertEqual(result[0]["waist_method"], "flat doubled")
        result = policy.measurements(product(body_html=chart(waist=78.74).replace("inches", "cm")))
        self.assertAlmostEqual(result[0]["inches"]["waist"], 31)

    def test_unknown_or_mixed_units_not_guessed(self):
        for body in [chart().replace(" (inches)", ""), chart().replace("inches", "inches / cm")]:
            result = policy.measurements(product(body_html=body))
            self.assertEqual(result[0]["inches"], {})
            self.assertTrue(result[0]["raw"])

    def test_conflicts_retained_without_asserting_fit(self):
        d = tracker.alert_decision(product(body_html=chart()+chart(thigh=11.4)), STORE, CFG, "new", context=SYNTHETIC_PROFILE)
        self.assertTrue(d["fit"]["conflict"])
        self.assertFalse(d["fit"]["roomy"])
        self.assertEqual(len(d["measurements"]), 2)


class PriceTests(unittest.TestCase):
    def test_upgrade_seeds_silently_and_unchanged_dedupes(self):
        p = product()
        events, state = tracker.clothing_price_events([p], None, POLICY)
        self.assertEqual(events, [])
        self.assertEqual(tracker.clothing_price_events([p], state, POLICY)[0], [])

    def test_meaningful_discount_vs_trivial(self):
        _, state = tracker.clothing_price_events([product()], None, POLICY)
        self.assertEqual(tracker.clothing_price_events([product(price="240")], state, POLICY)[0], [])
        events, next_state = tracker.clothing_price_events([product(price="200")], state, POLICY)
        self.assertEqual(events[0][0], "price_drop")
        self.assertEqual(tracker.clothing_price_events([product(price="200")], next_state, POLICY)[0], [])

    def test_reference_variant_not_cheapest_variant(self):
        _, state = tracker.clothing_price_events([product()], None, POLICY)
        p = product(variants=[{"id": 12, "title": "30", "price": "100"}])
        self.assertEqual(tracker.clothing_price_events([p], state, POLICY)[0], [])

    def test_new_restock_seed_events_not_duplicated(self):
        _, state = tracker.clothing_price_events([product()], None, POLICY)
        self.assertEqual(tracker.clothing_price_events([product(price="200")], state, POLICY, skip_ids=[1])[0], [])

    def test_hat_prices_do_not_produce_events_or_history(self):
        p = product("New Era 49ers Fitted Cap", "New Era", product_type="Cap")
        self.assertEqual(tracker.clothing_price_events([p], None, POLICY), ([], {}))

    def test_outside_brand_clothing_has_no_price_history(self):
        p = product("Outside Brand Patchwork Jeans", "Outside")
        self.assertEqual(tracker.clothing_price_events([p], None, POLICY), ([], {}))

    def test_wrong_size_discount_is_suppressed(self):
        d = tracker.alert_decision(product(), STORE, CFG, "price_drop",
                                   [{"variant": "30", "from": "250", "to": "200"}], context=SYNTHETIC_PROFILE)
        self.assertFalse(d["keep"])

    def test_meaningful_price_drop_does_not_rescue_ordinary_product(self):
        d = tracker.alert_decision(product("PBJ Standard Straight Jeans"), STORE, CFG, "price_drop",
                                   [{"variant": "S2", "from": "250", "to": "200"}])
        self.assertFalse(d["keep"])

    def test_disabled_policy_is_noop(self):
        self.assertEqual(tracker.clothing_price_events([product()], None, {}), ([], None))


class HatRegressionTests(unittest.TestCase):
    def test_actual_team_lists_unchanged_and_source_already_present(self):
        expected = ["49ers", "san francisco giants", "sf giants", "oakland athletics", "oakland a's",
                    "athletics", "los angeles lakers", "lakers", "golden state",
                    "california golden bears", "cal bears", "ucla"]
        for store in CFG["stores"]:
            if store["domain"] in {"hatclub.com", "culturekings.com", "www.neweracap.com"}:
                self.assertEqual(store["filters"]["include_keywords"], expected)

    def test_every_store_preserves_non_new_era_and_qualifying_hat_decisions(self):
        for store in CFG["stores"]:
            for vendor in ["Legacy Brand", "New Era", "NEWERA", "New-Era"]:
                for title in ["49ers Fitted Cap", "SF Giants Fitted Cap", "UCLA Cap", "New York Yankees Fitted Cap",
                              "Generic Fashion Cap", "Youth 49ers Cap", "Athletics Department Cap", "Boston Bruins Cap"]:
                    p = product(title, vendor, product_type="Hat")
                    expected = tracker.passes_filters(p, tracker.resolve_filters(store, CFG["filters"]))
                    with self.subTest(store=store["name"], vendor=vendor, title=title):
                        d = tracker.alert_decision(p, store, CFG, "new")
                        self.assertFalse(d["applies"])
                        team_match = any(term in title.lower() for term in CFG['stores'][1]['filters']['include_keywords'])
                        self.assertEqual(d["keep"], expected and (vendor == "Legacy Brand" or team_match))

    def test_new_era_team_relevance_still_required_on_configured_sources(self):
        for store in CFG["stores"]:
            if store["domain"] not in {"hatclub.com", "culturekings.com", "www.neweracap.com"}:
                continue
            for vendor in ["New Era", "NEWERA", "New-Era"]:
                self.assertTrue(tracker.alert_decision(product("49ers Fitted Cap", vendor, product_type="Hat"), store, CFG, "new")["keep"])
                for title in ["Yankees Fitted Cap", "Generic Fashion Cap", "Youth 49ers Cap"]:
                    self.assertFalse(tracker.alert_decision(product(title, vendor, product_type="Hat"), store, CFG, "new")["keep"])

    def test_unfiltered_sources_now_gate_only_new_era_using_existing_terms(self):
        store = next(s for s in CFG['stores'] if s['name'] == 'MYFITTEDS')
        for vendor in ['New Era', 'NEWERA', 'New-Era', 'us-neweracap-production']:
            unrelated = product('Texas Rangers Fitted Cap', vendor, product_type='Hat')
            self.assertTrue(tracker.passes_filters(unrelated, tracker.resolve_filters(store, CFG['filters'])))
            self.assertFalse(tracker.alert_decision(unrelated, store, CFG, 'new')['keep'])
            qualifying = product('49ers Fitted Cap', vendor, product_type='Hat')
            self.assertTrue(tracker.alert_decision(qualifying, store, CFG, 'new')['keep'])
        legacy = product('Texas Rangers Fitted Cap', 'Other Brand', product_type='Hat')
        self.assertTrue(tracker.alert_decision(legacy, store, CFG, 'new')['keep'])

    def test_new_era_title_and_named_collaboration_cannot_bypass_gate(self):
        store = next(s for s in CFG['stores'] if s['name'] == 'MYFITTEDS')
        for title in ['New Era Yankees Hat', 'Kapital x New Era Yankees Hat']:
            self.assertFalse(tracker.alert_decision(product(title, 'Kapital', product_type='Hat'), store, CFG, 'new')['keep'])

    def test_new_era_headwear_type_cannot_bypass_gate(self):
        p=product('Yankees Adjustable','New Era',product_type='Headwear')
        self.assertFalse(tracker.alert_decision(p,STORE,CFG,'new')['keep'])
        p['vendor']='Legacy Brand'
        self.assertTrue(tracker.alert_decision(p,STORE,CFG,'new')['keep'])

    def test_new_era_missing_relevance_config_fails_closed(self):
        cfg = dict(CFG, stores=[])
        self.assertFalse(tracker.alert_decision(product('49ers Cap', 'New Era', product_type='Hat'), STORE, cfg, 'new')['keep'])


class IntegrationTests(unittest.TestCase):
    def test_wrong_size_restock_does_not_consume_reference_size_price_drop(self):
        p = product(price='200', variants=[{'id':11,'title':'S2','available':True,'price':'200'},
                                         {'id':12,'title':'30','available':True,'price':'250'}])
        old_stock = {'11':{'title':'S2','available':True},'12':{'title':'30','available':False}}
        state = {'example.com':['1'], '_stock':{'example.com':{'1':old_stock}},
                 '_clothing_prices':{'example.com':{'1':{'11':'250','12':'250'}}}}
        events,_,_=tracker.diff_store([p],['1'],{'1':old_stock})
        prices,_=tracker.additional_price_events([p],events,state['_clothing_prices']['example.com'],CFG,STORE,context=SYNTHETIC_PROFILE)
        self.assertEqual(events[0][0],'restock')
        self.assertEqual(prices[0][0],'price_drop')
        report=replay_policy.replay([{'store':STORE,'products':[p]}],CFG,state,context=SYNTHETIC_PROFILE)
        self.assertTrue(report['results'][0]['would_alert_now'])
        self.assertEqual(report['results'][0]['event'][0],'price_drop')

    def test_replay_includes_existing_material_price_event(self):
        p=product(price='200')
        state={'example.com':['1'],'_stock':{'example.com':{'1':tracker.snapshot_variants(p)}},
               '_clothing_prices':{'example.com':{'1':{'11':'250'}}}}
        report=replay_policy.replay([{'store':STORE,'products':[p]}],CFG,state)
        self.assertEqual(report['summary']['would_alert_now'],1)
        self.assertEqual(report['results'][0]['event'][0],'price_drop')
    def test_price_drop_runs_once_then_dedupes_without_resetting_existing_state(self):
        p = product(price="200")
        cfg = dict(CFG, stores=[dict(STORE, verified=True)])
        state = {"example.com": ["1"], "_stock": {"example.com": {"1": tracker.snapshot_variants(p)}},
                 "_last_poll": {}, "_clothing_prices": {"example.com": {"1": {"11": "250"}}}}
        original_stock = copy.deepcopy(state["_stock"])
        with ExitStack() as stack:
            stack.enter_context(patch.object(tracker, "load_json", return_value=state))
            stack.enter_context(patch.object(tracker, "fetch_products", return_value=([p], [p])))
            stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
            stack.enter_context(patch.object(tracker, "clothing_context", return_value={}))
            stack.enter_context(patch.object(tracker, "report_health"))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            send = stack.enter_context(patch.object(tracker, "send_telegram"))
            stack.enter_context(patch.object(tracker, "save_json"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            tracker.cmd_run(cfg)
            tracker.cmd_run(cfg)
        self.assertEqual(send.call_count, 1)
        self.assertIn("Meaningful price drop", send.call_args.args[0])
        self.assertEqual(state["_stock"], original_stock)
        self.assertEqual(state["example.com"], ["1"])
        self.assertEqual(state["_clothing_prices"]["example.com"]["1"]["11"], "200")

    def test_offline_replay_has_no_network_delivery_or_state_writes(self):
        state = {"example.com": ["1"], "_stock": {"example.com": {"1": tracker.snapshot_variants(product())}}}
        before = copy.deepcopy(state)
        cfg = copy.deepcopy(CFG)
        before_cfg = copy.deepcopy(cfg)
        with ExitStack() as stack:
            forbidden = [stack.enter_context(patch.object(tracker, n)) for n in
                         ["save_json", "send_telegram", "http_get_json", "fetch_products", "process_telegram_commands"]]
            report = replay_policy.replay([{"store": STORE, "products": [product()]}], cfg, state)
        self.assertEqual(report["summary"]["would_alert_now"], 0)
        self.assertEqual(state, before)
        self.assertEqual(cfg, before_cfg)
        for mock in forbidden:
            mock.assert_not_called()

    def test_run_routes_clothing_and_hat_and_preserves_other_history(self):
        clothing = product()
        hat = product("49ers Fitted Cap", "Legacy Brand", product_type="Hat", id=2)
        outside = product("Outside Brand Patchwork Jeans", "Outside", id=3)
        cfg = copy.deepcopy(CFG)
        cfg["stores"] = [dict(STORE, verified=True)]
        state = {"example.com": ["0"], "_stock": {"example.com": {"0": {}}}, "_last_poll": {}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(tracker, "load_json", return_value=state))
            stack.enter_context(patch.object(tracker, "fetch_products", return_value=([clothing,hat,outside],[clothing,hat,outside])))
            stack.enter_context(patch.object(tracker, "process_telegram_commands", return_value=False))
            stack.enter_context(patch.object(tracker, "clothing_context", return_value={}))
            stack.enter_context(patch.object(tracker, "report_health"))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            send = stack.enter_context(patch.object(tracker, "send_telegram"))
            save = stack.enter_context(patch.object(tracker, "save_json"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            tracker.cmd_run(cfg)
        self.assertEqual(send.call_count, 2)
        messages = [c.args[0] for c in send.call_args_list]
        self.assertIn("unverified", messages[0])
        self.assertEqual(messages[1], tracker.format_message(STORE["name"], STORE["domain"], hat))
        self.assertEqual(state["example.com"], ["1", "2", "3", "0"])
        self.assertIn("0", state["_stock"]["example.com"])
        self.assertNotIn("2", state["_clothing_prices"]["example.com"])
        save.assert_called_once()

    def test_filter_preview_remains_read_only_with_new_policy(self):
        cfg = dict(CFG, stores=[STORE])
        with ExitStack() as stack:
            stack.enter_context(patch.object(tracker, "fetch_products", return_value=([product()], [])))
            stack.enter_context(patch.object(tracker.time, "sleep"))
            forbidden = [stack.enter_context(patch.object(tracker, n)) for n in
                         ["save_json", "send_telegram", "telegram_api", "process_telegram_commands"]]
            stack.enter_context(redirect_stdout(io.StringIO()))
            tracker.cmd_filters(cfg)
        for mock in forbidden:
            mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
