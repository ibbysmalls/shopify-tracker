"""Adversarial offline price recovery tests; no live state or network access."""
import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import delivery
import price_history as history
import tracker
from test_delivery import MemoryBackend
import test_delivery

POLICY = {'enabled': True, 'price_drop_percent': 15, 'price_drop_minimum': {'USD': 20}}
STORE = {'domain': 'example.com', 'name': 'Example', 'verified': True, 'currency': 'USD'}
CFG = {'stores': [STORE], 'poll': {'products_per_store': 50}, 'clothing_policy': POLICY}


def product(price='365', variants=None, **fields):
    p = {'id': 8133629509889, 'title': 'Synthetic Jeans', 'product_type': 'Jeans',
         'handle': 'synthetic-jeans', 'currency': 'USD',
         'variants': variants if variants is not None else
             [{'id': 11, 'title': 'M', 'available': True, 'price': str(price)}]}
    p.update(fields)
    return p


def observe(p, old=None, now=0, skip=False):
    with patch.object(tracker.time, 'time', return_value=now):
        return tracker.clothing_price_events([p], old, POLICY, skip_ids=[p['id']] if skip else [])


def record(state, vid='11'):
    entry = state[str(8133629509889)][history.META]['variants'][vid]
    return entry['contexts'][entry['active']]


def initial_state(price='365'):
    p = product(price)
    _, prices = observe(p)
    return {'example.com': [str(p['id'])], '_poll_limit': 50,
            '_stock': {'example.com': {str(p['id']): tracker.snapshot_variants(p)}},
            '_clothing_prices': {'example.com': prices}}


