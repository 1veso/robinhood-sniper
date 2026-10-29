"""Bounded provider work and durable deferral, with no external calls."""
from contextlib import contextmanager
from decimal import Decimal as D
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import explore as worker
from sniper.adapters.evm import RpcClient, RpcBudgetExceeded
from sniper.adapters.solana import request_json, request_budget, RequestBudgetExceeded, SourceError
from sniper.ledger import Ledger
from sniper.exploration import ExploratoryEngine
import test_comparison_worker as comparison_fixture
from test_comparison_worker import TOKEN, POOL, ASSET
from test_engine import NOW, quote
from test_paper_reentry import eligible


class EntryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = comparison_fixture.ComparisonWorkerTests(methodName='runTest')
        self.fixture.setUp()
        self.finished, self.proved, self.recorded = [], [], []
        self.original_time = NOW
        self.prove_hook = None

    def tearDown(self):
        self.fixture.tearDown()

    @contextmanager
    def offline(self):
        owner, fixture = self, self.fixture
        class Source:
            def __init__(self, rpc, store, market):
                self.rpc, self.store = rpc, store
                store.db.execute('CREATE TABLE IF NOT EXISTS exploration_outcomes(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            def pending(self):
                return [item for item in fixture.pending if not self.store.db.execute(
                    'SELECT 1 FROM exploration_outcomes WHERE event_id=?', (item['id'],)).fetchone()]
            def collect(self, **kwargs):
                fixture.collections.append(fixture.tick[0]); return {'sampled': 0}
            def prove(self, item, *, fallback_rpc=None):
                owner.proved.append(item['id'])
                if owner.prove_hook is not None:
                    owner.prove_hook(self.rpc, fallback_rpc)
                return {'token_address': TOKEN, 'pool': POOL, 'spent_usd': '100',
                        'wallet': 'wallet-a', 'tx_hash': 'observed-tx-a', 'observed_at': owner.original_time}
            def finish(self, item, result):
                owner.finished.append(result)
                self.store.db.execute('INSERT OR IGNORE INTO exploration_outcomes VALUES(?,?)',
                    (item['id'], json.dumps(result)))
        with fixture.offline(), patch.object(worker, 'ExploratorySource', Source):
            yield

    def image(self):
        return self.fixture.source_image()

    def seed_position(self, asset=ASSET):
        ledger, store, engine, _ = worker.open_trial(self.fixture.folder, self.fixture.policy, rules=self.fixture.rules)
        try:
            result = engine.consider(eligible('seed', asset), quote(asset=asset, pool_id=POOL), NOW)
            self.assertEqual(result['verdict'], 'PAPER_BUY')
        finally:
            ledger.close(); store.close()

    def test_slow_provider_defers_original_event_and_exits_publish_then_restart_once(self):
        other = 'eip155:4663/erc20:' + '33' * 20
        self.seed_position(other)
        fixture = self.fixture
        with self.offline():
            original = worker.GoPlusClient.screen.side_effect
            def slow_screen(token):
                fixture.tick[0] += worker.ENTRY_WORK_SECONDS
                return original(token)
            worker.GoPlusClient.screen.side_effect = slow_screen
            first = fixture.run_cycle()
        self.assertEqual(first['entry_work']['deferred'], 1)
        self.assertTrue(first['entry_work']['deadline_reached'])
        self.assertEqual(first['cycle_duration_seconds'], worker.ENTRY_WORK_SECONDS)
        self.assertEqual(len(first['fills']), 1)  # Only the seeded existing holding.
        self.assertEqual(self.finished, [])
        self.assertTrue((fixture.folder / 'report.html').exists())
        self.assertGreaterEqual(sum(bool(r.get('exit_only')) for r in fixture.routes), 2)
        self.assertFalse(any(not r.get('exit_only') for r in fixture.routes))
        fixture.tick[0] = NOW + 120
        with self.offline(): second = fixture.run_cycle()
        self.assertEqual(len(second['fills']), 2)
        self.assertEqual(self.finished[-1]['verdict'], 'PAPER_BUY')
        proof = next(json.loads(row[3]) for row in self.image()['events'] if row[1] == 'exploratory_wallet_buy')
        self.assertEqual(proof['observed_at'], NOW)
        fixture.tick[0] += 120
        with self.offline(): third = fixture.run_cycle()
        self.assertEqual(len(third['fills']), 2)
        self.assertEqual(len([r for r in self.finished if r.get('verdict') == 'PAPER_BUY']), 1)

    def test_already_open_keeps_funded_evidence_but_skips_entry_security_and_route(self):
        self.seed_position()
        with self.offline():
            screen = worker.GoPlusClient.screen
            result = self.fixture.run_cycle()
            screen.assert_not_called()
        self.assertEqual(len(self.proved), 1)
        self.assertEqual(result['entry_work']['cheap_rejections'], {'already_open': 1})
        self.assertTrue(all(r.get('exit_only') for r in self.fixture.routes))
        self.assertEqual(len(result['fills']), 1)
        self.assertTrue(any(row[1] == 'exploratory_wallet_buy' for row in self.image()['events']))

    def test_security_outage_is_pending_not_a_safety_rejection(self):
        with self.offline():
            worker.GoPlusClient.screen.side_effect = SourceError('fixture unavailable')
            result = self.fixture.run_cycle()
        self.assertEqual(result['decisions'], [])
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['entry_work']['deferred'], 1)
        self.assertEqual(self.finished, [])
        self.assertFalse(any(str(row[0]).startswith('safety_rejection:') for row in self.image()['metadata']))

    def test_final_canonical_recheck_cannot_admit_after_entry_deadline(self):
        with self.offline():
            original = worker.ConfirmationBook
            class SlowBook(original):
                def confirm(inner, *args):
                    result = super().confirm(*args)
                    self.fixture.tick[0] += worker.ENTRY_WORK_SECONDS
                    return result
            with patch.object(worker, 'ConfirmationBook', SlowBook):
                result = self.fixture.run_cycle()
        self.assertEqual(result['fills'], [])
        self.assertEqual(self.finished, [])
        self.assertEqual(result['entry_work']['deferred'], 1)

    def test_configured_historical_fallback_shares_proof_deadline_without_extra_time(self):
        (self.fixture.root / 'quote-rpc-url.txt').write_text('https://quote.fixture.invalid', encoding='utf-8')
        def inspect_deadlines(source_rpc, fallback_rpc):
            self.assertIsNotNone(fallback_rpc)
            self.assertIsNot(source_rpc, fallback_rpc)
            self.assertLess(abs(source_rpc._deadline - fallback_rpc._deadline), .1)
            self.fixture.tick[0] += 26
        self.prove_hook = inspect_deadlines
        with self.offline(): result = self.fixture.run_cycle()
        self.assertEqual(result['fills'], [])
        self.assertEqual(self.finished, [])
        self.assertEqual(result['entry_work']['deferred'], 1)
        self.assertEqual(result['stage_seconds']['funded_buy_proof'], 26)
        self.assertEqual(result['cycle_duration_seconds'], 26)

    def test_completion_audit_follows_successful_primary_publication(self):
        with self.offline(): result = self.fixture.run_cycle()
        records = [json.loads(row[3]) for row in self.image()['events'] if row[1] == 'exploratory_cycle_completed']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['primary_completed_at'], result['primary_completed_at'])
        self.assertIn('entry_quote', records[0]['stage_seconds'])
        self.assertIn('pending_maintenance', records[0]['stage_seconds'])
        self.assertIn('entry_local_overhead', records[0]['stage_seconds'])
        self.assertIn('financial_reporting', records[0]['stage_seconds'])
        self.assertEqual(len(result['fills']), 1)


