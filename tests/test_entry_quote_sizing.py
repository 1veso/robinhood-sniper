"""Affordable entry routes use isolated quotes; no provider or trading calls."""
from dataclasses import replace
from decimal import Decimal as D
import unittest
from unittest.mock import Mock

import explore
from sniper.exploration import ExploratoryEngine
from sniper.engine import PaperEngine
from sniper.ledger import Ledger
from sniper.policy import RiskPolicy
from test_engine import NOW, quote, candidate
from test_paper_reentry import eligible
import test_live_evidence as live_fixture
from sniper.adapters.evm import WETH


class EntryQuoteSizingTests(unittest.TestCase):
    def setUp(self):
        self.policy, _, _ = explore.profile_settings('two-wallet-30k-reentry')
        self.ledger = Ledger(':memory:', D(1000))
        self.engine = ExploratoryEngine(self.ledger, self.policy)
        self.calls = []

    def tearDown(self):
        self.ledger.close()

    def request(self, amount, *, deadline):
        self.calls.append(amount)
        # A 0.3% observed buy/sell cost, plus the adapter's separate 1% stress.
        output_spot = amount / D('1.003')
        q = quote(buy_cost_bps=D(130), sell_cost_bps=D(130),
                  route_max_notional_usd=output_spot,
                  route_max_sell_quantity=output_spot, route_impact_bps=D(10))
        return q, {'buy_input_usd': str(amount), 'block': 100, 'block_hash': 'fixture'}

    def test_initial_input_respects_budget_then_refines_to_actual_size(self):
        q, evidence = explore.sized_entry_quote(self.engine, self.request, clock=lambda: NOW)
        self.assertEqual(self.calls[0], D(5))
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertLessEqual(len(self.calls), 3)
        result = self.engine.consider(eligible(), q, NOW)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertLess(result['notional_usd'], 5)
        self.assertLessEqual(result['full_loss_risk_usd'], self.ledger.snapshot()['equity'] * D('.005'))
        input_for_fill = result['notional_usd'] * D('1.003')
        self.assertLessEqual(self.calls[-1] - input_for_fill, self.calls[-1] * D('.001'))
        self.assertEqual(evidence['entry_sizing']['maximum_seconds'], 25)
        self.assertEqual(evidence['entry_sizing']['policy_version'], self.policy.version)
        self.assertEqual(q.buy_cost_bps, D(130))
        self.assertEqual(q.sell_cost_bps, D(130))
        self.assertEqual(evidence['entry_sizing']['attempts'][-1]['planned_notional_usd'], str(result['notional_usd']))

    def test_twenty_five_dollar_impact_failure_does_not_reject_affordable_size(self):
        def request(amount, **kwargs):
            q, ev = self.request(amount, **kwargs)
            return replace(q, route_impact_bps=D(500) if amount > 5 else D(10)), ev
        large, _ = request(D(25), deadline=NOW + 25)
        self.assertIsNone(self.engine._size(large, self.ledger.snapshot()))
        self.calls.clear()
        q, _ = explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertNotIn(D(25), self.calls)
        result = self.engine.consider(eligible(), q, NOW)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertLessEqual(result['notional_usd'], q.route_max_notional_usd)
        self.assertLessEqual(result['quantity'], q.route_max_sell_quantity)
        self.assertLessEqual(result['full_loss_risk_usd'], self.ledger.snapshot()['equity'] * D('.005'))

    def test_initial_probe_impact_can_inform_smaller_quote_but_never_enter(self):
        def request(amount, **kwargs):
            q, ev = self.request(amount, **kwargs)
            return replace(q, route_impact_bps=D(101) if amount == 5 else D(99)), ev
        q, ev = explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertEqual(ev['entry_sizing']['attempts'][0]['quote']['route_impact_bps'], '101')
        self.assertEqual(q.route_impact_bps, D(99))
        self.assertEqual(self.engine.consider(eligible(), q, NOW)['verdict'], 'PAPER_BUY')

    def test_final_impact_failure_is_not_removed_from_admission(self):
        def request(amount, **kwargs):
            q, ev = self.request(amount, **kwargs)
            return replace(q, route_impact_bps=D(101)), ev
        q, _ = explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertEqual(q.route_impact_bps, D(101))
        result = self.engine.consider(eligible(), q, NOW)
        self.assertEqual(result['reason'], 'route_price_impact_exceeded')
        self.assertEqual(result['observed_route_impact_bps'], D(101))
        self.assertEqual(result['maximum_route_impact_bps'], D(100))
        self.assertEqual(self.ledger.snapshot()['open_positions'], 0)

    def test_strict_reference_keeps_its_existing_diagnostic_and_rejection(self):
        strict = Ledger(':memory:', D(1000))
        try:
            engine = PaperEngine(strict, RiskPolicy())
            q = quote(route_max_notional_usd=D(25), route_max_sell_quantity=D(25), route_impact_bps=D(101))
            result = engine.consider(candidate(), q, NOW)
            self.assertEqual(result['reason'], 'risk_or_cash_budget_exhausted')
            self.assertFalse(strict.positions())
        finally:
            strict.close()

    def test_no_money_budget_does_not_request_route_or_create_fill(self):
        with self.ledger.transaction():
            self.ledger._set('cash', '0')
        request = Mock()
        with self.assertRaises(explore.EntrySizingUnavailable) as caught:
            explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertEqual(caught.exception.reason, 'risk_or_cash_budget_exhausted')
        request.assert_not_called()
        self.assertEqual(self.ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)

    def test_existing_exposure_bounds_initial_request(self):
        for i in range(3):
            asset = f'eip155:4663:held{i}'
            self.engine.consider(eligible(str(i), asset), quote(asset), NOW)
        state = self.ledger.snapshot()
        remaining = min(state['starting_cash'], state['equity']) * D('.02') - state['stress_exposure']
        explore.sized_entry_quote(self.engine, self.request, clock=lambda: NOW)
        self.assertLessEqual(self.calls[0], remaining)
        self.assertLess(self.calls[0], 5)

    def test_final_engine_rechecks_changed_cash(self):
        q, _ = explore.sized_entry_quote(self.engine, self.request, clock=lambda: NOW)
        with self.ledger.transaction():
            self.ledger._set('cash', '0')
        self.assertEqual(self.engine.consider(eligible(), q, NOW)['reason'], 'risk_or_cash_budget_exhausted')
        self.assertFalse(self.ledger.positions())

    def test_nonconverging_costs_stop_after_three_quotes_without_fill(self):
        def request(amount, **kwargs):
            q, ev = self.request(amount, **kwargs)
            return replace(q, gas_usd=D(len(self.calls)) / 20), ev
        with self.assertRaises(explore.EntrySizingUnavailable) as caught:
            explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertEqual(caught.exception.reason, 'entry_quote_sizing_not_converged')
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.ledger.positions())

    def test_slow_quote_and_original_trial_deadline_block_entry(self):
        for end in (NOW + 25, NOW + 5):
            tick = [NOW]
            self.calls.clear()
            def request(amount, **kwargs):
                self.assertEqual(kwargs['deadline'], end)
                q, ev = self.request(amount, **kwargs)
                tick[0] = end
                return replace(q, observed_at=end), ev
            with self.subTest(end=end), self.assertRaises(explore.EntrySizingUnavailable) as caught:
                explore.sized_entry_quote(self.engine, request, clock=lambda: tick[0], deadline=end)
            self.assertEqual(caught.exception.reason, 'entry_quote_sizing_deadline')
            self.assertEqual(len(self.calls), 1)
            self.assertFalse(self.ledger.positions())

    def test_stale_or_sell_only_or_missing_input_evidence_never_returns_entry_quote(self):
        for kind in ('stale', 'sell_only', 'input_missing', 'input_nan', 'input_oversized'):
            def request(amount, **kwargs):
                q, ev = self.request(amount, **kwargs)
                if kind == 'stale': q = replace(q, observed_at=NOW - 31)
                if kind == 'sell_only': q = replace(q, route_side='sell')
                if kind == 'input_missing': ev = {}
                if kind == 'input_nan': ev['buy_input_usd'] = 'NaN'
                if kind == 'input_oversized': ev['buy_input_usd'] = str(amount + 1)
                return q, ev
            with self.subTest(kind=kind), self.assertRaises(explore.EntrySizingUnavailable):
                explore.sized_entry_quote(self.engine, request, clock=lambda: NOW)
        self.assertFalse(self.ledger.positions())

    def test_native_v3_evidence_converts_actual_base_input_and_rounding(self):
        fixture = live_fixture.RouteTests(); fixture.setUp()
        def request(amount, **kwargs):
            q = fixture.client.quote(WETH, live_fixture.POOL, max_notional=amount)
            return q, fixture.client.last_evidence
        q, ev = explore.sized_entry_quote(self.engine, request, clock=lambda: 1800000000)
        self.assertLessEqual(D(ev['buy_input_usd']), D(ev['requested_input_usd']))
        amounts = self.engine._size(q, self.ledger.snapshot())
        expected = amounts['notional'] * D(ev['buy_input_usd']) / q.route_max_notional_usd
        self.assertLessEqual(D(ev['buy_input_usd']) - expected, D(ev['buy_input_usd']) * D('.001'))


if __name__ == '__main__':
    unittest.main()
