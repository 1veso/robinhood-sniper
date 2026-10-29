from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger, json_text
from sniper.paper_comparison import Comparison, initialize
from sniper.policy import RiskPolicy
from test_engine import NOW, candidate, quote


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = replace(RiskPolicy(), min_independent_wallets=2, min_liquidity_usd=D(30000),
                              max_open_positions=0, max_entries_per_day=0, cooldown_seconds=0)
        self.source = Ledger(self.root / 'active.sqlite3', D(1000))
        ExploratoryEngine(self.source, self.policy)
        self.trial = dict(account_id=self.source._get('account_id'), source_id='source-uuid', ends_at=NOW+1000, started_at=NOW-100)
        with self.source.transaction():
            self.source._set('trial_manifest', json.dumps(self.trial, sort_keys=True))
        self.folder = self.root / 'comparison'
        self.manifest = initialize(self.folder, self.root / 'active.sqlite3', self.policy, self.trial, now=NOW)
        self.comparison = self.open()

    def open(self, **changes):
        args = dict(source_account_id=self.trial['account_id'], source_id='source-uuid', policy_version=self.policy.version)
        return Comparison(self.folder, **{**args, **changes})

    def tearDown(self):
        self.comparison.close(); self.source.close(); self.tmp.cleanup()

    def q(self, now, price='1', **changes):
        return quote(now=now, price=price, route_max_notional_usd=D(5), route_max_sell_quantity=D(100), route_impact_bps=D(1), **changes)

    def add_candidate(self, event='entry', now=NOW+1):
        c = candidate(event=event, now=now, strategy=ExploratoryEngine.strategy_name)
        c = replace(c, safety=replace(c.safety, checks={**c.safety.checks, 'rug_screen_available': True}))
        return self.comparison.capture(event, 'candidate', now, {'candidate': c.as_dict(), 'quote': self.q(now).as_dict(), 'cycle': now})

    def test_seed_identity_isolation_and_original_deadline(self):
        self.assertEqual(len(set(self.manifest['accounts'].values()) | {self.trial['account_id']}), 4)
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.snapshot()['cash'], self.source.snapshot()['cash'])
            self.assertEqual(json.loads(ledger._get('paper_comparison'))['entry_deadline'], self.trial['ends_at'])
        self.assertIsNone(self.source._get('paper_comparison'))
        with self.assertRaises(ValueError):
            initialize(self.folder, self.root / 'active.sqlite3', self.policy, self.trial, now=NOW+1)
        with self.assertRaises(ValueError):
            self.open(source_id='wrong')

    def test_resume_exactly_once_and_no_source_fills(self):
        self.add_candidate()
        self.comparison.process()
        self.comparison.close(); self.comparison = self.open()
        self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 1)
            self.assertEqual(ledger._get('comparison_cursor'), '1')
        self.assertEqual(self.source.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)

    def test_financial_operation_and_cursor_rollback_together(self):
        self.add_candidate()
        ledger, _ = self.comparison.portfolios()['baseline']
        original = ledger.record_event
        def fail(event, *args, **kwargs):
            if event.startswith('comparison-input:'):
                raise RuntimeError('simulated interruption before cursor commit')
            return original(event, *args, **kwargs)
        with patch.object(ledger, 'record_event', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.comparison.process()
        self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)
        self.assertEqual(ledger._get('comparison_cursor'), '0')
        self.comparison.process()
        self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 1)

    def test_missing_and_undersized_quotes_are_not_sales(self):
        self.add_candidate(); self.comparison.process()
        self.comparison.capture('empty', 'watch', NOW+2, {'quotes': []})
        self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.snapshot()['unresolved_positions'], 1)
            self.assertEqual(ledger.snapshot()['closed_positions'], 0)

    def test_tape_rejects_ordering_and_identity_conflicts(self):
        self.add_candidate()
        self.assertEqual(self.add_candidate(), 1)
        with self.assertRaises(ValueError):
            self.comparison.capture('earlier', 'gap', NOW, {'reason': 'out_of_order'})
        with self.assertRaises(ValueError):
            self.comparison.capture('entry', 'gap', NOW+1, {'reason': 'same_id_different_payload'})

    def test_changed_input_and_missing_sequence_fail_before_fills(self):
        self.add_candidate()
        with self.comparison.conn:
            self.comparison.conn.execute("UPDATE observations SET payload='{}' WHERE seq=1")
        with self.assertRaisesRegex(ValueError, 'changed payload'):
            self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)

    def test_failed_portfolio_open_never_leaves_partial_roster(self):
        path = self.folder / 'B' / 'ledger.sqlite3'
        ledger = Ledger(path, D(1000), require_existing=True)
        with ledger.transaction():
            ledger._set('account_id', 'changed')
        ledger.close()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                self.comparison.portfolios()
            self.assertEqual(self.comparison.ledgers, {})

    def test_source_halt_clear_does_not_clear_independent_halt(self):
        self.comparison.capture('halt', 'control', NOW, {'entry_halt': 'source_review'})
        self.comparison.process()
        ledger, _ = self.comparison.portfolios()['A']
        with ledger.transaction(): ledger._set('entry_halt', 'independent_review')
        self.comparison.capture('clear', 'control', NOW+1, {'entry_halt': ''})
        self.comparison.process()
        self.assertEqual(ledger._get('entry_halt'), 'independent_review')
        self.assertEqual(self.comparison.portfolios()['baseline'][0]._get('entry_halt'), '')

    def test_deadline_stops_entries_but_original_exit_continues(self):
        self.add_candidate(); self.comparison.process()
        self.comparison.capture('deadline-watch', 'watch', NOW+1001, {'quotes': [self.q(NOW+1001, '.5').as_dict()]})
        self.add_candidate('late', NOW+1002); self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger.snapshot()['open_positions'], 0)
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 2)

    def test_backlog_publish_does_not_advance_past_unprocessed_observations(self):
        self.add_candidate()
        self.comparison.capture('watch', 'watch', NOW+2, {'quotes': [self.q(NOW+2).as_dict()]})
        self.comparison.process(limit=1)
        self.comparison.publish(NOW+20)
        self.comparison.process()
        for ledger, _ in self.comparison.portfolios().values():
            self.assertEqual(ledger._get('comparison_cursor'), '2')


if __name__ == '__main__':
    unittest.main()