class ProviderBudgetTests(unittest.TestCase):
    def response(self, tick, *, seconds=2, chunks=(b'{', b'"ok":true}', b'')):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        pieces = iter(chunks)
        def read(size):
            tick[0] += seconds
            return next(pieces)
        response.read1 = Mock(side_effect=read)
        response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Mock()))
        return response

    def test_http_slow_body_obeys_shared_absolute_budget_and_context_resets(self):
        tick = [100.0]
        response = self.response(tick)
        with patch('urllib.request.urlopen', return_value=response) as opened:
            with request_budget(103, clock=lambda: tick[0]):
                with self.assertRaises(RequestBudgetExceeded):
                    request_json('GET', 'https://fixture.invalid', None, {}, 10)
            self.assertEqual(opened.call_args.kwargs['timeout'], 3)
            self.assertEqual(response.read1.call_count, 2)
            self.assertEqual(response.fp.raw._sock.settimeout.call_args.args[0], 1)
            response.read.return_value = b'{"ok":true}'
            self.assertEqual(request_json('GET', 'https://fixture.invalid', None, {}, 10), {'ok': True})
            self.assertEqual(opened.call_args.kwargs['timeout'], 10)

    def test_rpc_slow_body_rechecks_remaining_budget_between_reads(self):
        tick = [100.0]
        response = self.response(tick)
        rpc = RpcClient(max_retries=0)
        with patch('sniper.adapters.evm.time.monotonic', side_effect=lambda: tick[0]), \
             patch('urllib.request.urlopen', return_value=response) as opened:
            with rpc.budget(3):
                with self.assertRaises(RpcBudgetExceeded): rpc.call('eth_blockNumber')
        self.assertEqual(opened.call_args.kwargs['timeout'], 3)
        self.assertEqual(response.read1.call_count, 2)
        self.assertIsNone(rpc._deadline)


class AccountPreflightTests(unittest.TestCase):
    def test_cash_exhaustion_is_a_definite_account_blocker(self):
        ledger = Ledger(':memory:', D(1000))
        policy, _, _ = worker.profile_settings('two-wallet-30k-reentry')
        engine = ExploratoryEngine(ledger, policy)
        try:
            with ledger.transaction(): ledger._set('cash', '0')
            self.assertEqual(worker.entry_account_blocker(engine, ASSET, NOW), 'risk_or_cash_budget_exhausted')
            self.assertEqual(ledger.snapshot()['open_positions'], 0)
        finally:
            ledger.close()

    def test_stale_holdings_keep_stress_exposure_and_do_not_free_entry_risk(self):
        ledger = Ledger(':memory:', D(1000))
        policy, _, _ = worker.profile_settings('two-wallet-30k-reentry')
        engine = ExploratoryEngine(ledger, policy)
        try:
            for i in range(4):
                asset = 'eip155:4663:held' + str(i)
                self.assertEqual(engine.consider(eligible(str(i), asset), quote(asset), NOW)['verdict'], 'PAPER_BUY')
            before = ledger.snapshot()
            now = NOW + policy.max_quote_age_seconds + 1
            self.assertEqual(worker.entry_account_blocker(engine, ASSET, now), 'risk_or_cash_budget_exhausted')
            after = ledger.snapshot()
            self.assertEqual(after['stress_exposure'], before['stress_exposure'])
            self.assertEqual(after['open_positions'], 4)
            self.assertEqual(after['unresolved_positions'], 4)
            self.assertLessEqual(worker.affordable_input_ceiling(policy, after), D('.01'))
        finally:
            ledger.close()


if __name__ == '__main__':
    unittest.main()
