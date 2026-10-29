"""Read-only signal attribution and immutable legacy-comparison coverage."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from sniper.console import ConsoleData
from sniper.engine import PaperEngine
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger, json_text
from sniper.paper_comparison import legacy_entry_candidate
from sniper.policy import RiskPolicy
from test_engine import NOW, candidate, quote
import test_paper_comparison as comparison_fixtures

TOKEN = '0x' + '11' * 20
POOL = '0x' + '22' * 20
OTHER_POOL = '0x' + '33' * 20
ASSET = 'eip155:4663/erc20:' + TOKEN


def proof(index, **changes):
    return dict(token_address=TOKEN, pool=POOL, chain_id=4663,
        wallet='0x' + f'{index:040x}', tx_hash='0x' + f'{index:064x}',
        block_hash='0x' + '44' * 32, block_number=100, log_index=index,
        observed_at=NOW + 1, spent_usd='100', verified_swap=True,
        factory_verified=True) | changes


class DualComparisonTests(unittest.TestCase):
    setUp = comparison_fixtures.ComparisonTests.setUp
    tearDown = comparison_fixtures.ComparisonTests.tearDown
    open = comparison_fixtures.ComparisonTests.open
    q = comparison_fixtures.ComparisonTests.q

    def dual(self, proofs=None, *, event='dual', **metadata):
        proofs = [proof(1), proof(2)] if proofs is None else proofs
        c = candidate(event=event, asset=ASSET, now=NOW + 1,
            strategy=ExploratoryEngine.strategy_name)
        c = replace(c, safety=replace(c.safety, checks={**c.safety.checks, 'rug_screen_available': True}),
            metadata={'source_id': 'source-uuid', 'decision_at': NOW + 1, 'entry_revision': 'dual-revision',
                'initiating_strategy': 'whale_follow', 'qualified_strategies': ['whale_follow', 'two_wallet_v3'],
                'canonical_checked_at': NOW + 1,
                'strategy_signals': [{'strategy_id': 'whale_follow', 'qualified': True, 'source_proofs': [proofs[0]]},
                    {'strategy_id': 'two_wallet_v3', 'qualified': True, 'source_proofs': proofs}], **metadata})
        return c, self.q(NOW + 1, asset=ASSET, pool_id=POOL)

    def capture(self, c, q, now=NOW + 1):
        self.comparison.capture(c.event_id, 'candidate', now,
            {'candidate': c.as_dict(), 'quote': q.as_dict(), 'cycle': now})

    def test_same_pool_proofs_preserve_legacy_rule_and_source_attribution(self):
        c, q = self.dual()
        narrowed = legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid')
        self.assertEqual(len(narrowed.confirmations), 2)
        self.assertNotIn('entry_revision', narrowed.metadata)
        self.assertNotIn('initiating_strategy', narrowed.metadata)
        self.assertEqual(narrowed.metadata['comparison_source_signals'], c.metadata['strategy_signals'])
        self.capture(c, q); self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 1)
            row = json.loads(ledger.conn.execute('SELECT payload FROM decisions').fetchone()[0])
            self.assertEqual(row['candidate']['metadata']['comparison_entry_scope'], 'legacy_same_pool_two_wallet')
        self.assertEqual(self.source.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)

    def test_cross_pool_only_is_durable_gap_and_restart_does_not_replay(self):
        c, q = self.dual([proof(1), {**proof(2), 'pool': OTHER_POOL}])
        self.capture(c, q); self.comparison.process()
        self.comparison.close(); self.comparison = self.open(); self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)
            gaps = ledger.conn.execute("SELECT payload FROM events WHERE kind='comparison_gap'").fetchall()
            self.assertEqual(len(gaps), 1)
            self.assertEqual(json.loads(gaps[0][0])['reason'], 'entry_strategy_revision_not_comparable')
            self.assertEqual(ledger._get('comparison_cursor'), '1')

    def test_qualifying_same_pool_subset_can_coexist_with_other_pool(self):
        c, q = self.dual([proof(1), {**proof(2), 'pool': OTHER_POOL}, proof(3)])
        narrowed = legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid')
        self.assertEqual({p.wallet for p in narrowed.confirmations}, {proof(1)['wallet'], proof(3)['wallet']})

    def test_missing_mismatched_and_stale_binding_never_admits(self):
        for change in ({'source_id': 'other-source'}, {'canonical_checked_at': None},
                       {'canonical_checked_at': NOW + 2}, {'canonical_checked_at': NOW - 10000},
                       {'strategy_signals': [{'strategy_id': 'whale_follow', 'qualified': True, 'source_proofs': [proof(1)]}]}):
            with self.subTest(change=change):
                c, q = self.dual(**change)
                self.assertIsNone(legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid'))
        for field, value in (('token_address', '0x' + '55' * 20), ('chain_id', 1),
                             ('verified_swap', False), ('factory_verified', False), ('spent_usd', 'NaN'),
                             ('block_hash', 'missing'), ('observed_at', NOW - 10000)):
            with self.subTest(field=field):
                c, q = self.dual([proof(1), {**proof(2), field: value}])
                self.assertIsNone(legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid'))

    def test_same_wallet_or_same_transaction_does_not_form_legacy_pair(self):
        for field in ('wallet', 'tx_hash'):
            c, q = self.dual([proof(1), {**proof(2), field: proof(1)[field]}])
            self.assertIsNone(legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid'))

    def test_dual_selectors_without_revision_do_not_fall_back_to_legacy(self):
        for revision in (None, '', {}):
            c, q = self.dual(entry_revision=revision)
            self.assertIsNone(legacy_entry_candidate(c, q, NOW + 1, self.policy, 'source-uuid'))

    def test_noncomparable_input_does_not_stop_existing_original_exits(self):
        c, q = self.dual(); self.capture(c, q); self.comparison.process()
        c2, q2 = self.dual([proof(1), {**proof(2), 'pool': OTHER_POOL}], event='cross-pool')
        self.capture(c2, q2, NOW + 2)
        self.comparison.capture('stop', 'watch', NOW + 3,
            {'quotes': [self.q(NOW + 3, '.5', asset=ASSET, pool_id=POOL).as_dict()]})
        self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.snapshot()['open_positions'], 0)
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 2)
        self.assertEqual(self.comparison.manifest, self.manifest)


class EntrySignalConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'runtime/exploration'; self.folder.mkdir(parents=True)
        self.ledger = Ledger(self.folder / 'ledger.sqlite3', D(1000))
        self.policy = RiskPolicy(); self.engine = PaperEngine(self.ledger, self.policy)
        self.trial = {'account_id': self.ledger._get('account_id'), 'started_at': NOW,
            'ends_at': NOW + 604800, 'policy_version': self.policy.version,
            'entry_revision': 'dual-revision', 'entry_revision_started_at': NOW + 1}
        (self.folder / 'trial.json').write_text(json.dumps(self.trial))
        (self.folder / 'status.json').write_text(json.dumps({'finished_at': NOW + 5, 'errors': []}))
        self.data = ConsoleData(self.root)

    def tearDown(self):
        self.ledger.close(); self.tmp.cleanup()

    def signal(self, strategy, now, **change):
        payload = dict(signal_key=strategy + ':signal', event_id='candidate', asset_id=ASSET,
            symbol='TEST', strategy_id=strategy, entry_revision='dual-revision', qualified=True,
            qualification_reason='verified_purchase', observed_wallets=2, required_wallets=2,
            source_pools=[POOL, OTHER_POOL], execution_status='not_evaluated',
            execution_reason=None, initiating_strategy=None, qualified_strategies=[strategy],
            source_proofs=[{'credential': 'MUST_NOT_ESCAPE'}]) | change
        self.ledger.record_event(strategy + ':' + str(now), 'entry_strategy_signal', payload, now)

    def test_two_paths_visible_even_before_signals_without_writes(self):
        before = self.ledger.conn.total_changes
        view = self.data.robinhood()['entry_strategies']
        self.assertEqual([s['strategy_id'] for s in view['strategies']], ['whale_follow', 'two_wallet_v3'])
        self.assertEqual(view['signals'], [])
        self.assertEqual(self.ledger.conn.total_changes, before)

    def test_latest_append_only_state_preserves_both_paths_and_held_qualification(self):
        self.signal('whale_follow', NOW + 1)
        self.signal('whale_follow', NOW + 2, execution_status='opened', initiating_strategy='whale_follow')
        self.signal('two_wallet_v3', NOW + 3, execution_status='blocked', execution_reason='already_open',
                    initiating_strategy='whale_follow', qualified_strategies=['whale_follow', 'two_wallet_v3'])
        before = self.ledger.conn.total_changes
        view = self.data.robinhood()['entry_strategies']
        self.assertEqual(view['projected_signal_count'], 2)
        states = {s['strategy_id']: s for s in view['signals']}
        self.assertEqual(states['whale_follow']['execution_status'], 'opened')
        self.assertTrue(states['two_wallet_v3']['qualified'])
        self.assertEqual(states['two_wallet_v3']['execution_reason'], 'already_open')
        self.assertNotIn('source_proofs', json.dumps(view)); self.assertNotIn('MUST_NOT_ESCAPE', json.dumps(view))
        self.assertEqual(self.ledger.conn.total_changes, before)
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)

    def test_current_rejections_require_entry_revision_and_activation(self):
        values = [('old-time', NOW, 'dual-revision'), ('old-rules', NOW + 2, 'older'),
                  ('missing', NOW + 2, None), ('current', NOW + 3, 'dual-revision')]
        with self.ledger.transaction():
            for event, at, revision in values:
                self.ledger.conn.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?)', (event, ASSET, at, 'SKIP', 'already_open',
                    json_text({'candidate': {'metadata': {'entry_revision': revision}}, 'result': {'policy_version': self.policy.version}})))
        view = self.data.robinhood()
        self.assertEqual(view['rejection_scopes']['current_policy']['decision_count'], 1)
        self.assertEqual(view['rejection_scopes']['account']['decision_count'], 4)
        self.assertEqual(view['rejection_scopes']['current_policy']['entry_revision'], 'dual-revision')

    def test_pinned_observed_and_reviewed_eligibility_are_distinct_public_fields(self):
        for eligibility in ('observed_recipient', 'reviewed_tracked'):
            self.trial['entry_rules'] = {'whale_follow': {'eligibility': eligibility, 'unrelated_private': 'NOT_PUBLIC'}}
            (self.folder / 'trial.json').write_text(json.dumps(self.trial))
            view = self.data.robinhood()['entry_strategies']
            self.assertEqual(view['strategies'][0]['eligibility'], eligibility)
            self.assertNotIn('NOT_PUBLIC', json.dumps(view))

    def test_position_and_exits_keep_initiator_without_separate_returns(self):
        c = candidate(now=NOW + 1, metadata={'decision_at': NOW + 1, 'entry_revision': 'dual-revision',
            'initiating_strategy': 'whale_follow', 'qualified_strategies': ['whale_follow', 'two_wallet_v3']})
        self.engine.consider(c, quote(now=NOW + 1), NOW + 1)
        view = self.data.robinhood()
        self.assertEqual(view['snapshot']['positions'][0]['initiating_strategy'], 'whale_follow')
        self.engine.watch({c.asset_id: quote(now=NOW + 2, price='.5')}, NOW + 2)
        view = self.data.robinhood()
        self.assertEqual(len(view['fills']), 2)
        self.assertTrue(all(f['initiating_strategy'] == 'whale_follow' for f in view['fills']))
        self.assertEqual(view['closed'][0]['initiating_strategy'], 'whale_follow')
        self.assertNotIn('returns', view['entry_strategies'])


if __name__ == '__main__':
    unittest.main()
