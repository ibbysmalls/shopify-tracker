"""Offline delivery regressions; no credentials or live messages."""
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

import delivery
import tracker


class MemoryBackend:
    def __init__(self):
        self.data = {'version': 1, 'events': {}}
        self.writes = 0
    def read(self):
        return copy.deepcopy(self.data)
    def write(self, data):
        self.data = copy.deepcopy(data)
        self.writes += 1
    def close(self):
        pass


def fresh_outbox(*_):
    return delivery.Outbox(MemoryBackend())


class OutboxTests(unittest.TestCase):
    def test_success_survives_runner_loss_and_stale_history(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('event', {'public': 'catalogue'})
        self.assertTrue(box.deliver('event', lambda: 123))
        # Simulate SIGKILL, skipping close and losing seen.json entirely.
        with patch('delivery.time.time', return_value=10**12):
            restarted = delivery.Outbox(backend)
        send = Mock()
        restarted.queue('event', {'public': 'catalogue'})
        self.assertFalse(restarted.deliver('event', send))
        send.assert_not_called()
        self.assertEqual(backend.data['events']['event']['message_id'], 123)
        self.assertNotIn('payload', backend.data['events']['event'])

    def test_pending_checkpoint_survives_cancellation_before_send(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('event', {'product': 'public'})
        with patch('delivery.time.time', return_value=10**12):
            restarted = delivery.Outbox(backend)
        send = Mock(return_value=2)
        restarted.deliver('event', send)
        send.assert_called_once()

    def test_429_persists_global_retry_after_and_keeps_pending(self):
        backend = MemoryBackend()
        with patch('delivery.time.time', return_value=100):
            box = delivery.Outbox(backend)
            box.queue_many([('a', {}), ('b', {})])
            with self.assertRaises(delivery.RateLimited):
                box.deliver('a', Mock(side_effect=delivery.RateLimited(1200)))
            box.close()
        with patch('delivery.time.time', return_value=1000):
            restarted = delivery.Outbox(backend)
            for key in ['a', 'b']:
                send = Mock()
                with self.assertRaises(delivery.Deferred):
                    restarted.deliver(key, send)
                send.assert_not_called()
        self.assertEqual(backend.data['retry_at'], 1300)
        with patch('delivery.time.time', return_value=1301):
            self.assertTrue(restarted.deliver('a', lambda: 1))

    def test_ambiguous_transport_failure_is_retryable_after_cooldown(self):
        backend = MemoryBackend()
        with patch('delivery.time.time', return_value=100):
            box = delivery.Outbox(backend)
            box.queue('a', {})
            with self.assertRaises(delivery.Deferred):
                box.deliver('a', Mock(side_effect=TimeoutError('secret URL')))
            box.close()
        with patch('delivery.time.time', return_value=200):
            restarted = delivery.Outbox(backend)
            with self.assertRaises(delivery.Deferred):
                restarted.deliver('a', Mock())
        with patch('delivery.time.time', return_value=1001), redirect_stdout(io.StringIO()) as output:
            self.assertTrue(restarted.deliver('a', lambda: 3))
        self.assertIn('ambiguous', output.getvalue())

    def test_event_and_observation_are_one_atomic_checkpoint(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        before = backend.writes
        cursor = {'stock': {'v': {'available': True}}, 'next_sequence': 1}
        box.queue_many([('a', {'product': 'public'})], domain='example.com', observations={'p': cursor})
        self.assertEqual(backend.writes, before + 1)
        self.assertEqual(backend.data['events']['a']['status'], 'pending')
        self.assertEqual(backend.data['observations']['example.com']['p'], cursor)

    def test_explicit_rejection_remains_pending_and_retries(self):
        backend = MemoryBackend()
        with patch('delivery.time.time', return_value=100):
            box = delivery.Outbox(backend)
            box.queue('a', {})
            with self.assertRaises(delivery.Rejected):
                box.deliver('a', Mock(side_effect=delivery.Rejected('rejected')))
            self.assertEqual(backend.data['events']['a']['status'], 'pending')
        with patch('delivery.time.time', return_value=161):
            self.assertTrue(box.deliver('a', lambda: 2))

    def test_budget_preflight_stop_is_pending_not_an_ambiguous_send(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('a', {})
        with self.assertRaises(delivery.Deferred):
            box.deliver('a', Mock(side_effect=delivery.Deferred('request not started: budget')))
        self.assertEqual(backend.data['events']['a']['status'], 'pending')
        self.assertEqual(backend.data['events']['a'].get('retry_at', 0), 0)
        self.assertTrue(box.deliver('a', lambda: 123))

    def test_cancellation_during_send_is_ambiguous(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('a', {})
        with self.assertRaises(KeyboardInterrupt):
            box.deliver('a', Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(backend.data['events']['a']['status'], 'sending')

    def test_failed_checkpoint_prevents_send(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('a', {})
        send = Mock()
        backend.write = Mock(side_effect=delivery.JournalError('storage unavailable'))
        with self.assertRaises(delivery.JournalError):
            box.deliver('a', send)
        send.assert_not_called()

    def test_active_lease_blocks_concurrent_runner(self):
        backend = MemoryBackend()
        delivery.Outbox(backend)
        with self.assertRaisesRegex(delivery.JournalError, 'lease'):
            delivery.Outbox(backend)

    def test_local_lock_blocks_concurrent_process_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'outbox.json')
            first = delivery.LocalBackend(path)
            with self.assertRaises(delivery.JournalError):
                delivery.LocalBackend(path)
            first.close()
            delivery.LocalBackend(path).close()

    def test_soft_deadline_never_starts_send(self):
        box = fresh_outbox()
        box.queue('a', {})
        box.deadline = 0
        send = Mock()
        with self.assertRaises(delivery.Deferred):
            box.deliver('a', send)
        send.assert_not_called()
        self.assertEqual(box.data['events']['a']['status'], 'pending')

    def test_remote_cas_conflict_stops_without_automatic_overwrite(self):
        backend = object.__new__(delivery.GitHubBackend)
        backend.branch, backend.sha = 'delivery-state', 'old-sha'
        backend.request = Mock(side_effect=urllib.error.HTTPError('url', 409, 'Conflict', {}, None))
        with self.assertRaises(delivery.JournalError):
            backend.write({'version': 1, 'events': {}})
        backend.request.assert_called_once()
        self.assertEqual(backend.request.call_args.args[2]['sha'], 'old-sha')


class TrackerDeliveryTests(unittest.TestCase):
    def run_tracker(self, backend, state, products, send, cfg=None, fetch=None):
        cfg = cfg or {'stores': [{'domain': 'example.com', 'name': 'Example', 'verified': True}],
                      'poll': {'products_per_store': 50}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(delivery, 'open_outbox', side_effect=lambda _: delivery.Outbox(backend)))
            stack.enter_context(patch.object(tracker, 'load_json', return_value=state))
            stack.enter_context(patch.object(tracker, 'save_json'))
            stack.enter_context(patch.object(tracker, 'fetch_products', return_value=(products, products), side_effect=fetch))
            stack.enter_context(patch.object(tracker, 'send_telegram', send))
            stack.enter_context(patch.object(tracker, 'process_telegram_commands', return_value=False))
            stack.enter_context(patch.object(tracker, 'clothing_context', return_value={}))
            stack.enter_context(patch.object(tracker.time, 'sleep'))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            tracker.cmd_run(cfg)
        return output.getvalue()

    @staticmethod
    def product(available=True):
        return {'id': 2, 'title': 'Shirt', 'product_type': 'Shirt', 'handle': 'shirt', 'currency': 'USD',
                'variants': [{'id': 20, 'title': 'M', 'available': available, 'price': '50'}]}

    def test_failed_event_retries_even_after_seen_advanced_and_product_disappears(self):
        backend = MemoryBackend()
        state = {'example.com': ['1'], '_poll_limit': 50}
        with patch('delivery.time.time', return_value=100):
            self.run_tracker(backend, state, [self.product()], Mock(side_effect=delivery.RateLimited(600)))
        self.assertIn('2', state['example.com'])  # observation independent of durable delivery
        with patch('delivery.time.time', return_value=701):
            send = Mock(return_value=10)
            self.run_tracker(backend, state, [], send)
        send.assert_called_once()
        self.assertTrue(all(e['status'] == 'sent' for e in backend.data['events'].values()))

    def test_one_rejected_pending_event_does_not_block_other_pending_events(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        for pid in [2, 3]:
            p = dict(self.product(), id=pid)
            box.queue(str(pid), {'domain': 'example.com', 'kind': 'new', 'product': p, 'extra': []})
        box.close()
        send = Mock(side_effect=[delivery.Rejected('bad request'), 10])
        self.run_tracker(backend, {'example.com': ['1']}, [], send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(backend.data['events']['2']['status'], 'pending')
        self.assertEqual(backend.data['events']['3']['status'], 'sent')

    def test_recovery_deliveries_are_included_in_run_summary(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.queue('queued', {'domain': 'example.com', 'kind': 'new',
                             'product': self.product(), 'extra': None})
        box.close()
        output = self.run_tracker(backend, {'example.com': ['2']}, [], Mock(return_value=10))
        self.assertIn('sent 1 notification(s)', output)

    def test_confirmed_event_does_not_repeat_when_main_history_commit_lost(self):
        backend = MemoryBackend()
        old = {'example.com': ['1'], '_poll_limit': 50}
        send = Mock(return_value=10)
        self.run_tracker(backend, copy.deepcopy(old), [self.product()], send)
        self.run_tracker(backend, copy.deepcopy(old), [self.product()], send)
        send.assert_called_once()

    def test_lost_history_then_out_of_stock_then_genuine_restock_is_delivered(self):
        backend = MemoryBackend()
        unavailable = self.product(False)
        original = {'example.com': ['2'], '_poll_limit': 50,
                    '_stock': {'example.com': {'2': tracker.snapshot_variants(unavailable)}}}
        send = Mock(return_value=10)
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        # The send's history commit is lost. Even the subsequent out-of-stock
        # observation's history commit may be lost independently.
        self.run_tracker(backend, copy.deepcopy(original), [unavailable], send)
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(sum(e['status'] == 'sent' for e in backend.data['events'].values()), 2)

    def test_lost_history_unchanged_restock_is_not_replayed(self):
        backend = MemoryBackend()
        original = {'example.com': ['2'], '_poll_limit': 50,
                    '_stock': {'example.com': {'2': tracker.snapshot_variants(self.product(False))}}}
        send = Mock(return_value=10)
        for _ in range(3):
            self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        send.assert_called_once()

    def test_legacy_receipts_migrate_without_reset_or_future_restock_loss(self):
        backend = MemoryBackend()
        original = {'example.com': ['2'], '_poll_limit': 50,
                    '_stock': {'example.com': {'2': tracker.snapshot_variants(self.product(False))}}}
        send = Mock(return_value=10)
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        backend.data.pop('observations')  # deployed pre-observation journal schema
        original_receipts = copy.deepcopy(backend.data['events'])
        self.run_tracker(backend, copy.deepcopy(original), [self.product(False)], send)
        self.assertEqual(backend.data['events'], original_receipts)
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        self.assertEqual(send.call_count, 2)
        for key, receipt in original_receipts.items():
            self.assertEqual(backend.data['events'][key], receipt)

    def test_legacy_unchanged_restock_does_not_replay_during_migration(self):
        backend = MemoryBackend()
        original = {'example.com': ['2'], '_poll_limit': 50,
                    '_stock': {'example.com': {'2': tracker.snapshot_variants(self.product(False))}}}
        send = Mock(return_value=10)
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        backend.data.pop('observations')
        self.run_tracker(backend, copy.deepcopy(original), [self.product()], send)
        send.assert_called_once()

    def test_price_references_survive_lost_history_without_replay_or_lost_later_drop(self):
        backend = MemoryBackend()
        original = {'example.com': ['2'], '_poll_limit': 50,
                    '_stock': {'example.com': {'2': tracker.snapshot_variants(self.product())}},
                    '_clothing_prices': {'example.com': tracker.clothing_price_events(
                        [dict(self.product(), variants=[dict(self.product()['variants'][0], price='100')])],
                        None, {'enabled': True})[1]}}
        cfg = {'stores': [{'domain': 'example.com', 'name': 'Example', 'verified': True}],
               'poll': {'products_per_store': 50}, 'clothing_policy': {'enabled': True}}
        send = Mock(return_value=10)
        for price in ['50', '50', '100', '50', '25']:
            p = self.product()
            p['variants'][0]['price'] = price
            self.run_tracker(backend, copy.deepcopy(original), [p], send, cfg)
        self.assertEqual(send.call_count, 2)
        self.assertTrue(all('Meaningful price drop' in call.args[0] for call in send.call_args_list))

    def test_later_genuine_restock_gets_new_event_after_success(self):
        backend = MemoryBackend()
        state = {'example.com': ['1'], '_poll_limit': 50}
        send = Mock(return_value=10)
        self.run_tracker(backend, state, [self.product()], send)
        self.run_tracker(backend, state, [self.product(False)], send)
        self.run_tracker(backend, state, [self.product()], send)
        self.assertEqual(send.call_count, 2)
        self.assertIn('Restocked', send.call_args.args[0])

    def test_send_parses_telegram_retry_after(self):
        error = urllib.error.HTTPError('secret-url', 429, 'Too Many Requests', {},
                                      io.BytesIO(b'{"parameters":{"retry_after":321}}'))
        with patch.dict(os.environ, TELEGRAM_BOT_TOKEN='synthetic', TELEGRAM_CHAT_ID='123'), \
                patch.object(tracker.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(delivery.RateLimited) as caught:
                tracker.send_telegram('offline test')
        self.assertEqual(caught.exception.seconds, 321)
        self.assertNotIn('secret-url', str(caught.exception))

    def test_budget_limited_polls_prioritize_previously_deferred_stores(self):
        state = {'a.example': ['1'], 'b.example': ['1']}
        cfg = {'stores': [{'domain': domain, 'name': domain, 'verified': True}
                          for domain in ['a.example', 'b.example']]}
        fetched = []
        for _ in range(2):
            box = fresh_outbox()
            def fetch(domain, *args, **kwargs):
                fetched.append(domain)
                box.deadline = 0  # this store used the remaining polling budget
                p = dict(self.product(), id=1)
                return [p], [p]
            with patch.object(tracker, 'load_json', return_value=state), \
                    patch.object(tracker, 'save_json'), patch.object(tracker, 'fetch_products', side_effect=fetch), \
                    patch.object(tracker, 'process_telegram_commands', return_value=False), \
                    patch.object(tracker, 'report_health'), patch.object(tracker.time, 'sleep'), \
                    redirect_stdout(io.StringIO()):
                tracker._cmd_run(cfg, outbox=box)
        self.assertEqual(fetched, ['a.example', 'b.example'])
        self.assertEqual(list(state['_last_poll']), ['a.example', 'b.example'])

    def test_budget_stop_mid_fetch_is_deferred_without_failure_or_poll_timestamp(self):
        state = {'example.com': ['1'], '_last_poll': {'example.com': 42}}
        cfg = {'stores': [{'domain': 'example.com', 'name': 'Example', 'verified': True}]}
        with patch.object(tracker, 'load_json', return_value=state), \
                patch.object(tracker, 'save_json'), \
                patch.object(tracker, 'fetch_products', side_effect=delivery.Deferred('budget')), \
                patch.object(tracker, 'process_telegram_commands', return_value=False), \
                patch.object(tracker, 'report_health') as health, redirect_stdout(io.StringIO()):
            tracker._cmd_run(cfg, outbox=fresh_outbox())
        self.assertEqual(state['_last_poll']['example.com'], 42)
        self.assertEqual(health.call_args.args[2], [])  # no failed store
        self.assertEqual(health.call_args.args[-1], ['Example'])

    def test_soft_deadline_defers_fetches_and_preserves_unpolled_history(self):
        backend = MemoryBackend()
        box = delivery.Outbox(backend)
        box.deadline = 0
        old = {'example.com': ['1'], '_stock': {'example.com': {'1': {}}}}
        cfg = {'stores': [{'domain': 'example.com', 'name': 'Example', 'verified': True}]}
        with patch.object(tracker, 'load_json', return_value=old), \
                patch.object(tracker, 'save_json'), patch.object(tracker, 'fetch_products') as fetch, \
                patch.object(tracker, 'process_telegram_commands', return_value=False), \
                redirect_stdout(io.StringIO()):
            tracker._cmd_run(cfg, outbox=box)
        fetch.assert_not_called()
        self.assertEqual(old['example.com'], ['1'])
        self.assertEqual(old['_stock']['example.com'], {'1': {}})