class PriceBaselineTests(unittest.TestCase):
    def test_recovered_roundtrips_and_further_reductions(self):
        for title, low, high in [('1101BK',365,521), ('0105BK',365,521),
                                  ('1101EC',335,479), ('Trash Panda',276,394)]:
            with self.subTest(title=title):
                _, state = observe(product(low))
                for t, price in enumerate([high,low,high,low]):
                    events, state = observe(product(price), state, t+1)
                    self.assertEqual(events, [])
                self.assertEqual(record(state)['baseline'], str(low))
                events, _ = observe(product(low-60), state, 10)
                self.assertEqual(len(events), 1)

    def test_trash_panda_original_discount_then_roundtrip(self):
        _, state = observe(product(396))
        events, state = observe(product(276), state, 1)
        self.assertEqual(len(events), 1)
        _, state = observe(product(394), state, 2)
        self.assertEqual(observe(product(276), state, 3)[0], [])

    def test_three_observations_under_24h_cannot_rearm(self):
        _, state = observe(product(365))
        for t in [1,2,86399]:
            _, state = observe(product(521), state, t)
        self.assertEqual(record(state)['baseline'], '365')
        self.assertEqual(observe(product(365), state, 86400)[0], [])

    def test_24h_with_only_two_observations_cannot_rearm(self):
        _, state = observe(product(365))
        for t in [1,86401]:
            _, state = observe(product(521), state, t)
        self.assertEqual(record(state)['baseline'], '365')
        self.assertEqual(observe(product(365), state, 86402)[0], [])

    def test_both_exact_boundaries_rearm_using_minimum_higher_price(self):
        _, state = observe(product(365))
        for t, price in [(1,521),(2,500),(86401,521)]:
            _, state = observe(product(price), state, t)
        self.assertEqual(record(state)['baseline'], '500')
        self.assertEqual(len(observe(product(365), state, 86402)[0]), 1)

    def test_duplicate_timestamp_is_not_additional_success(self):
        _, state = observe(product(365))
        for t in [1,1,86401]:
            _, state = observe(product(521), state, t)
        self.assertEqual(record(state)['baseline'], '365')

    def test_interrupted_sequence_restarts_clock_and_count(self):
        for interruption in [product(365), product('bad'), product(521,currency='EUR'),
                             product(521,currency=None), product(521, market='CA'), product(variants=[])]:
            with self.subTest(interruption=interruption):
                _, state = observe(product(365))
                _, state = observe(product(521), state, 1)
                _, state = observe(interruption, state, 86401)
                _, state = observe(product(521), state, 86402)
                _, state = observe(product(521), state, 86403)
                self.assertEqual(record(state)['baseline'], '365')
                self.assertEqual(observe(product(365), state, 86404)[0], [])

    def test_backwards_clock_restarts_rearming(self):
        _, state = observe(product(365))
        for t in [10,20,1,86411]:
            _, state = observe(product(521), state, t)
        self.assertEqual(record(state)['baseline'], '365')

    def test_unknown_or_conflicting_context_is_quarantined(self):
        for fields in [{'currency': None}, {'currency': 'UNK'}, {'currency': '$'},
                       {'currency_code': 'EUR'}, {'market': ''}]:
            p = product(**fields)
            _, state = observe(p)
            p['variants'][0]['price'] = '200'
            self.assertEqual(observe(p,state,1)[0], [])

    def test_changed_store_market_cannot_compare_old_prices(self):
        p = product()
        _, old = tracker.clothing_price_events([p],None,POLICY,dict(STORE,market='US'))
        self.assertEqual(tracker.clothing_price_events([product(200)],old,POLICY,
                                                     dict(STORE,market='CA'))[0], [])

    def test_numeric_normalization_and_trivial_drops_accumulate(self):
        _, state = observe(product('1,000.00'))
        for price in ['$1,000', '1000.000', '999', '980']:
            events, state = observe(product(price), state, 1)
            self.assertEqual(events, [])
        events, state = observe(product(800), state, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(record(state)['baseline'], '800')

    def test_reordered_different_variants_do_not_contaminate_each_other(self):
        def p(a,b,reverse=False):
            variants = [{'id':11,'title':'M','price':str(a)}, {'id':12,'title':'L','price':str(b)}]
            return product(variants=list(reversed(variants)) if reverse else variants)
        _, state = observe(p(365,600))
        events, state = observe(p(521,400,True),state,1)
        self.assertEqual([d['variant_id'] for d in events[0][2]], ['12'])
        self.assertEqual(record(state,'11')['baseline'], '365')
        self.assertEqual(record(state,'12')['baseline'], '400')
        self.assertEqual(observe(p(365,400),state,2)[0], [])
        events, _ = observe(p(280,400,True),state,3)
        self.assertEqual([d['variant_id'] for d in events[0][2]], ['11'])

    def test_duplicate_variant_identity_is_quarantined(self):
        _, state = observe(product())
        p = product(variants=[{'id':11,'title':'M','price':'200'}, {'id':11,'title':'L','price':'100'}])
        self.assertEqual(observe(p,state,1)[0], [])

    def test_small_drop_needs_both_unchanged_thresholds(self):
        for start, end, expected in [(100,85,False),(1000,980,False),(100,80,True)]:
            _, state = observe(product(start))
            self.assertEqual(bool(observe(product(end),state,1)[0]), expected)

    def test_legacy_migration_silently_seeds_no_fabricated_drop(self):
        legacy = {str(product()['id']): {'11':'521'}}
        events, state = observe(product(365),legacy,1)
        self.assertEqual(events, [])
        self.assertEqual(record(state)['baseline'], '365')
        self.assertEqual(observe(product(365),state,2)[0], [])
        self.assertEqual(len(observe(product(280),state,3)[0]),1)

    def test_restock_advertisement_small_reduction_updates_only_its_variant(self):
        p = product(variants=[{'id':11,'title':'M','price':'100'},
                              {'id':12,'title':'L','price':'200'}])
        _, state = observe(p)
        p['variants'][0]['price'] = '95'
        events,state = observe(p,state,1,skip=True)
        self.assertEqual(events,[])
        self.assertEqual(record(state,'11')['baseline'],'95')
        self.assertEqual(record(state,'12')['baseline'],'200')
        p['variants'][0]['price'] = '80'
        self.assertEqual(observe(p,state,2)[0],[])

    def test_superseded_pending_reservation_cannot_reactivate_after_rearm(self):
        p = product(521)
        _, state = observe(p)
        events,state = observe(product(365),state,1)
        prices=state[str(p['id'])]
        history.reserve(prices,events[0][2],'old')
        payload={'kind':'price_drop','product':product(365),'extra':events[0][2]}
        for now in [2,3,86402]:
            _, state = observe(product(521),state,now)
        self.assertEqual(record(state)['baseline'],'521')
        self.assertEqual(history.eligible(state[str(p['id'])],payload,'old',POLICY,STORE,
                                          tracker.numeric_price),[])
        newer,_=observe(product(365),state,86403)
        self.assertEqual(len(newer),1)
        self.assertEqual(newer[0][2][0]['generation'],1)

    def test_context_reentry_restock_small_reduction_uses_advertised_reference(self):
        _,state=observe(product(100))
        _,state=observe(product(200,currency='EUR'),state,1)
        _,state=observe(product(95),state,2,skip=True)
        self.assertEqual(record(state)['baseline'],'95')
        self.assertEqual(observe(product(80),state,3)[0],[])

    def test_legacy_higher_scalar_cannot_fake_first_return_to_unknown_advertised_low(self):
        legacy={str(product()['id']): {'11':'521'}}
        _,state=observe(product(521),legacy,1)
        self.assertTrue(record(state)['unproven'])
        events,state=observe(product(365),state,2)
        self.assertEqual(events,[])
        self.assertNotIn('unproven',record(state))
        _,state=observe(product(521),state,3)
        self.assertEqual(observe(product(365),state,4)[0],[])
        self.assertEqual(len(observe(product(280),state,5)[0]),1)


class DurablePriceTests(unittest.TestCase):
    run_tracker = test_delivery.TrackerDeliveryTests.run_tracker

    def run_prices(self, backend, state, prices, send, times=None):
        for i, price in enumerate(prices):
            with patch.object(tracker.time,'time', return_value=(times or list(range(len(prices))))[i]):
                self.run_tracker(backend,copy.deepcopy(state),[product(price)],send,CFG)

    def test_roundtrip_and_further_drop_survive_every_main_history_loss(self):
        backend = MemoryBackend()
        send = Mock(return_value=7)
        state = initial_state(521)
        self.run_prices(backend,state,[365,521,365,280],send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(backend.data['events']), 2)

    def test_rearm_metadata_survives_disposable_runners(self):
        backend = MemoryBackend()
        send = Mock(return_value=7)
        self.run_prices(backend,initial_state(),[521,521,521,365],send,[1,2,86401,86402])
        send.assert_called_once()

    def test_failed_fetch_interrupt_is_durable(self):
        backend = MemoryBackend()
        send = Mock(return_value=7)
        state = initial_state()
        self.run_prices(backend,state,[521,521],send,[1,2])
        self.run_tracker(backend,copy.deepcopy(state),[],send,CFG,
                         fetch=Mock(side_effect=RuntimeError('offline failure')))
        self.run_prices(backend,state,[521,365],send,[86401,86402])
        send.assert_not_called()

    def queued(self, price, status='pending', guard=False):
        backend = MemoryBackend()
        state = initial_state()
        box = delivery.Outbox(backend)
        tracker.migrate_price_history(box,state,CFG)
        payload = {'domain':'example.com','kind':'price_drop','product':product(price),
                   'extra':[{'variant':'M','from':'521','to':str(price)}]}
        box.queue('legacy',payload)
        box.data['events']['legacy']['status'] = status
        box.checkpoint()
        box.close()
        return backend,state

    def test_stale_pending_equal_higher_and_below_threshold_are_suppressed(self):
        for price in [365,394,340]:
            for status in ['pending','sending']:
                with self.subTest(price=price,status=status):
                    backend,state = self.queued(price,status)
                    send = Mock(return_value=9)
                    self.run_tracker(backend,state,[],send,CFG)
                    send.assert_not_called()
                    event = backend.data['events']['legacy']
                    self.assertEqual(event['status'],status)
                    self.assertIn('suppressed',event)
                    self.assertNotIn('message_id',event)

    def test_historical_payload_eligible_further_reduction_retained(self):
        backend,state = self.queued(280)
        send = Mock(return_value=9)
        self.run_tracker(backend,state,[],send,CFG)
        send.assert_called_once()
        self.assertEqual(backend.data['events']['legacy']['message_id'],9)

    def test_lower_historical_payload_migration_preserves_next_genuine_drop(self):
        backend=MemoryBackend()
        state=initial_state(521)
        state['_clothing_prices']['example.com']={str(product()['id']):{'11':'521'}}
        backend.data['events']['legacy']={'status':'pending','payload':{
            'domain':'example.com','kind':'price_drop','product':product(365),
            'extra':[{'variant':'M','from':'521','to':'365'}]}}
        send=Mock(return_value=9)
        self.run_tracker(backend,copy.deepcopy(state),[],send,CFG)
        send.assert_not_called()
        self.assertIn('suppressed',backend.data['events']['legacy'])
        self.run_tracker(backend,copy.deepcopy(state),[product(280)],send,CFG)
        send.assert_called_once()
        self.assertIn('365',send.call_args.args[0])

    def test_newer_durable_metadata_wins_repeated_stale_migration(self):
        backend,state = self.queued(280)
        self.run_tracker(backend,state,[],Mock(return_value=9),CFG)
        prices = copy.deepcopy(backend.data['observations']['example.com'][str(product()['id'])]['prices'])
        receipts = copy.deepcopy(backend.data['events'])
        box = delivery.Outbox(backend)
        for _ in range(3):
            tracker.migrate_price_history(box,initial_state(521),CFG)
        self.assertEqual(box.data['observations']['example.com'][str(product()['id'])]['prices'], prices)
        self.assertEqual(box.data['events'],receipts)
        box.close()

    def test_stale_observation_cannot_overwrite_newer_confirmed_checkpoint(self):
        backend,state = self.queued(280)
        stale = copy.deepcopy(backend.data['observations']['example.com'])
        self.run_tracker(backend,state,[],Mock(return_value=9),CFG)
        latest = copy.deepcopy(backend.data['observations']['example.com'][str(product()['id'])]['prices'])
        box = delivery.Outbox(backend)
        box.queue_many([],domain='example.com',observations=stale)
        self.assertEqual(box.data['observations']['example.com'][str(product()['id'])]['prices'],latest)
        box.close()

    def test_stale_cursor_cannot_rollback_stock_or_sequence_with_newer_price_metadata(self):
        backend,state = self.queued(280)
        pid=str(product()['id'])
        stale=copy.deepcopy(backend.data['observations']['example.com'])
        box=delivery.Outbox(backend)
        cursor=copy.deepcopy(stale[pid])
        cursor.update(stock={'11':{'available':False}},next_sequence=2)
        cursor['_revision'] += 1
        cursor['prices'][history.META]['revision'] += 1
        box.queue_many([],domain='example.com',observations={pid:cursor})
        box.queue_many([],domain='example.com',observations=stale)
        self.assertEqual(box.data['observations']['example.com'][pid],cursor)
        # Even an older caller with no cursor revision cannot roll back stock.
        stale[pid].pop('_revision',None)
        box.queue_many([],domain='example.com',observations=stale)
        self.assertEqual(box.data['observations']['example.com'][pid],cursor)
        box.close()

    def test_rejected_stale_observation_event_cannot_deliver_under_fresh_key(self):
        backend=MemoryBackend()
        box=delivery.Outbox(backend)
        state=initial_state(521)
        tracker.migrate_price_history(box,state,CFG)
        pid=str(product()['id'])
        original=copy.deepcopy(box.data['observations']['example.com'][pid])
        events,stale_prices=observe(product(280),{pid:original['prices']},1)
        stale=copy.deepcopy(original)
        stale['prices']=stale_prices[pid]
        stale['_revision']+=1
        history.reserve(stale['prices'],events[0][2],'stale')
        _,new_prices=observe(product(365),{pid:original['prices']},2)
        newer=copy.deepcopy(original)
        newer.update(prices=new_prices[pid],_revision=stale['_revision']+1)
        box.queue_many([],domain='example.com',observations={pid:newer})
        box.queue_many([('stale',{'domain':'example.com','kind':'price_drop',
                                 'product':product(280),'extra':events[0][2]})],
                       domain='example.com',observations={pid:stale})
        with patch.object(tracker,'send_telegram') as send:
            self.assertFalse(tracker.deliver_event(box,'stale',CFG))
            send.assert_not_called()
        self.assertEqual(box.data['observations']['example.com'][pid],newer)
        box.close()

    def test_receipts_byte_equivalent_during_legacy_migration(self):
        backend = MemoryBackend()
        backend.data['events'] = {'sent':{'status':'sent','message_id':7,'sent_at':10},
                                  'uncertain':{'status':'sending','retry_at':10**12,'payload':
                                      {'domain':'example.com','kind':'restock','product':product(),'extra':['M']}}}
        import json
        before = json.dumps(backend.data['events'],sort_keys=True)
        box = delivery.Outbox(backend)
        state = initial_state()
        state['_clothing_prices'] = {'example.com': {str(product()['id']):{'11':'365'}}}
        tracker.migrate_price_history(box,state,CFG)
        after = json.dumps(box.data['events'],sort_keys=True)
        self.assertEqual(before,after)
        saved = copy.deepcopy(box.data)
        tracker.migrate_price_history(box,state,CFG)
        self.assertEqual(box.data,saved)
        box.close()

    def test_checkpoint_failure_before_send_does_not_send_or_flush_on_close(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        tracker.migrate_price_history(box,initial_state(),CFG)
        box.queue('p',{'domain':'example.com','kind':'price_drop','product':product(280),
                       'extra':[{'variant':'M','from':'365','to':'280'}]})
        before = copy.deepcopy(backend.data)
        backend.write = Mock(side_effect=delivery.JournalError('offline checkpoint failure'))
        with patch.object(tracker,'send_telegram') as send:
            with self.assertRaises(delivery.JournalError):
                tracker.deliver_event(box,'p',CFG)
            send.assert_not_called()
        box.close()
        self.assertEqual(backend.data,before)
        backend.write.assert_called_once()

    def test_crash_after_pending_checkpoint_keeps_own_price_reservation_deliverable(self):
        backend = MemoryBackend()
        state = initial_state(521)
        send = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_tracker(backend,copy.deepcopy(state),[product(365)],send,CFG)
        event = next(iter(backend.data['events'].values()))
        self.assertEqual(event['status'],'sending')
        with patch.object(tracker.time,'time',return_value=10**12):
            self.run_tracker(backend,copy.deepcopy(state),[],Mock(return_value=9),CFG)
        self.assertEqual(next(iter(backend.data['events'].values()))['status'],'sent')

    def test_first_seed_is_durable_even_without_existing_event_or_cursor(self):
        backend = MemoryBackend()
        self.run_tracker(backend,{},[product()],Mock(return_value=9),CFG)
        self.assertIn(history.META,backend.data['observations']['example.com'][str(product()['id'])]['prices'])
        send = Mock(return_value=9)
        self.run_tracker(backend,{},[product(280)],send,CFG)
        send.assert_called_once()
        self.assertIn('Meaningful price drop',send.call_args.args[0])

    def test_disappearing_product_interrupts_higher_sequence_with_lost_main_state(self):
        backend = MemoryBackend()
        send = Mock(return_value=9)
        state = initial_state()
        self.run_prices(backend,state,[521,521],send,[1,2])
        self.run_tracker(backend,copy.deepcopy(state),[],send,CFG)
        self.run_prices(backend,state,[521,365],send,[86401,86402])
        send.assert_not_called()

    def test_own_pending_offer_is_stale_after_newer_price_observation(self):
        backend = MemoryBackend()
        send = Mock(side_effect=delivery.Rejected('offline'))
        state = initial_state(521)
        self.run_prices(backend,state,[365],send,[1])
        # Keep the pending send deferred while observing a different offer.
        self.run_prices(backend,state,[521],send,[2])
        send = Mock(return_value=9)
        self.run_prices(backend,state,[521],send,[1000])
        send.assert_not_called()
        self.assertIn('suppressed',next(iter(backend.data['events'].values())))

    def test_partial_variant_staleness_only_formats_eligible_variant(self):
        backend = MemoryBackend()
        state = initial_state()
        p = product(variants=[{'id':11,'title':'M','available':True,'price':'365'},
                              {'id':12,'title':'L','available':True,'price':'600'}])
        _, prices = observe(p)
        state['_clothing_prices']['example.com'] = prices
        box = delivery.Outbox(backend)
        tracker.migrate_price_history(box,state,CFG)
        payload = {'domain':'example.com','kind':'price_drop','product':p,
                   'extra':[{'variant':'M','from':'521','to':'365'},
                            {'variant':'L','from':'600','to':'400'}]}
        payload['product'] = copy.deepcopy(p)
        payload['product']['variants'][1]['price'] = '400'
        box.queue('partial',payload)
        with patch.object(tracker,'send_telegram',return_value=9) as send, redirect_stdout(io.StringIO()):
            tracker.deliver_event(box,'partial',CFG)
        self.assertIn('600',send.call_args.args[0])
        self.assertNotIn('521',send.call_args.args[0])
        box.close()

    def test_crash_immediately_after_atomic_queue_before_send(self):
        backend = MemoryBackend()
        write = backend.write
        def crash(data):
            write(data)
            if any(e['status'] == 'pending' for e in data['events'].values()):
                raise KeyboardInterrupt
        backend.write = crash
        send = Mock(return_value=9)
        with self.assertRaises(KeyboardInterrupt):
            self.run_tracker(backend,initial_state(521),[product(365)],send,CFG)
        send.assert_not_called()
        backend.write = write
        self.run_tracker(backend,initial_state(521),[],send,CFG)
        send.assert_called_once()

    def test_unknown_price_migration_never_suppresses_new_or_restock(self):
        backend = MemoryBackend()
        p = product(currency=None)
        state = initial_state()
        state['_clothing_prices']['example.com'] = {str(p['id']): {'11':'521'}}
        state['example.com'] = ['other']
        state['_stock'] = {}
        cfg = dict(CFG,stores=[dict(STORE,currency=None)])
        send = Mock(return_value=9)
        self.run_tracker(backend,state,[p],send,cfg)
        unavailable = copy.deepcopy(p)
        unavailable['variants'][0]['available'] = False
        self.run_tracker(backend,state,[unavailable],send,cfg)
        self.run_tracker(backend,state,[p],send,cfg)
        self.assertEqual(send.call_count,2)
        self.assertIn('restock',send.call_args.args[0].lower())


if __name__ == '__main__':
    unittest.main()
