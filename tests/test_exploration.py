from dataclasses import replace
from decimal import Decimal as D
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.engine import PaperEngine
from sniper.exploration import ExploratoryEngine, exploratory_policy, pool_adjusted_screen
from sniper.exploratory_source import ExploratorySource, signal_id, verify_recipient_acquisition
from sniper.exploratory_alerts import notify_fills
from sniper.adapters.evm import RpcClient, decode_swap, USDG
from sniper.adapters.evm_store import EvmStore
from sniper.ledger import Ledger
from sniper.models import SafetyEvidence, WalletConfirmation
from sniper.policy import RiskPolicy
from test_engine import quote, candidate, NOW
from test_evm import FakeChain, TOKEN, POOL, WALLET, OTHER, TX_SWAP, word, swap_log, receipt, transfer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('explore_worker', ROOT / 'explore.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def exploratory_candidate(**kwargs):
    c = candidate(strategy=ExploratoryEngine.strategy_name, **kwargs)
    return replace(c, confirmations=[replace(c.confirmations[0], qualified=False)],
                   safety=replace(c.safety, checks={k: None for k in c.safety.checks}))


class ExplorationEngineTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(':memory:', D(1000))
        self.policy = exploratory_policy(RiskPolicy())
        self.engine = ExploratoryEngine(self.ledger, self.policy)

    def tearDown(self):
        self.ledger.close()

    def test_unknowns_are_labeled_and_position_risk_retained(self):
        result = self.engine.consider(exploratory_candidate(), quote(), NOW)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['observed_wallets'], 1)
        self.assertNotIn('independent_wallets', result)
        self.assertFalse(result['strict_strategy_result'])
        self.assertEqual(result['wallet_qualification'], 'unestablished')
        self.assertEqual(len(result['unknown_safety_checks']), 5)
        self.assertLessEqual(result['full_loss_risk_usd'], D(5))
        self.assertEqual(self.engine.consider(exploratory_candidate(), quote(), NOW)['reason'], 'duplicate_event')

    def test_known_failure_still_blocks(self):
        c = exploratory_candidate()
        c = replace(c, safety=replace(c.safety, checks={'sellability': False}))
        self.assertEqual(self.engine.consider(c, quote(), NOW)['reason'], 'safety_failed:sellability')

    def test_unverified_payment_and_missing_route_still_block(self):
        c = exploratory_candidate()
        c = replace(c, confirmations=[replace(c.confirmations[0], verified_swap=False)])
        self.assertEqual(self.engine.consider(c, quote(), NOW)['verdict'], 'SKIP')
        self.assertEqual(self.engine.consider(exploratory_candidate(event='two'), None, NOW)['verdict'], 'SKIP')

    def test_modes_cannot_open_each_others_accounts(self):
        with self.assertRaisesRegex(ValueError, 'evaluation mode'):
            PaperEngine(self.ledger, self.policy)
        strict = Ledger(':memory:', D(1000))
        try:
            PaperEngine(strict, self.policy)
            with self.assertRaisesRegex(ValueError, 'evaluation mode'):
                ExploratoryEngine(strict, self.policy)
        finally:
            strict.close()

    def test_policy_changes_only_confirmation_count(self):
        a,b = RiskPolicy().as_dict(), self.policy.as_dict()
        self.assertEqual([k for k in a if a[k] != b[k]], ['min_independent_wallets'])

    def test_only_factory_verified_pool_balance_is_excluded(self):
        raw = {'holders': [{'address': POOL, 'percent': '.85'}, {'address': WALLET, 'percent': '.02'}],
               'holder_count': '3', 'creator_percent': '.01'}
        s = SafetyEvidence('token', NOW, {'holder_distribution': False, 'sellability': False}, 'test', {'raw_report': raw})
        adjusted = pool_adjusted_screen(s, POOL)
        self.assertTrue(adjusted.checks['holder_distribution'])
        self.assertFalse(adjusted.checks['sellability'])
        self.assertIs(pool_adjusted_screen(s, TOKEN), s)
        raw['creator_percent'] = '.1'
        self.assertFalse(pool_adjusted_screen(s, POOL).checks['holder_distribution'])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        def transport(payload):
            if isinstance(payload, list):
                return [transport(p) for p in payload]
            if payload['method'] == 'eth_call':
                data = payload['params'][0]['data']
                values = {'0x0dfe1681': TOKEN, '0xd21220a7': USDG, '0xddca3f43': 3000, '0x1698ee82': POOL}
                if data[:10] in values:
                    return {'jsonrpc': '2.0', 'id': payload['id'], 'result': '0x' + word(values[data[:10]])}
            return self.chain(payload)
        self.store = EvmStore()
        market = type('Market', (), {'quote': lambda _, *a, **k: quote(now=k['now'])})()
        self.source = ExploratorySource(RpcClient(transport=transport), self.store, market, clock=lambda: 1800000000)

    def tearDown(self):
        self.store.close()

    def test_sampled_canonical_direct_buy_survives_restart_and_dedupes(self):
        result = self.source.collect()
        self.assertEqual(result['sampled'], 1)
        s = self.source.pending()[0]
        proof = self.source.prove(s)
        self.assertEqual(proof['wallet'], WALLET)
        self.assertEqual(proof['spent_usd'], '10.000000')
        self.assertEqual(proof['qualification'], 'unestablished')
        self.source.finish(s, {'verdict': 'PAPER_BUY'})
        self.assertFalse(self.source.pending())
        self.assertEqual(self.source.collect()['sampled'], 0)

    def test_changed_chain_invalidates_signal_proof(self):
        self.source.collect()
        s = self.source.pending()[0]
        self.chain.fork_from = 102
        with self.assertRaisesRegex(RuntimeError, 'canonical'):
            self.source.prove(s)

    def test_routed_buy_keeps_recipient_payer_and_relayer_separate(self):
        swap = decode_swap(swap_log())
        rc = receipt()
        rc['logs'][0] = transfer(USDG, OTHER, POOL, 10000000, 0)
        rc['logs'].append({'topics': []})
        proof = verify_recipient_acquisition(swap, {'pool': POOL, 'token0': TOKEN, 'token1': USDG}, rc, {'hash': TX_SWAP, 'from': OTHER})
        self.assertEqual(proof['wallet'], WALLET)
        self.assertEqual(proof['transaction_sender'], OTHER)
        self.assertEqual(proof['base_payers'], [OTHER])
        self.assertIn('not proven personal', proof['spend_basis'])
        for field, value in [('status', '0x0'), ('blockHash', '0x' + 'ff'*32)]:
            changed = {**rc, field: value}
            self.assertIsNone(verify_recipient_acquisition(swap, {'pool': POOL, 'token0': TOKEN, 'token1': USDG}, changed, {'hash': TX_SWAP, 'from': OTHER}))

    def test_airdrop_unpaid_or_roundtrip_is_not_a_funded_acquisition(self):
        swap = decode_swap(swap_log())
        pool = {'pool': POOL, 'token0': TOKEN, 'token1': USDG}
        tx = {'hash': TX_SWAP, 'from': OTHER}
        rc = receipt()
        rc['logs'] = rc['logs'][1:]
        self.assertIsNone(verify_recipient_acquisition(swap, pool, rc, tx))
        rc = receipt()
        rc['logs'].append(transfer(TOKEN, WALLET, OTHER, 2000000000, 3))
        self.assertIsNone(verify_recipient_acquisition(swap, pool, rc, tx))
        rc = receipt()
        rc['logs'][0] = transfer(USDG, OTHER, POOL, 9999999, 0)
        self.assertIsNone(verify_recipient_acquisition(swap, pool, rc, tx))


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / 'exploration'
        self.folder.mkdir()
        self.policy = exploratory_policy(RiskPolicy())

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_preserves_trial_deadline_and_lost_state_does_not_reset(self):
        l,s,e,m = worker.open_trial(self.folder, self.policy, start=True, now=NOW)
        l.close(); s.close()
        l,s,e,m2 = worker.open_trial(self.folder, self.policy, start=True, now=NOW+100)
        l.close(); s.close()
        self.assertEqual(m, m2)
        (self.folder / 'events.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            worker.open_trial(self.folder, self.policy, start=True, now=NOW+200)

    def test_duration_tampering_rejected(self):
        l,s,e,m = worker.open_trial(self.folder, self.policy, start=True, now=NOW)
        l.close(); s.close()
        m['ends_at'] += 86400
        (self.folder/'trial.json').write_text(json.dumps(m), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duration'):
            worker.open_trial(self.folder, self.policy)

    def test_fill_notifications_are_labeled_deduplicated_and_journal_bound(self):
        l,s,e,m = worker.open_trial(self.folder, self.policy, start=True, now=NOW)
        try:
            e.consider(exploratory_candidate(), quote(), NOW)
            (self.folder/'telegram-bot-token.txt').write_text('test-fixture')
            (self.folder/'telegram-recipient.json').write_text(json.dumps({'bot_id': 1, 'chat_id': 2}))
            messages = []
            class Client:
                def __init__(self, key): self.key=key
                def call(self, method): return {'id': 1}
                def send(self, destination, message):
                    self.assert_destination = destination
                    messages.append(message)
                    return 42
            self.assertEqual(notify_fills(l, self.folder, self.folder, client_factory=Client)['sent'], 1)
            self.assertEqual(notify_fills(l, self.folder, self.folder, client_factory=Client)['sent'], 0)
            self.assertEqual(len(messages), 1)
            self.assertIn('EXPLORATORY PAPER OPEN', messages[0])
            self.assertIn('No real-money order', messages[0])
            (self.folder/'notifications.sqlite3').unlink()
            with self.assertRaisesRegex(ValueError, 'journal missing'):
                notify_fills(l, self.folder, self.folder, client_factory=Client)
        finally:
            l.close(); s.close()

    def test_complete_live_data_path_fill_restart_exit_and_week_cutoff(self):
        tick = [NOW]
        asset = 'eip155:4663/erc20:' + TOKEN
        swap = {**decode_swap(swap_log()), 'observed_at': NOW}
        class Source:
            def __init__(self, *args): pass
            def collect(self): return {'sampled': 1}
            def pending(self): return [swap]
            def finish(self, *args): pass
            def prove(self, s):
                return {'token_address': TOKEN, 'pool': POOL, 'spent_usd': '100', 'wallet': WALLET,
                        'tx_hash': TX_SWAP, 'observed_at': NOW}
        class Market:
            def __init__(self, **kwargs): pass
            def quote(self, *args, **kwargs): return quote(asset=asset, now=tick[0], pool_id=POOL)
        class Route:
            last_evidence = {'scope': 'synthetic offline test only'}
            def __init__(self, *args): pass
            def quote(self, *args, **kwargs): return quote(asset=asset, now=tick[0], pool_id=POOL)
        with patch.object(worker.time, 'time', side_effect=lambda: tick[0]), patch.object(worker, 'ExploratorySource', Source), \
             patch.object(worker, 'DexScreenerClient', Market), patch.object(worker, 'V3RouteClient', Route), \
             patch.object(worker.GoPlusClient, 'screen', side_effect=lambda _: SafetyEvidence(asset, tick[0], {}, 'test')):
            first = worker.run_cycle(self.folder, self.folder.parent, start=True)
            self.assertEqual(len(first['fills']), 1)
            self.assertEqual(first['snapshot']['open_positions'], 1)
            tick[0] += 60
            second = worker.run_cycle(self.folder, self.folder.parent)
            self.assertEqual(len(second['fills']), 1)
            tick[0] = NOW + 7*86400
            last = worker.run_cycle(self.folder, self.folder.parent)
            self.assertEqual(last['trial_state'], 'COMPLETE')
            self.assertEqual(len(last['fills']), 2)
            self.assertEqual(last['snapshot']['closed_positions'], 1)
            self.assertEqual(last['total_monitoring_gaps'], 1)
