"""Parallel strategy market notices, using isolated committed paper decisions."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from sniper.entry_strategies import evaluate_signals
from sniper.ledger import json_text
from sniper.market_alerts import record_market_signal, MAX_AGE, POOL_COOLDOWN, HOURLY_LIMIT
from sniper.parallel_entry import PROFILE, record_signals
import test_parallel_entry_engine as engine_fixtures
from test_entry_strategies import make_proof, NOW, WALLET, OTHER, POOL, POOL2, TOKEN, TX2


class ParallelMarketAlertsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = engine_fixtures.ParallelEntryEngineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger

    def observation(self, *, proofs=None, initiator='whale_follow', event='notice', commit=True):
        proofs = proofs if proofs is not None else self.fixture.two_proofs()
        c = self.fixture.candidate(proofs, initiator=initiator, event=event)
        c = replace(c, metadata={**c.metadata, 'profile': PROFILE, 'proof_event': event + ':proof',
                                'decision_at': NOW},
                    safety=replace(c.safety, checks={**c.safety.checks, 'liquidity_controls': None}))
        self.ledger.record_event(event + ':proof', 'exploratory_wallet_buy', proofs[0], NOW)
        q = self.fixture.quote()
        result = self.fixture.engine.consider(c, q, NOW) if commit else {'verdict': 'PAPER_BUY', 'reason': 'eligible'}
        return c, q, result

    def record(self, c, q, result, now=NOW):
        return record_market_signal(self.ledger, c, q, result, now)

    def messages(self):
        return [json.loads(row['payload']) for row in self.ledger.conn.execute(
            "SELECT payload FROM events WHERE kind='market_signal' ORDER BY event_id")]

    def test_a_initiated_b_also_qualified_uses_two_recipient_signal(self):
        c, q, result = self.observation()
        self.assertEqual(len(c.confirmations), 1)
        self.assertEqual(result['initiating_strategy'], 'whale_follow')
        self.assertIn('two_wallet_v3', result['qualified_strategies'])
        before = self.ledger.snapshot()
        self.assertTrue(self.record(c, q, result))
        self.assertFalse(self.record(c, q, result))
        self.assertEqual(self.ledger.snapshot(), before)
        payload = self.messages()[0]
        self.assertEqual(payload['signal_strategy'], 'two_wallet_v3')
        self.assertEqual(payload['signal_pool_ids'], [POOL, POOL2])
        self.assertEqual(payload['pool_id'], POOL)
        text = payload['message']
        self.assertIn('Buying recipients: 2', text)
        self.assertIn('Execution pool: ' + POOL, text)
        self.assertIn('Signal across pools: ' + POOL + ', ' + POOL2, text)
        self.assertIn('liquidity_controls', text)
        self.assertIn('do not establish wallet profitability or controller independence', text)
        self.assertIn('not a buy instruction or safety guarantee', text)

    def test_a_single_buyer_never_becomes_a_market_notice(self):
        c, q, result = self.observation(proofs=[make_proof()])
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertNotIn('two_wallet_v3', result['qualified_strategies'])
        self.assertFalse(self.record(c, q, result))
        self.assertEqual(self.messages(), [])

    def test_same_pool_two_buyers_still_produce_one_notice(self):
        c, q, result = self.observation(proofs=[make_proof(), make_proof(wallet=OTHER, tx=TX2)])
        self.assertTrue(self.record(c, q, result))
        self.assertEqual(self.messages()[0]['signal_pool_ids'], [POOL])
        self.assertIn('Signal pool: ' + POOL, self.messages()[0]['message'])

    def test_b_without_a_review_registry_can_notify(self):
        self.fixture.registry = {'status': 'EMPTY', 'wallets': {}}
        c, q, result = self.observation(initiator='two_wallet_v3')
        self.assertEqual(result['qualified_strategies'], ['two_wallet_v3'])
        self.assertTrue(self.record(c, q, result))

    def test_a_revoked_at_transaction_b_fallback_uses_committed_candidate(self):
        c, q, _ = self.observation(commit=False)
        self.assertEqual(c.metadata['initiating_strategy'], 'whale_follow')
        self.fixture.registry = {'status': 'EMPTY', 'wallets': {}}
        result = self.fixture.engine.consider(c, q, NOW)
        self.assertEqual(result['initiating_strategy'], 'two_wallet_v3')
        self.assertEqual(result['qualified_strategies'], ['two_wallet_v3'])
        self.assertTrue(self.record(c, q, result))

    def test_proposed_metadata_without_committed_decision_never_notifies(self):
        c, q, proposed = self.observation(commit=False)
        proposed.update(qualified_strategies=['whale_follow', 'two_wallet_v3'],
                        entry_revision=c.metadata['entry_revision'])
        self.assertFalse(self.record(c, q, proposed))
        self.assertEqual(self.messages(), [])

    def test_forged_outer_b_metadata_cannot_upgrade_committed_a_only(self):
        c, q, result = self.observation(proofs=[make_proof()])
        forged_signals = evaluate_signals(self.fixture.two_proofs(), token=TOKEN, now=NOW,
            last_closed_at=None, rules=self.fixture.rules, registry=self.fixture.registry)
        c = replace(c, metadata={**c.metadata, 'strategy_signals': forged_signals,
                                 'qualified_strategies': ['whale_follow', 'two_wallet_v3']})
        self.assertFalse(self.record(c, q, result))
        forged_result = dict(result, qualified_strategies=['whale_follow', 'two_wallet_v3'])
        self.assertFalse(self.record(c, q, forged_result))

    def test_committed_decision_requires_authoritative_signal_event(self):
        c, q, result = self.observation()
        self.ledger.conn.execute("DELETE FROM events WHERE kind='entry_strategy_signal'")
        self.assertFalse(self.record(c, q, result))

    def test_uncommitted_financial_transaction_cannot_publish_market_notice(self):
        c, q, result = self.observation()
        with self.ledger.transaction():
            self.assertFalse(self.record(c, q, result))
        self.assertEqual(self.messages(), [])

    def test_changed_revision_source_or_profile_cannot_relabel_old_decision(self):
        c, q, result = self.observation()
        for field, value in (('entry_revision', 'wrong'), ('source_id', 'wrong'),
                             ('proof_event', 'wrong'), ('profile', 'two-wallet-30k-reentry')):
            with self.subTest(field=field):
                changed = replace(c, metadata={**c.metadata, field: value})
                self.assertFalse(self.record(changed, q, result))
        self.ledger._set('entry_revision', 'different-active-revision')
        self.assertFalse(self.record(c, q, result))

    def test_selected_execution_pool_proof_and_quote_cannot_borrow_other_pool(self):
        c, q, result = self.observation()
        self.assertFalse(self.record(c, replace(q, pool_id=POOL2), result))
        wrong = make_proof(pool=POOL2)
        self.ledger.conn.execute("UPDATE events SET payload=? WHERE event_id=?",
                                 (json_text(wrong), c.metadata['proof_event']))
        self.assertFalse(self.record(c, q, result))

    def test_missing_coverage_or_known_failure_stays_blocked(self):
        for value in (None, False):
            with self.subTest(goplus=value):
                c, q, _ = self.observation(event='safety-' + str(value), commit=False)
                c = replace(c, safety=replace(c.safety, checks={**c.safety.checks, 'rug_screen_available': value}))
                result = self.fixture.engine.consider(c, q, NOW)
                self.assertFalse(self.record(c, q, result))

    def test_notice_witness_expires_independently_of_fresh_quote(self):
        c, q, result = self.observation()
        # Proofs were observed ten seconds before the quote/decision.
        self.assertFalse(self.record(c, q, result, now=NOW + 891))
        self.assertFalse(self.record(c, q, result, now=NOW + 901))

    def test_queue_limits_remain_the_existing_values_without_sending(self):
        self.assertEqual((HOURLY_LIMIT, POOL_COOLDOWN, MAX_AGE), (3, 6 * 3600, 900))

    def test_forged_committed_b_proof_fields_still_fail_validation(self):
        for field, value in (('chain_id', 1), ('token_address', '0x' + 'bb' * 20),
                             ('factory_verified', False), ('observed_at', NOW + 1),
                             ('wallet', WALLET), ('tx_hash', make_proof()['tx_hash'])):
            with self.subTest(field=field):
                event = 'forged-' + field
                c, q, result = self.observation(event=event)
                row = self.ledger.conn.execute('SELECT payload FROM decisions WHERE event_id=?', (event,)).fetchone()
                saved = json.loads(row['payload'])
                original = self.fixture.signals(event)['two_wallet_v3']
                forged = deepcopy(original)
                forged['source_proofs'][1][field] = value
                saved['candidate']['metadata']['strategy_signals'][1] = forged
                self.ledger.conn.execute('UPDATE decisions SET payload=? WHERE event_id=?', (json_text(saved), event))
                record_signals(self.ledger, event, c.asset_id, c.metadata['entry_revision'], [forged], NOW,
                    execution_status='opened' if result['verdict'] == 'PAPER_BUY' else 'blocked',
                    execution_reason=result['reason'], execution_pool_id=q.pool_id,
                    initiating_strategy=result['initiating_strategy'])
                self.assertFalse(self.record(c, q, result))


if __name__ == '__main__':
    unittest.main()
