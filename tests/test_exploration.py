from dataclasses import replace
from decimal import Decimal as D
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.engine import PaperEngine
from sniper.exploration import ExploratoryEngine, exploratory_policy, pool_adjusted_screen, observed_pool_screen, observe_creator_balance
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


class CollectorCadenceTests(unittest.TestCase):
    def collect_interval(self, tip):
        from unittest.mock import Mock
        from sniper.adapters.evm import CHAIN_ID
        from test_evm import bh
        store = EvmStore(':memory:')
        try:
            store.commit_chunk(CHAIN_ID, [], [], {'number': 100, 'hash': bh(100)}, expected_cursor=None)
            rpc = Mock(batch_enabled=False)
            requests = []
            def call(method, params=None):
                if method == 'eth_blockNumber':
                    return hex(tip+5)
                if method == 'eth_getLogs':
                    requests.append(params[0])
                    return []
                raise AssertionError(method)
            rpc.call.side_effect = call
            source = ExploratorySource(rpc, store, Mock())
            source.collector._recover_reorg = lambda: None
            source.collector.block = lambda n: {'number': n, 'hash': bh(n), 'parent_hash': bh(n-1), 'timestamp': NOW}
            return source.collect(), requests
        finally:
            store.close()

    def test_normal_two_minute_interval_has_no_artificial_gap(self):
        result, requests = self.collect_interval(1300)
        self.assertEqual(result['from_block'], 101)
        self.assertEqual(result['blocks_scanned'], 1200)
        self.assertEqual(result['skipped_blocks'], 0)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(int(q['toBlock'],16)-int(q['fromBlock'],16)<500 for q in requests))

    def test_extended_downtime_remains_bounded_and_disclosed(self):
        result, requests = self.collect_interval(4100)
        self.assertEqual(result['blocks_scanned'], 3000)
        self.assertEqual(result['skipped_blocks'], 1000)
        self.assertEqual(len(requests), 6)


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

    def test_canonical_buy_resolves_listing_conflict_without_inventing_tax_or_sell_pass(self):
        raw = {'is_in_dex': '0', 'is_honeypot': '0', 'cannot_buy': '0', 'trading_cooldown': '0', 'buy_tax': ''}
        screen = SafetyEvidence('eip155:4663/erc20:'+TOKEN, NOW, {'sellability':False, 'contract_controls':False}, 'test', {'raw_report': raw})
        proof = {'factory_verified':True, 'verified_swap':True, 'token_address':TOKEN, 'pool':POOL, 'tx_hash':TX_SWAP}
        fixed = observed_pool_screen(screen, proof)
        self.assertIsNone(fixed.checks['sellability'])
        self.assertFalse(fixed.checks['contract_controls'])
        self.assertIs(observed_pool_screen(screen, {**proof, 'factory_verified':False}), screen)
        raw['is_honeypot'] = '1'
        self.assertFalse(observed_pool_screen(screen, proof).checks['sellability'])
        raw['is_honeypot']='0'; raw['sell_tax']='.1'
        self.assertFalse(observed_pool_screen(screen, proof).checks['sellability'])

    def test_creator_concentration_is_checked_even_without_holder_list(self):
        screen = SafetyEvidence('eip155:4663/erc20:'+TOKEN, NOW, {'holder_distribution':None}, 'test', {'raw_report':{'creator_address':WALLET}})
        def transport(p):
            method = p['method']
            if method=='eth_blockNumber': value=hex(105)
            elif method=='eth_getBlockByNumber': value={'number':hex(100),'timestamp':hex(NOW),'hash':'0x'+'aa'*32}
            else:
                self.assertEqual(p['params'][1], hex(100))
                value='0x'+word(1000 if p['params'][0]['data']=='0x18160ddd' else 100)
            return {'jsonrpc':'2.0','id':p['id'],'result':value}
        rpc = RpcClient(transport=lambda p: [transport(x) for x in p] if isinstance(p,list) else transport(p))
        with patch('sniper.exploration.time.time', return_value=NOW):
            observed=observe_creator_balance(screen,rpc)
        self.assertFalse(observed.checks['holder_distribution'])
        self.assertEqual(observed.details['creator_balance_observation']['share'], '0.1')


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

    def test_sampling_keeps_all_swap_legs_of_each_selected_transaction(self):
        original = self.source.rpc.transport
        def transport(payload):
            result = original(payload)
            if payload['method'] == 'eth_getLogs':
                second = {**result['result'][0], 'logIndex': '0x3', 'address': OTHER}
                result['result'].append(second)
            return result
        self.source.rpc.transport = transport
        result = self.source.collect(sample_size=1)
        self.assertEqual(result['sampled_transactions'], 1)
        self.assertEqual(result['sampled'], 2)
        self.assertEqual(len(self.source.pending()), 2)

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

    def test_candidate_pool_followup_is_bounded_and_validates_batched_headers(self):
        logs=[{**swap_log(),'transactionHash':'0x'+f'{i+1:064x}','logIndex':hex(i),'address':POOL if i<20 else OTHER} for i in range(40)]
        decoded=[decode_swap(x) for x in logs]
        global_pool=min(decoded,key=signal_id)['address']
        watched=OTHER if global_pool==POOL else POOL
        original=self.source.rpc.transport
        def transport(payload):
            if isinstance(payload,list):return [transport(p) for p in payload]
            if payload['method']=='eth_getLogs':return {'jsonrpc':'2.0','id':payload['id'],'result':logs}
            return original(payload)
        self.source.rpc.transport=transport
        self.source.rpc.batch_enabled=True
        result=self.source.collect(sample_size=1,watch_pools=[watched])
        self.assertEqual(result['global_sampled_transactions'],1)
        self.assertEqual(result['focused_pool_signals'],16)
        self.assertEqual(result['sampled_transactions'],17)
        self.assertEqual(sum(s['address']==watched for s in self.source.pending()),16)

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
        collectors, routes = [], []
        (self.folder.parent/'observation-rpc-url.txt').write_text('https://collector.invalid')
        (self.folder.parent/'quote-rpc-url.txt').write_text('https://quotes.invalid/v2/private-fixture')
        asset = 'eip155:4663/erc20:' + TOKEN
        swap = {**decode_swap(swap_log()), 'observed_at': NOW}
        class Source:
            def __init__(self, rpc, *args): collectors.append(rpc.url)
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
            def __init__(self, rpc, *args): routes.append(rpc.url)
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
            self.assertEqual(set(collectors), {'https://collector.invalid'})
            self.assertEqual(set(routes), {'https://quotes.invalid/v2/private-fixture'})
