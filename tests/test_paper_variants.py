import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger, json_text
from sniper.paper_variants import VariantEngine, modeled_proceeds
from sniper.policy import RiskPolicy
from test_engine import NOW, candidate, quote


POLICY = replace(RiskPolicy(), min_independent_wallets=2, min_liquidity_usd=D(30000),
                 cooldown_seconds=0, max_open_positions=0, max_entries_per_day=0)


def eligible(event='one', now=NOW):
    c = candidate(event=event, now=now, strategy=ExploratoryEngine.strategy_name)
    return replace(c, safety=replace(c.safety, checks={**c.safety.checks, 'rug_screen_available': True}))


def bounded(now=NOW, price='1', **kwargs):
    return quote(now=now, price=str(price), route_max_notional_usd=D(100),
                 route_max_sell_quantity=D(100), route_impact_bps=D(0), **kwargs)


def bind(ledger, variant):
    binding = {'variant': variant, 'comparison_id': 'isolated-synthetic-test',
               'source_account_id': 'different-source-account', 'source_id': 'fixture-source',
               'started_at': NOW, 'entry_deadline': NOW + 7 * 86400,
               'excluded_from_strategy': True, 'synthetic': True}
    with ledger.transaction():
        ledger._set('paper_comparison', json_text(binding))
    return binding


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'comparison.sqlite3'
        self.ledger = Ledger(self.path, POLICY.starting_cash_usd)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def engine(self, variant):
        bind(self.ledger, variant)
        return VariantEngine(self.ledger, POLICY, variant)

    def buy(self, engine, event='one', now=NOW, price='1'):
        result = engine.consider(eligible(event, now), bounded(now, price), now)
        self.assertEqual(result['verdict'], 'PAPER_BUY', result)
        return self.ledger.positions()[0]

    def mark(self, engine, now, price, **kwargs):
        q = bounded(now, price, **kwargs)
        return engine.watch({q.asset_id: q}, now)

    def test_requires_explicit_bound_separate_account(self):
        with self.assertRaisesRegex(ValueError, 'isolated'):
            VariantEngine(self.ledger, POLICY, 'A')
        binding = bind(self.ledger, 'A')
        binding['source_account_id'] = self.ledger._get('account_id')
        self.ledger._set('paper_comparison', json_text(binding))
        with self.assertRaisesRegex(ValueError, 'isolated'):
            VariantEngine(self.ledger, POLICY, 'A')
        self.assertIsNone(self.ledger._get('paper_comparison_binding'))
        self.assertIsNone(self.ledger._get('policy_version'))

    def test_binding_is_immutable_and_deadline_never_extends(self):
        engine = self.engine('A')
        binding = json.loads(self.ledger._get('paper_comparison'))
        binding['entry_deadline'] += 100
        self.ledger._set('paper_comparison', json_text(binding))
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            VariantEngine(self.ledger, POLICY, 'A')
        self.assertEqual(engine.binding['entry_deadline'], NOW + 7 * 86400)

    def test_frozen_stop_loss_budget_uses_costs_and_actual_quantity(self):
        engine = self.engine('A')
        p = self.buy(engine)
        record = engine._position_record(p['position_id'])
        stop = p['entry_price'] * D('.65')
        expected = p['total_entry_cost'] - modeled_proceeds(p['qty'], bounded(), price=stop)
        self.assertEqual(D(record['stop_threshold']), stop)
        self.assertEqual(D(record['stop_loss_budget']), expected)
        self.assertNotEqual(expected, p['total_entry_cost'] * D('.35'))
        self.mark(engine, NOW + 1, '1.2')
        self.assertIsNone(engine._position_record(p['position_id'])['armed_at'])

    def test_break_even_arm_survives_restart_and_records_real_costed_loss_on_gap(self):
        engine = self.engine('A')
        p = self.buy(engine)
        self.assertEqual(self.mark(engine, NOW + 1, '1.5'), [])
        self.assertEqual(engine._position_record(p['position_id'])['armed_at'], NOW + 1)
        self.ledger.close()
        self.ledger = Ledger(self.path, POLICY.starting_cash_usd, require_existing=True)
        engine = VariantEngine(self.ledger, POLICY, 'A')
        self.mark(engine, NOW + 2, '1.5')
        self.assertEqual(self.ledger.conn.execute("SELECT count(*) FROM events WHERE kind='comparison_break_even_armed'").fetchone()[0], 1)
        result = self.mark(engine, NOW + 3, '1')[0]
        self.assertEqual(result['reason'], 'break_even_protection')
        self.assertLess(result['pnl'], 0)
        self.assertFalse(self.ledger.positions())
        close = self.ledger.conn.execute("SELECT * FROM closed_positions").fetchone()
        self.assertEqual(D(close['realized_pnl']), result['pnl'])
        self.assertEqual(engine._position_record(p['position_id'])['close_reason'], 'break_even_protection')

    def test_hard_stop_precedes_break_even_and_unavailable_quote_cannot_close(self):
        engine = self.engine('A')
        p = self.buy(engine)
        self.mark(engine, NOW + 1, '1.5')
        unresolved = engine.watch({}, NOW + 2)
        self.assertEqual(unresolved[0]['reason'], 'missing_quote')
        self.assertEqual(self.ledger.positions()[0]['qty'], p['qty'])
        result = self.mark(engine, NOW + 3, '.5')[0]
        self.assertEqual(result['reason'], 'stop_loss')

    def test_partial_take_profit_retains_original_trailing_exit(self):
        engine = self.engine('A')
        p = self.buy(engine)
        self.mark(engine, NOW + 1, '1.5')
        partial = self.mark(engine, NOW + 2, '2.1')[0]
        self.assertEqual(partial['reason'], 'take_profit')
        self.assertEqual(partial['quantity'], p['qty0'] * POLICY.take_profit_fraction)
        close = self.mark(engine, NOW + 3, '1.4')[0]
        self.assertEqual(close['reason'], 'trailing_stop')

    def test_inherited_positions_keep_original_exits(self):
        active_engine = ExploratoryEngine(self.ledger, POLICY)
        p = self.buy(active_engine)
        engine = self.engine('A')
        self.assertTrue(engine._position_record(p['position_id'])['inherited_original_exits'])
        self.mark(engine, NOW + 1, '1.5')
        self.assertIsNone(engine._position_record(p['position_id'])['armed_at'])
        self.assertEqual(self.mark(engine, NOW + 2, '1'), [])
        self.assertEqual(self.ledger.positions()[0]['qty'], p['qty'])

    def test_B_requires_strict_recovery_and_fresh_buyers_only_after_hard_stop(self):
        engine = self.engine('B')
        p = self.buy(engine)
        threshold = D(engine._position_record(p['position_id'])['stop_threshold'])
        self.mark(engine, NOW + 1, '.5')
        for at, price in ((NOW + 2, D('.58')), (NOW + 3, threshold)):
            result = engine.consider(eligible(str(at), at), bounded(at, price), at)
            self.assertEqual(result['reason'], 'stop_recovery_required')
            self.assertEqual(result['previous_stop_threshold'], threshold)
        old_buyers = replace(eligible('old-buyers', NOW + 4), confirmations=eligible().confirmations)
        result = engine.consider(old_buyers, bounded(NOW + 4, '1'), NOW + 4)
        self.assertEqual(result['reason'], 'reentry_requires_new_buyers_after_close')
        self.buy(engine, 'recovered', NOW + 5, threshold + D('.0001'))
        entry = json.loads(self.ledger.conn.execute("SELECT payload FROM events WHERE event_id='comparison:entry:position:recovered'").fetchone()[0])
        self.assertEqual(entry['previous_exit_reason'], 'stop_loss')
        self.assertEqual(entry['previous_position_id'], p['position_id'])

    def test_B_time_exit_reentry_has_no_recovery_rule(self):
        engine = self.engine('B')
        self.buy(engine)
        at = NOW + POLICY.time_stop_seconds
        self.assertEqual(self.mark(engine, at, '1')[0]['reason'], 'time_stop')
        self.buy(engine, 'time-reentry', at + 1, '.5')

    def test_B_seed_history_reconstructs_only_audited_original_stop(self):
        source = ExploratoryEngine(self.ledger, POLICY)
        p = self.buy(source)
        self.mark(source, NOW + 1, '.5')
        engine = self.engine('B')
        result = engine.consider(eligible('blocked', NOW + 2), bounded(NOW + 2, '.6'), NOW + 2)
        self.assertEqual(result['reason'], 'stop_recovery_required')
        self.assertEqual(result['threshold_source'], 'audited_original_entry_fill_and_policy')
        self.assertEqual(result['previous_stop_threshold'], p['entry_price'] * D('.65'))
        self.ledger.conn.execute("DELETE FROM decisions WHERE event_id='one'")
        result = engine.consider(eligible('unknown', NOW + 3), bounded(NOW + 3, '1'), NOW + 3)
        self.assertEqual(result['reason'], 'stop_recovery_evidence_missing')

    def test_size_coverage_fails_closed_for_entry_and_mark(self):
        engine = self.engine('A')
        result = engine.consider(eligible('unbounded'), quote(), NOW)
        self.assertEqual(result['reason'], 'comparison_quote_size_coverage_missing')
        p = self.buy(engine)
        self.mark(engine, NOW + 1, '1.5')
        q = replace(bounded(NOW + 2, '1'), route_max_sell_quantity=p['qty'] / 2)
        result = engine.watch({q.asset_id: q}, NOW + 2)
        self.assertEqual(result[0]['reason'], 'route_size_not_covered')
        self.assertEqual(self.ledger.positions()[0]['qty'], p['qty'])

    def test_deadline_blocks_entries_but_original_exits_continue(self):
        engine = self.engine('baseline')
        at = engine.binding['entry_deadline']
        self.buy(engine)
        result = self.mark(engine, at, '.5')[0]
        self.assertEqual(result['reason'], 'stop_loss')
        result = engine.consider(eligible('late', at + 1), bounded(at + 1), at + 1)
        self.assertEqual(result['reason'], 'comparison_entry_deadline')
        self.assertFalse(self.ledger.positions())

    def test_atomic_entry_and_variant_audit_rollback(self):
        engine = self.engine('A')
        before = self.ledger.snapshot()
        with patch.object(engine, '_position_opened', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                engine.consider(eligible(), bounded(), NOW)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 0)
        self.buy(engine)
        self.assertEqual(engine.consider(eligible(), bounded(), NOW)['reason'], 'duplicate_event')
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM paper_variant_positions').fetchone()[0], 1)

    def test_close_and_experiment_audit_rollback_together(self):
        engine = self.engine('A')
        self.buy(engine)
        before = self.ledger.snapshot()
        with patch.object(engine, '_position_closed', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.mark(engine, NOW + 1, '.5')
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0], 1)
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM closed_positions').fetchone()[0], 0)
        self.assertEqual(self.mark(engine, NOW + 1, '.5')[0]['reason'], 'stop_loss')

    def test_baseline_retains_profitable_giveback_without_arming(self):
        engine = self.engine('baseline')
        p = self.buy(engine)
        self.assertEqual(self.mark(engine, NOW + 1, '1.8'), [])
        self.assertEqual(self.mark(engine, NOW + 2, '1'), [])
        self.assertIsNone(engine._position_record(p['position_id'])['armed_at'])
        self.assertEqual(self.ledger.positions()[0]['qty'], p['qty'])


if __name__ == '__main__':
    unittest.main()
