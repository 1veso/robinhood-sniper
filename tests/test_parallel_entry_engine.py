"""Isolated admission/accounting tests; no external providers or strategy data."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import unittest

from sniper.adapters.evm import CHAIN_ID, ReorgError
from sniper.adapters.evm_store import EvmStore
from sniper.confirmation_book import ConfirmationBook
from sniper.entry_strategies import entry_rules, evaluate_signals, rules_hash
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger, json_text
from sniper.models import Candidate, SafetyEvidence, WalletConfirmation
from sniper.parallel_entry import ParallelEntryEngine, signal_proofs
from sniper.policy import RiskPolicy
from test_engine import quote
from test_entry_strategies import make_proof, registry, NOW, WALLET, OTHER, TOKEN, POOL, POOL2, TX, TX2, BLOCK


ASSET = 'eip155:4663/erc20:' + TOKEN


class CanonicalRpc:
    def __init__(self):
        self.calls = []
        self.hash = BLOCK

    def call(self, method, params):
        if method != 'eth_getBlockByNumber':
            raise AssertionError('No network/provider method is permitted in this fixture')
        self.calls.append((method, params))
        return {'number': params[0], 'hash': self.hash}


class ParallelEntryEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.rules = entry_rules()
        self.registry = registry()
        self.store = EvmStore(':memory:')
        self.book = ConfirmationBook(self.store)
        self.ledger = Ledger(':memory:', D(1000))
        self.policy = replace(RiskPolicy(), min_independent_wallets=2,
            min_liquidity_usd=D(30000), max_open_positions=0,
            max_entries_per_day=0, cooldown_seconds=0,
            required_safety_checks=RiskPolicy().required_safety_checks + ('rug_screen_available',))
        self.rpc = CanonicalRpc()
        self.pin()
        self.engine = self.new_engine()

    def tearDown(self):
        self.ledger.close()
        self.store.close()

    def pin(self):
        with self.ledger.transaction():
            self.ledger._set('entry_rules', json_text(self.rules))
            self.ledger._set('entry_revision', rules_hash(self.rules))
            self.ledger._set('trial_manifest', json_text({
                'source_id': self.store.identity, 'ends_at': NOW + 604800}))

    def new_engine(self):
        return ParallelEntryEngine(self.ledger, self.policy, entry_rules=self.rules,
            runtime=Path('isolated-fixture'), store=self.store,
            registry_loader=lambda runtime, now: deepcopy(self.registry), clock=lambda: self.now)

    def persist(self, proofs):
        for proof in proofs:
            event = {k: proof[k] for k in ('tx_hash', 'log_index', 'block_number', 'block_hash', 'observed_at')}
            event.update(kind='sampled_swap', address=proof['pool'])
            self.store.commit_chunk(CHAIN_ID, [event], [{'number': 100, 'hash': BLOCK}],
                {'number': 100, 'hash': BLOCK}, expected_cursor=self.store.cursor(CHAIN_ID))
            self.book.record(proof['tx_hash'] + ':' + str(proof['log_index']), proof)

    def candidate(self, proofs=None, *, initiator='whale_follow', event='first', confirm=True):
        proofs = proofs if proofs is not None else [make_proof()]
        self.persist(proofs)
        closed = self.ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?',
            (ASSET,)).fetchone()[0]
        signals = evaluate_signals(proofs, token=TOKEN, now=self.now, last_closed_at=closed,
            rules=self.rules, registry=self.registry)
        selected = next(s for s in signals if s['strategy_id'] == initiator)
        checked = self.engine.confirm_proofs(signal_proofs(signals), self.rpc) if confirm else self.now
        safety = SafetyEvidence(ASSET, self.now,
            {**{k: True for k in self.policy.required_safety_checks}, 'rug_screen_available': True},
            'synthetic-test', {'report_pool_id': POOL})
        return Candidate(event_id=event, asset_id=ASSET, symbol='TEST', observed_at=self.now,
            strategy=ExploratoryEngine.strategy_name,
            confirmations=[WalletConfirmation.from_dict(c) for c in selected['confirmations']],
            safety=safety, metadata={'entry_revision': rules_hash(self.rules),
                'source_id': self.store.identity, 'canonical_checked_at': checked,
                'strategy_signals': signals, 'initiating_strategy': initiator,
                'qualified_strategies': [s['strategy_id'] for s in signals if s['qualified']]})

    def quote(self, **changes):
        return quote(ASSET, self.now, pool_id=POOL, route_max_notional_usd=D(10),
            route_max_sell_quantity=D(10), route_impact_bps=D(5), **changes)

    def consider(self, candidate, **quote_changes):
        return self.engine.consider(candidate, self.quote(**quote_changes), self.now)

    def two_proofs(self, **kwargs):
        return [make_proof(**kwargs), make_proof(wallet=OTHER, tx=TX2, pool=POOL2, **kwargs)]

    def signals(self, event):
        rows = self.ledger.conn.execute("SELECT payload FROM events WHERE kind='entry_strategy_signal'").fetchall()
        return {p['strategy_id']: p for p in (json.loads(r['payload']) for r in rows) if p['event_id'] == event}

    def test_a_single_reviewed_buyer_passes_shared_policy_minimum_two(self):
        result = self.consider(self.candidate())
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['observed_wallets'], 1)
        self.assertEqual(result['qualified_strategies'], ['whale_follow'])
        self.assertEqual(self.ledger.positions()[0]['strategy'], 'whale_follow')
        self.assertEqual(self.policy.min_independent_wallets, 2)

    def test_b_cross_pool_observed_buyers_do_not_require_a_review_registry(self):
        self.registry = {'status': 'EMPTY', 'wallets': {}}
        result = self.consider(self.candidate(self.two_proofs(), initiator='two_wallet_v3'))
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['qualified_strategies'], ['two_wallet_v3'])
        self.assertEqual(self.ledger.positions()[0]['strategy'], 'two_wallet_v3')
        self.assertEqual(self.signals('first')['two_wallet_v3']['source_pools'], [POOL, POOL2])

    def test_b_confirmation_remains_visible_when_a_already_holds_token(self):
        self.assertEqual(self.consider(self.candidate())['verdict'], 'PAPER_BUY')
        result = self.consider(self.candidate(self.two_proofs(), initiator='two_wallet_v3', event='later'))
        self.assertEqual(result['reason'], 'already_open')
        signals = self.signals('later')
        self.assertTrue(signals['two_wallet_v3']['qualified'])
        self.assertEqual(signals['two_wallet_v3']['execution_reason'], 'already_open')
        self.assertEqual(set(result['qualified_strategies']), {'whale_follow', 'two_wallet_v3'})
        self.assertEqual(self.ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)

    def test_candidate_booleans_cannot_forge_second_strategy_qualification(self):
        c = self.candidate()
        metadata = deepcopy(c.metadata)
        metadata['qualified_strategies'] = ['whale_follow', 'two_wallet_v3']
        metadata['strategy_signals'][1]['qualified'] = True
        result = self.consider(replace(c, metadata=metadata))
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['qualified_strategies'], ['whale_follow'])
        self.assertFalse(self.signals('first')['two_wallet_v3']['qualified'])

    def test_fill_attribution_keeps_the_final_admission_snapshot(self):
        c = self.candidate()
        original_size = self.engine._size
        def size_after_registry_publication(q, snapshot):
            # A new research view after the final admission check cannot
            # retrospectively relabel the already admitted transaction.
            self.registry = {'status': 'EMPTY', 'wallets': {}}
            return original_size(q, snapshot)
        self.engine._size = size_after_registry_publication
        result = self.consider(c)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['qualified_strategies'], ['whale_follow'])
        self.assertTrue(self.signals('first')['whale_follow']['qualified'])
        self.assertEqual(self.ledger.positions()[0]['strategy'], 'whale_follow')

    def test_selected_path_cannot_borrow_other_path_confirmations(self):
        c = self.candidate(self.two_proofs())
        other = c.metadata['strategy_signals'][1]['confirmations']
        result = self.consider(replace(c, confirmations=[WalletConfirmation.from_dict(x) for x in other]))
        self.assertEqual(result['reason'], 'entry_confirmation_evidence_mismatch')
        self.assertFalse(self.ledger.positions())

    def test_metadata_canonical_timestamp_without_actual_check_cannot_enter(self):
        result = self.consider(self.candidate(confirm=False))
        self.assertEqual(result['reason'], 'canonical_signal_check_missing')
        self.assertEqual(self.ledger.snapshot()['cash'], D(1000))

    def test_restart_requires_new_rpc_check_even_with_persisted_proof(self):
        c = self.candidate()
        self.engine = self.new_engine()
        self.assertEqual(self.consider(c)['reason'], 'canonical_signal_check_missing')
        checked = self.engine.confirm_proofs(signal_proofs(c.metadata['strategy_signals']), self.rpc)
        retry = replace(c, event_id='checked-after-restart', metadata={**c.metadata, 'canonical_checked_at': checked})
        self.assertEqual(self.consider(retry)['verdict'], 'PAPER_BUY')

    def test_actual_reorg_prevents_receipt_and_later_local_revocation_prevents_entry(self):
        c = self.candidate()
        self.rpc.hash = '0x' + '88' * 32
        with self.assertRaises(ReorgError):
            self.engine.confirm_proofs(signal_proofs(c.metadata['strategy_signals']), self.rpc)
        self.store.db.execute('UPDATE evm_events SET canonical=0')
        self.assertEqual(self.consider(c)['reason'], 'canonical_signal_check_missing')

    def test_failed_rpc_recheck_revokes_previous_success_even_before_journal_reconciliation(self):
        c = self.candidate()
        self.rpc.hash = '0x' + '88' * 32
        with self.assertRaises(ReorgError):
            self.engine.confirm_proofs(signal_proofs(c.metadata['strategy_signals']), self.rpc)
        self.assertEqual(self.consider(c)['reason'], 'canonical_signal_check_missing')
        self.assertEqual(self.ledger.snapshot()['cash'], D(1000))

    def test_unselected_path_cannot_backdate_a_later_confirmation_before_decision(self):
        c = self.candidate(self.two_proofs())
        metadata = deepcopy(c.metadata)
        # A's selected purchase precedes the decision; B's other purchase does not.
        newer = make_proof(wallet=OTHER, tx=TX2, pool=POOL2, at=self.now-1)
        self.store.db.execute('DELETE FROM paper_confirmations WHERE tx_hash=?', (TX2,))
        self.store.db.execute('DELETE FROM evm_events WHERE tx_hash=?', (TX2,))
        self.persist([newer])
        signals = evaluate_signals([make_proof(), newer], token=TOKEN, now=self.now,
            last_closed_at=None, rules=self.rules, registry=self.registry)
        checked = self.engine.confirm_proofs(signal_proofs(signals), self.rpc)
        metadata.update(strategy_signals=signals, canonical_checked_at=checked, decision_at=self.now-5)
        c = replace(c, observed_at=self.now-5, safety=replace(c.safety, observed_at=self.now-5), metadata=metadata)
        result = self.consider(c)
        self.assertEqual(result['verdict'], 'SKIP')
        self.assertFalse(self.ledger.positions())

    def test_modified_proof_cannot_reuse_check_for_original_payload(self):
        c = self.candidate()
        metadata = deepcopy(c.metadata)
        for signal in metadata['strategy_signals']:
            for proof in signal['source_proofs']:
                proof['spent_usd'] = '200'
        self.assertEqual(self.consider(replace(c, metadata=metadata))['reason'], 'canonical_signal_check_missing')

    def test_source_and_rules_are_rechecked_after_candidate_creation(self):
        c = self.candidate()
        with self.ledger.transaction():
            self.ledger._set('entry_revision', 'changed-after-check')
        self.assertEqual(self.consider(c)['reason'], 'entry_revision_mismatch')
        self.pin()
        metadata = {**c.metadata, 'source_id': 'wrong-source'}
        self.assertEqual(self.consider(replace(c, event_id='wrong-source', metadata=metadata))['reason'], 'entry_source_mismatch')

    def test_different_chain_asset_cannot_reuse_robinhood_confirmation(self):
        c = self.candidate()
        asset = 'eip155:1/erc20:' + TOKEN
        c = replace(c, asset_id=asset, safety=replace(c.safety, asset_id=asset))
        q = replace(self.quote(), asset_id=asset)
        self.assertEqual(self.engine.consider(c, q, self.now)['reason'], 'entry_asset_mismatch')

    def test_expired_canonical_check_is_not_refreshed_by_metadata(self):
        c = self.candidate()
        self.now += self.policy.max_quote_age_seconds + 1
        c = replace(c, observed_at=self.now, safety=replace(c.safety, observed_at=self.now),
            metadata={**c.metadata, 'canonical_checked_at': self.now})
        self.assertEqual(self.consider(c)['reason'], 'canonical_signal_check_missing')

    def test_current_registry_can_revoke_a_without_disabling_b(self):
        c = self.candidate(self.two_proofs())
        self.registry = {'status': 'EMPTY', 'wallets': {}}
        result = self.consider(c)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['initiating_strategy'], 'two_wallet_v3')
        self.assertEqual(result['qualified_strategies'], ['two_wallet_v3'])
        self.assertEqual(self.ledger.positions()[0]['strategy'], 'two_wallet_v3')
        self.assertFalse(self.signals('first')['whale_follow']['qualified'])

    def test_many_buyers_keep_all_journal_evidence_but_bound_checked_witness(self):
        proofs = [make_proof(wallet='0x'+f'{i:040x}', tx='0x'+f'{i:064x}',
                             pool=POOL if i % 2 else POOL2) for i in range(1, 41)]
        proofs.append(make_proof())
        c = self.candidate(proofs)
        self.assertLessEqual(len(signal_proofs(c.metadata['strategy_signals'])), 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_confirmations').fetchone()[0], 41)
        self.assertEqual(len(self.book.recent_token_proofs(TOKEN, now=self.now, age=900, minimum_spend=D(10))), 41)
        self.assertEqual(self.consider(c)['verdict'], 'PAPER_BUY')

    def test_signal_pools_do_not_transfer_liquidity_or_safety_to_execution_pool(self):
        c = self.candidate(self.two_proofs(), initiator='two_wallet_v3')
        self.assertEqual(self.consider(c, liquidity_usd=D(29999))['reason'], 'insufficient_liquidity')
        c = replace(c, event_id='wrong-safety-pool', safety=replace(c.safety, details={'report_pool_id': POOL2}))
        self.assertEqual(self.consider(c)['reason'], 'safety_pool_mismatch')
        self.assertEqual(self.ledger.snapshot()['cash'], D(1000))

    def test_known_safety_failure_and_missing_required_provider_still_block(self):
        for key, value, expected in (('sellability', False, 'safety_failed:sellability'),
                                     ('rug_screen_available', None, 'safety_unknown:rug_screen_available')):
            c = self.candidate(event=key)
            checks = {**c.safety.checks, key: value}
            result = self.consider(replace(c, safety=replace(c.safety, checks=checks)))
            self.assertEqual(result['reason'], expected)
        self.assertFalse(self.ledger.positions())

    def test_original_stop_and_costs_keep_initiating_strategy_attribution(self):
        result = self.consider(self.candidate())
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertLessEqual(result['full_loss_risk_usd'], D(5))
        self.assertEqual(self.ledger.snapshot()['cash'] + self.ledger.snapshot()['position_basis'], D(1000))
        self.now += 1
        actions = self.engine.watch({ASSET: self.quote(price='0.1')}, self.now)
        self.assertEqual(actions[0]['reason'], 'stop_loss')
        rows = self.ledger.conn.execute('SELECT action,strategy FROM fills ORDER BY id').fetchall()
        self.assertEqual([(r['action'], r['strategy']) for r in rows], [('OPEN', 'whale_follow'), ('CLOSE', 'whale_follow')])
        closed = self.ledger.conn.execute('SELECT realized_pnl FROM closed_positions').fetchone()
        self.assertLess(D(closed['realized_pnl']), 0)
        self.assertFalse(self.ledger.positions())

    def test_a_cooldown_does_not_become_b_cooldown_and_b_needs_new_buyers(self):
        self.rules = entry_rules(whale_reentry='cooldown_24h')
        self.pin()
        self.engine = self.new_engine()
        self.assertEqual(self.consider(self.candidate())['verdict'], 'PAPER_BUY')
        self.now += 1
        self.engine.watch({ASSET: self.quote(price='0.1')}, self.now)
        self.now += 20
        fresh = [make_proof(tx='0x'+'12'*32, at=self.now-2),
                 make_proof(wallet=OTHER, tx='0x'+'13'*32, pool=POOL2, at=self.now-1)]
        c = self.candidate(fresh, event='a-cooldown')
        result = self.consider(c)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['initiating_strategy'], 'two_wallet_v3')
        self.assertEqual(result['qualified_strategies'], ['two_wallet_v3'])
        self.assertFalse(self.signals('a-cooldown')['whale_follow']['qualified'])
        self.assertEqual(self.ledger.positions()[0]['strategy'], 'two_wallet_v3')

    def test_b_cannot_reenter_using_two_preclose_purchases(self):
        c = self.candidate(self.two_proofs(), initiator='two_wallet_v3')
        self.assertEqual(self.consider(c)['verdict'], 'PAPER_BUY')
        self.now += 1
        self.engine.watch({ASSET: self.quote(price='0.1')}, self.now)
        self.now += 1
        checked = self.engine.confirm_proofs(signal_proofs(c.metadata['strategy_signals']), self.rpc)
        retry = replace(c, event_id='preclose-replay', observed_at=self.now,
            safety=replace(c.safety, observed_at=self.now),
            metadata={**c.metadata, 'canonical_checked_at': checked})
        self.assertEqual(self.consider(retry)['reason'], 'entry_strategy_no_longer_qualified')
        self.assertFalse(self.ledger.positions())

    def test_a_fresh_postclose_rule_requires_only_its_one_new_eligible_buyer(self):
        self.assertEqual(self.consider(self.candidate())['verdict'], 'PAPER_BUY')
        self.now += 1
        self.engine.watch({ASSET: self.quote(price='0.1')}, self.now)
        self.now += 20
        fresh = [make_proof(tx='0x'+'12'*32, at=self.now-1)]
        result = self.consider(self.candidate(fresh, event='one-fresh-whale'))
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertEqual(result['observed_wallets'], 1)

    def test_deadline_blocks_new_entries_without_disabling_original_exit(self):
        self.assertEqual(self.consider(self.candidate())['verdict'], 'PAPER_BUY')
        with self.ledger.transaction():
            self.ledger._set('trial_manifest', json_text({'source_id': self.store.identity, 'ends_at': self.now}))
        self.assertEqual(self.consider(self.candidate(event='after-deadline'))['reason'], 'trial_entry_deadline')
        self.now += 1
        result = self.engine.watch({ASSET: self.quote(price='0.1')}, self.now)
        self.assertEqual(result[0]['reason'], 'stop_loss')


if __name__ == '__main__':
    unittest.main()
