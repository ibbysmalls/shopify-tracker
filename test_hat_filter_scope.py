"""Offline regressions for owner-specified per-store hat filter scope."""
import copy
import unittest
from unittest.mock import Mock

import tracker
from test_clothing_policy import CFG, product
from test_delivery import MemoryBackend
import test_delivery

SCOPED = {'hatclub.com': 'Hat Club', 'culturekings.com': 'Culture Kings US',
          'www.neweracap.com': 'New Era Cap'}
HOME = next(s for s in CFG['stores'] if s['domain'] == 'homegame-newyork.com')


def hat(title='New York Yankees Fitted Cap', vendor='New Era', available=True):
    return product(title, vendor, product_type='Hat', variants=[
        {'id':11,'title':'7 1/2','available':available,'price':'60'}])


class HatScopeTests(unittest.TestCase):
    def test_only_exact_three_configured_stores_have_team_lists(self):
        scoped = {s['domain']:s['name'] for s in CFG['stores']
                  if s.get('filters', {}).get('include_keywords')}
        self.assertEqual(scoped, SCOPED)
        self.assertNotIn('filters', HOME)

    def test_all_sources_use_own_filters_for_every_hat_brand_and_identity(self):
        for store in CFG['stores']:
            for title,vendor in [('Yankees Cap','New Era'),('New Era Yankees Hat','Unassigned'),
                                 ('Kapital x New Era Yankees Hat','Kapital'),('Rangers Hat','NEWERA'),
                                 ('Yankees Hat','Legacy Brand')]:
                p=hat(title,vendor)
                expected=tracker.passes_filters(p,tracker.resolve_filters(store,CFG['filters']))
                with self.subTest(store=store['domain'],title=title,vendor=vendor):
                    self.assertEqual(tracker.alert_decision(p,store,CFG,'new')['keep'],expected)
                    self.assertEqual(expected,store['domain'] not in SCOPED)

    def test_three_scoped_stores_keep_team_and_non_team_exclusions(self):
        for store in CFG['stores']:
            if store['domain'] not in SCOPED:continue
            for title,expected in [('49ers Cap',True),('San Francisco Giants Cap',True),
                                   ('Oakland Athletics Cap',True),('UCLA Cap',True),
                                   ('Yankees Cap',False),('Youth 49ers Cap',False),
                                   ('Athletics Department Cap',False),('Boston Bruins Cap',False)]:
                with self.subTest(store=store['domain'],title=title):
                    self.assertEqual(tracker.alert_decision(hat(title),store,CFG,'new')['keep'],expected)

    def test_scoped_stores_do_not_borrow_another_stores_terms(self):
        store=copy.deepcopy(next(s for s in CFG['stores'] if s['domain']=='hatclub.com'))
        store['filters']['include_keywords']=['yankees']
        self.assertTrue(tracker.alert_decision(hat('Yankees Cap'),store,CFG,'new')['keep'])
        self.assertFalse(tracker.alert_decision(hat('49ers Cap'),store,CFG,'new')['keep'])

    def test_other_store_keyword_type_and_stock_filters_are_preserved(self):
        store=dict(HOME,filters={'include_keywords':['exclusive'],'exclude_keywords':['youth'],
                                 'include_product_types':['hat']})
        self.assertTrue(tracker.alert_decision(hat('Exclusive Yankees Cap'),store,CFG,'new')['keep'])
        for p in [hat('Yankees Cap'),hat('Youth Exclusive Yankees Cap'),
                  dict(hat('Exclusive Yankees Cap'),product_type='Cap'),
                  hat('Exclusive Yankees Cap',available=False)]:
            self.assertFalse(tracker.alert_decision(p,store,CFG,'new')['keep'])
        self.assertFalse(tracker.alert_decision(hat('Socks Hat'),HOME,CFG,'new')['keep'])

    def test_display_names_and_domain_substrings_never_expand_scope(self):
        for domain in ['not-hatclub.com','hatclub.com.example.invalid','culturekings.com.example.invalid',
                       'www.neweracap.com.example.invalid','example.com']:
            for name in SCOPED.values():
                store={'domain':domain,'name':name,'verified':True}
                self.assertTrue(tracker.alert_decision(hat(),store,CFG,'new')['keep'])

    def test_stock_gate_remains_for_scoped_and_unscoped_stores(self):
        for store in CFG['stores']:
            p=hat('49ers Cap',available=False)
            with self.subTest(store=store['domain']):
                self.assertFalse(tracker.alert_decision(p,store,CFG,'new')['keep'])
                p['variants'].append({'id':12,'title':'7 5/8','available':True,'price':'60'})
                self.assertTrue(tracker.alert_decision(p,store,CFG,'new')['keep'])


class HatEventScopeTests(unittest.TestCase):
    run_tracker=test_delivery.TrackerDeliveryTests.run_tracker

    def test_new_dedup_and_later_restock_after_lost_history_preserve_scope(self):
        stores=[s for s in CFG['stores'] if s['domain'] in set(SCOPED)|{'homegame-newyork.com','www.myfitteds.com'}]
        self.assertEqual(len(stores),5)
        cases=[(s,'Yankees Cap',s['domain'] not in SCOPED) for s in stores]
        cases += [(s,'49ers Cap',True) for s in stores if s['domain'] in SCOPED]
        for store,title,allowed in cases:
            with self.subTest(store=store['domain'],title=title):
                backend=MemoryBackend()
                cfg={'stores':[store],'filters':CFG['filters'],'clothing_policy':CFG['clothing_policy'],
                     'new_era_policy':CFG['new_era_policy'],'poll':{'products_per_store':50}}
                original={store['domain']:['older-product'],'_poll_limit':50}
                p=hat(title)
                send=Mock(return_value=9)
                self.run_tracker(backend,copy.deepcopy(original),[p],send,cfg)
                self.run_tracker(backend,copy.deepcopy(original),[p],send,cfg)
                p['variants'][0]['available']=False
                self.run_tracker(backend,copy.deepcopy(original),[p],send,cfg)
                p['variants'][0]['available']=True
                self.run_tracker(backend,copy.deepcopy(original),[p],send,cfg)
                self.assertEqual(send.call_count,2 if allowed else 0)
                if allowed:
                    self.assertIn('Restocked',send.call_args.args[0])
                    self.assertEqual(len(backend.data['events']),2)

    def test_previously_filtered_known_homegame_hat_does_not_replay(self):
        p=hat()
        cfg={'stores':[HOME],'filters':CFG['filters'],'clothing_policy':CFG['clothing_policy'],
             'new_era_policy':CFG['new_era_policy'],'poll':{'products_per_store':50}}
        state={HOME['domain']:['1'],'_stock':{HOME['domain']:{'1':tracker.snapshot_variants(p)}},
               '_poll_limit':50}
        send=Mock(return_value=9)
        self.run_tracker(MemoryBackend(),state,[p],send,cfg)
        send.assert_not_called()


if __name__=='__main__':
    unittest.main()
