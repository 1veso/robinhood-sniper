"""Real worker/book/entry-engine integration with isolated provider fixtures."""
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import explore as worker
from sniper.models import SafetyEvidence
from sniper.parallel_entry import ParallelEntryEngine, PROFILE
from test_engine import quote
from test_entry_strategies import NOW, WALLET, OTHER, TOKEN, POOL, POOL2, TX, TX2, BLOCK, make_proof, registry


class ParallelWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'exploration'; self.folder.mkdir()
        self.policy, rules, _ = worker.profile_settings('two-wallet-30k-reentry')
        ledger, store, _, before = worker.open_trial(self.folder, self.policy, start=True, now=NOW - 60, rules=rules)
        ledger.close(); store.close()
        worker.activate_parallel(self.root, whale_eligibility='reviewed_tracked',
                                 whale_reentry='fresh_post_close', now=NOW - 1)
        self.trial = json.loads((self.folder / 'trial.json').read_text())
        self.assertEqual(self.trial['account_id'], before['account_id'])
        self.assertEqual(self.trial['ends_at'], before['ends_at'])
        self.tick, self.price = [NOW], D(1)
        self.pending, self.routes, self.finished, self.rpc_checks = [], [], [], []
        self.registry = registry()
        self.safety_failure, self.route_available, self.block_hash = None, True, BLOCK
        self.entry_quote_advance_once = 0

    def tearDown(self):
        self.temp.cleanup()

    def add(self, evidence, event=None):
        self.pending.append({'id': event or evidence['tx_hash'], 'proof': deepcopy(evidence)})

    @contextmanager
    def offline(self):
        owner = self
        class RPC:
            def __init__(self, *args, **kwargs): pass
            @contextmanager
            def budget(self, *args): yield
            def call(self, method, params):
                if method != 'eth_getBlockByNumber':
                    raise AssertionError('Unexpected fixture RPC method: ' + method)
                owner.rpc_checks.append((method, params))
                return {'number': params[0], 'hash': owner.block_hash}
        class Source:
            def __init__(self, rpc, store, market): self.store = store
            def collect(self, **kwargs):
                # Real canonical journal commit precedes ConfirmationBook.record.
                events = [{**s['proof'], 'kind': 'sampled_swap', 'address': s['proof']['pool']} for s in owner.pending]
                if events:
                    tip = {'number': 100, 'hash': BLOCK}
                    self.store.commit_chunk(4663, events, [tip], tip, expected_cursor=self.store.cursor(4663))
                return {'sampled': len(events), 'fixture': True}
            def pending(self): return list(owner.pending)
            def prove(self, swap, **kwargs): return deepcopy(swap['proof'])
            def finish(self, swap, result): owner.finished.append((swap['id'], deepcopy(result)))
        class Market:
            def __init__(self, **kwargs): self.transport = None
        class Route:
            def __init__(self, *args): self.last_evidence = {}
            def quote(self, token, pool, **kwargs):
                if not kwargs.get('exit_only') and owner.entry_quote_advance_once:
                    owner.tick[0] += owner.entry_quote_advance_once
                    owner.entry_quote_advance_once = 0
                owner.routes.append({'token': token, 'pool': pool, **kwargs})
                amount = D(kwargs['max_notional'])
                qty = kwargs['sell_quantity'] if kwargs.get('exit_only') else amount / owner.price
                self.last_evidence = {'scope': 'isolated provider fixture', 'buy_input_usd': str(amount)}
                return quote(asset='eip155:4663/erc20:' + token, now=owner.tick[0], price=owner.price,
                    pool_id=pool, executable=owner.route_available, route_max_notional_usd=qty * owner.price,
                    route_max_sell_quantity=qty, route_impact_bps=D(1),
                    route_side='sell' if kwargs.get('exit_only') else 'both')
        def safety(client, token):
            if owner.safety_failure == 'missing':
                raise worker.SourceError('Required provider response unavailable in fixture')
            checks = {k: True for k in owner.policy.required_safety_checks}
            if owner.safety_failure:
                checks[owner.safety_failure] = False
            return SafetyEvidence('eip155:4663/erc20:' + token, owner.tick[0], checks,
                'isolated-provider-fixture', {'rug_screen': {'status': 'PASS'}})
        def engine(*args, **kwargs):
            return ParallelEntryEngine(*args, **kwargs, registry_loader=lambda *_: deepcopy(owner.registry),
                                       clock=lambda: owner.tick[0])
        with ExitStack() as stack:
            for obj, name, value in [
                (worker.time, 'time', lambda: owner.tick[0]), (worker, 'RpcClient', RPC),
                (worker, 'ExploratorySource', Source), (worker, 'DexScreenerClient', Market),
                (worker, 'V3RouteClient', Route), (worker, 'ParallelEntryEngine', engine),
                (worker, 'load_wallet_registry', lambda *_: deepcopy(owner.registry)),
                (worker, 'signal_id', lambda swap: swap['id']), (worker.GoPlusClient, 'screen', safety),
                (worker, 'pool_adjusted_screen', lambda s, *a: s),
                (worker, 'observed_pool_screen', lambda s, *a: s),
                (worker, 'observe_creator_balance', lambda s, *a: s),
                (worker, 'supplement_from_runtime', lambda s, *a: s),
                (worker, 'enhance_rug_screen', lambda s: s),
                (worker, 'notify_fills', lambda *a, **k: {'sent': 0, 'fixture': True}),
                (worker, 'notify_market', lambda *a, **k: {'sent': 0, 'fixture': True})]:
                stack.enter_context(patch.object(obj, name, value))
            network = stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('External network forbidden')))
            yield
            network.assert_not_called()

    def cycle(self):
        with self.offline():
            return worker.run_cycle(self.folder, self.root, profile=PROFILE)

    def signals(self):
        with closing(sqlite3.connect((self.folder / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)) as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM events WHERE kind='entry_strategy_signal' ORDER BY rowid")]

    def latest_signals(self):
        return {s['strategy_id']: s for s in self.signals()}

    def assert_clean(self, result):
        self.assertEqual(result['errors'], [], result['errors'])
        self.assertEqual(result['trial']['account_id'], self.trial['account_id'])
        self.assertEqual(result['trial']['ends_at'], self.trial['ends_at'])
        self.assertFalse(result['real_money_enabled'])

    def test_one_reviewed_direct_purchase_opens_a_without_b(self):
        self.add(make_proof()); result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['fills'][0]['strategy'], 'whale_follow')
        signals = self.latest_signals()
        self.assertTrue(signals['whale_follow']['qualified']); self.assertFalse(signals['two_wallet_v3']['qualified'])
        self.assertGreater(len(self.rpc_checks), 0)

    def test_two_same_pool_buyers_open_b_when_a_is_ineligible(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); self.add(make_proof(wallet=OTHER, tx=TX2))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['fills'][0]['strategy'], 'two_wallet_v3')
        self.assertFalse(self.latest_signals()['whale_follow']['qualified'])

    def test_optional_b_proof_expiring_during_quote_does_not_block_fresh_a(self):
        self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2, at=NOW - 895))
        self.add(make_proof(at=NOW - 10))
        self.entry_quote_advance_once = 10
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(self.tick[0], NOW + 10)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['fills'][0]['strategy'], 'whale_follow')
        history = self.signals()
        self.assertTrue(any(s['strategy_id'] == 'two_wallet_v3' and s['qualified'] for s in history))
        final = self.latest_signals()
        self.assertTrue(final['whale_follow']['qualified'])
        self.assertFalse(final['two_wallet_v3']['qualified'])
        self.assertEqual(final['two_wallet_v3']['observed_wallets'], 1)
        self.assertTrue(all(p['tx_hash'] != TX2 for s in final.values() for p in s['source_proofs']))

    def test_two_cross_pool_buyers_open_b_using_only_selected_pool_route(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['snapshot']['positions'][0]['pool_id'], POOL2)
        self.assertEqual(set(self.latest_signals()['two_wallet_v3']['source_pools']), {POOL, POOL2})
        self.assertTrue(all(r['pool'] == POOL2 for r in self.routes))

    def test_same_buyer_in_two_pools_does_not_form_b(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); self.add(make_proof(tx=TX2, pool=POOL2))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(result['fills'], [])
        self.assertFalse(self.latest_signals()['two_wallet_v3']['qualified'])
        self.assertEqual(self.routes, [])

    def test_two_tokens_never_combine_into_one_confirmation(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2, token='0x' + 'bb' * 20))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(result['fills'], [])
        self.assertFalse(any(s['qualified'] for s in self.signals()))

    def test_b_qualifies_after_a_holds_without_duplicate_fill_or_new_route(self):
        self.add(make_proof()); first = self.cycle(); self.assert_clean(first)
        self.tick[0] += 120; self.pending = []
        self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2, at=self.tick[0] - 1))
        self.routes.clear(); result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 1)
        signal = self.latest_signals()['two_wallet_v3']
        self.assertTrue(signal['qualified']); self.assertEqual(signal['execution_reason'], 'already_open')
        self.assertTrue(all(r.get('exit_only') is True for r in self.routes))

    def test_both_paths_qualify_together_and_one_shared_position_has_a_initiator(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); first = self.cycle(); self.assert_clean(first)
        self.assertEqual(first['fills'], [])
        self.tick[0] += 120; self.pending = []; self.registry = registry(WALLET, OTHER)
        self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2, at=self.tick[0] - 1))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['fills'][0]['strategy'], 'whale_follow')
        signals = self.latest_signals()
        self.assertTrue(all(s['qualified'] for s in signals.values()))
        self.assertTrue(all(s['initiating_strategy'] == 'whale_follow' for s in signals.values()))

    def test_changed_canonical_block_prevents_entry(self):
        self.block_hash = '0x' + '88' * 32
        self.add(make_proof()); result = self.cycle()
        self.assertEqual(result['fills'], []); self.assertEqual(self.routes, [])
        self.assertTrue(any('ReorgError' in error for error in result['errors']))

    def test_shared_missing_provider_response_blocks_single_whale(self):
        self.safety_failure = 'missing'; self.add(make_proof()); result = self.cycle()
        self.assertEqual(result['fills'], []); self.assertEqual(self.routes, [])
        self.assertTrue(any('Required provider response unavailable' in error for error in result['errors']))

    def test_shared_safety_failure_blocks_signal_without_buy_quote(self):
        self.safety_failure = 'sellability'; self.add(make_proof()); result = self.cycle()
        self.assertEqual(result['fills'], []); self.assertEqual(self.routes, [])
        self.assertTrue(self.latest_signals()['whale_follow']['qualified'])

    def test_unavailable_sized_route_cannot_admit_a(self):
        self.route_available = False; self.add(make_proof()); result = self.cycle()
        self.assertEqual(result['fills'], [])
        self.assertTrue(self.latest_signals()['whale_follow']['qualified'])

    def test_original_stop_exit_survives_registry_loss(self):
        self.add(make_proof()); first = self.cycle(); self.assert_clean(first)
        self.pending = []; self.tick[0] += 120; self.price = D('.5')
        self.registry = {'status': 'UNAVAILABLE', 'wallets': {}, 'checked_at': self.tick[0]}
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 2)
        self.assertEqual(result['fills'][-1]['reason'], 'stop_loss')
        self.assertEqual(result['fills'][-1]['strategy'], 'whale_follow')
        self.assertEqual(result['snapshot']['open_positions'], 0)

    def test_a_fresh_post_close_reentry_does_not_borrow_bs_two_buyer_requirement(self):
        self.add(make_proof()); self.assert_clean(self.cycle())
        self.pending = []; self.tick[0] += 120; self.price = D('.5')
        stopped = self.cycle(); self.assert_clean(stopped)
        self.assertEqual(stopped['snapshot']['open_positions'], 0)
        self.tick[0] += 120; self.price = D(1)
        self.add(make_proof(tx=TX2, at=self.tick[0] - 1))
        result = self.cycle(); self.assert_clean(result)
        self.assertEqual(len(result['fills']), 3)
        self.assertEqual(result['fills'][-1]['action'], 'OPEN')
        self.assertEqual(result['fills'][-1]['strategy'], 'whale_follow')
        self.assertTrue(self.latest_signals()['whale_follow']['qualified'])
        self.assertFalse(self.latest_signals()['two_wallet_v3']['qualified'])

    def test_b_reentry_counts_only_two_distinct_post_close_buys(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}, 'checked_at': NOW}
        self.add(make_proof()); self.add(make_proof(wallet=OTHER, tx=TX2, pool=POOL2))
        self.assert_clean(self.cycle())
        self.pending = []; self.tick[0] += 120; self.price = D('.5')
        closed = self.cycle(); self.assert_clean(closed)
        self.assertEqual(closed['snapshot']['open_positions'], 0)
        closed_at = self.tick[0]
        self.tick[0] += 120; self.price = D(1)
        self.add(make_proof(tx='0x' + '89' * 32, at=self.tick[0] - 1))
        one = self.cycle(); self.assert_clean(one)
        self.assertEqual(len(one['fills']), 2)
        self.assertFalse(self.latest_signals()['two_wallet_v3']['qualified'])
        self.pending = []; self.tick[0] += 120
        self.add(make_proof(wallet=OTHER, tx='0x' + '90' * 32, pool=POOL2, at=self.tick[0] - 1))
        two = self.cycle(); self.assert_clean(two)
        self.assertEqual(len(two['fills']), 3)
        self.assertEqual(two['fills'][-1]['strategy'], 'two_wallet_v3')
        signal = self.latest_signals()['two_wallet_v3']
        self.assertTrue(signal['qualified'])
        self.assertTrue(all(p['observed_at'] > closed_at for p in signal['source_proofs']))


if __name__ == '__main__':
    unittest.main()
