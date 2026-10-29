from dataclasses import replace
from decimal import Decimal as D
import unittest

from sniper.comparison_metrics import report
from sniper.engine import PaperEngine
from sniper.ledger import Ledger, json_text
from sniper.policy import RiskPolicy
from test_engine import NOW, candidate, quote


class ComparisonMetricsTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(':memory:', D(1000))
        self.policy = replace(RiskPolicy(), cooldown_seconds=0,
                              max_entries_per_day=0, max_open_positions=0)
        self.engine = PaperEngine(self.ledger, self.policy)
        self.asset = quote().asset_id

    def tearDown(self):
        self.ledger.close()

    def seed(self, now=NOW):
        snapshot = self.ledger.snapshot()
        with self.ledger.transaction():
            metadata = dict(comparison_id='synthetic-comparison', variant='A',
                source_account_id='source-only-account', source_id='synthetic-source',
                excluded_from_strategy=True, started_at=now, entry_deadline=now + 604800,
                seed_fill_id=self.ledger.conn.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0],
                seed_equity_id=self.ledger.conn.execute('SELECT COALESCE(MAX(id),0) FROM equity_history').fetchone()[0],
                seed_event_rowid=self.ledger.conn.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0],
                seed_closed_ids=[r[0] for r in self.ledger.conn.execute('SELECT position_id FROM closed_positions')],
                seed_equity=str(snapshot['equity']), seed_realized_pnl=str(snapshot['realized_pnl']),
                seed_fees_paid=str(snapshot['fees_paid']), seed_positions=snapshot['positions'],
                inherited_position_ids=[p['position_id'] for p in snapshot['positions']])
            self.ledger._set('paper_comparison', json_text(metadata))
        return metadata

    def buy(self, event, now):
        result = self.engine.consider(candidate(event=event, now=now), quote(now=now), now)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        return result

    def mark(self, price, now):
        q = quote(price=price, now=now)
        return self.engine.watch({q.asset_id: q}, now)

    def test_incremental_totals_keep_prior_token_loss_visible(self):
        self.buy('old-loss', NOW)
        self.mark('.5', NOW + 1)
        old = self.ledger.snapshot()['realized_pnl']
        self.assertLess(old, 0)
        self.seed(NOW + 2)
        self.buy('new-winner', NOW + 3)
        self.mark('3', NOW + 4)  # Original partial profit.
        self.mark('1.8', NOW + 5)  # Original trailing close.
        changes = self.ledger.conn.total_changes
        result = report(self.ledger, NOW + 5)
        self.assertEqual(changes, self.ledger.conn.total_changes)
        self.assertEqual(result['new_entries'], 1)
        self.assertEqual(result['new_closed_positions'], 1)
        self.assertEqual(result['closed_win_rate_denominator'], 1)
        self.assertEqual(D(result['closed_win_rate']), D(1))
        self.assertGreater(D(result['net_pnl']), 0)
        self.assertAlmostEqual(D(result['net_pnl']), self.ledger.snapshot()['realized_pnl'] - old, places=20)
        self.assertEqual(result['reentry_pnl_by_previous_exit_reason'][0]['previous_exit_reason'], 'stop_loss')
        new = result['new_position_pnl_by_token'][0]
        all_time = result['account_to_date_pnl_by_token'][0]
        self.assertAlmostEqual(D(all_time['net_pnl']), D(new['net_pnl']) + old, places=20)
        self.assertEqual(all_time['positions'], 2)
        self.assertEqual(D(result['incremental_pnl_by_token'][0]['net_pnl']), D(result['net_pnl']))

    def test_seed_positions_and_missing_marks_remain_unrealized(self):
        self.buy('inherited', NOW)
        self.seed(NOW + 1)
        result = report(self.ledger, NOW + 100)
        self.assertEqual(result['new_entries'], 0)
        self.assertEqual(result['new_fills'], 0)
        self.assertFalse(result['valuation_complete'])
        self.assertEqual(result['unresolved_positions'][0]['mark_status'], 'stale_quote')
        self.assertEqual(D(result['realized_pnl']), 0)
        self.assertLess(D(result['unrealized_pnl_change']), 0)
        self.assertEqual(self.ledger.positions()[0]['mark_status'], 'ok')
        self.assertEqual(len(self.ledger.positions()), 1)
        self.assertEqual(D(result['incremental_pnl_by_token'][0]['net_pnl']), D(result['net_pnl']))

    def test_gap_cutoff_and_drawdown_use_only_comparison_interval(self):
        self.ledger.record_event('old-gap', 'monitoring_gap', {'seconds': 1000}, NOW)
        self.seed()
        self.buy('new', NOW + 1)
        self.mark('.5', NOW + 2)
        self.ledger.record_event('new-gap', 'comparison_gap', {'reason': 'missing_source'}, NOW + 2)
        self.ledger.record_event('route-failure', 'route_observation_failure', {'reason': 'unavailable'}, NOW + 2)
        result = report(self.ledger, NOW + 2)
        self.assertNotIn('monitoring_gap', result['gap_counts'])
        self.assertEqual(result['gap_counts']['comparison_gap'], 1)
        self.assertEqual(result['gap_counts']['route_observation_failure'], 1)
        self.assertAlmostEqual(D(result['max_observed_drawdown_usd']), -D(result['net_pnl']), places=20)
        self.assertGreater(D(result['max_observed_drawdown_pct']), 0)

    def test_later_protection_outcomes_require_same_pool_and_full_quantity(self):
        self.seed()
        self.buy('protected', NOW + 1)
        p = self.ledger.positions()[0]
        close_quote = quote(now=NOW + 2, price='1.02')
        with self.ledger.transaction():
            self.engine._sell(p, p['qty'], close_quote, NOW + 2, 'break_even_protection')
        def record(name, at, q):
            self.ledger.record_event(name, 'comparison_quote_observation', {'quote': q.as_dict(), 'input_seq': at}, at)
        small = quote(now=NOW + 3, price='10', route_max_notional_usd='1', route_max_sell_quantity='.01', route_impact_bps='1')
        record('too-small', NOW + 3, small)
        result = report(self.ledger, NOW + 3)
        outcome = result['after_protection_outcomes'][0]
        self.assertEqual(outcome['status'], 'UNKNOWN')
        self.assertIsNone(outcome['maximum_observed_holding_net_pnl'])
        wrong = quote(now=NOW + 4, price='100', pool_id='other', route_max_notional_usd='100', route_max_sell_quantity='100', route_impact_bps='1')
        record('wrong-pool', NOW + 4, wrong)
        valid = quote(now=NOW + 5, price='2', route_max_notional_usd='100', route_max_sell_quantity=p['qty'], route_impact_bps='1')
        record('valid', NOW + 5, valid)
        record('same-source-observation', NOW + 6, valid)
        stale = replace(valid, observed_at=NOW + 7)
        record('stale', NOW + 100, stale)
        result = report(self.ledger, NOW + 100)
        outcome = result['after_protection_outcomes'][0]
        self.assertEqual(outcome['status'], 'SAMPLED')
        self.assertEqual(outcome['valid_unique_samples'], 1)
        self.assertEqual(outcome['unknown_observations']['route_size_not_covered'], 1)
        self.assertEqual(outcome['unknown_observations']['pool_mismatch'], 1)
        self.assertEqual(outcome['unknown_observations']['stale_quote'], 1)
        self.assertEqual(outcome['latest_valid']['source_observed_at'], NOW + 5)
        self.assertEqual(outcome['latest_valid_age_seconds'], 95)
        self.assertFalse(outcome['current_followup_valuation_available'])
        self.assertGreater(D(outcome['latest_valid']['difference_from_recorded_close']), 0)
        self.assertEqual(result['new_fills'], 2)

    def test_refuses_live_account_and_past_timestamp(self):
        with self.assertRaisesRegex(ValueError, 'isolated comparison'):
            report(self.ledger, NOW)
        meta = self.seed()
        with self.assertRaisesRegex(ValueError, 'look ahead'):
            report(self.ledger, NOW - 1)
        meta['source_account_id'] = self.ledger._get('account_id')
        self.ledger._set('paper_comparison', json_text(meta))
        with self.assertRaisesRegex(ValueError, 'separate'):
            report(self.ledger, NOW)


if __name__ == '__main__':
    unittest.main()
