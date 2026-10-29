"""Isolated, evidence-bound paper comparisons; never active strategy admission.

The original account must be copied explicitly by the comparison runner. Merely
constructing this engine cannot turn a normal paper ledger into an experiment.
"""
from __future__ import annotations

import json
from decimal import Decimal

from .exploration import ExploratoryEngine
from .ledger import BPS, ZERO, decimal, impact_fraction, json_text, timestamp

D = Decimal
ONE = D('1')


def modeled_proceeds(quantity, quote, *, price=None):
    """The same actual-quantity sale-cost formula used by PaperEngine._sell."""
    price = quote.price_usd if price is None else price
    gross = quantity * price
    return max(ZERO, gross * (ONE - quote.sell_cost_bps / BPS
                             - impact_fraction(gross, quote.liquidity_usd)) - quote.gas_usd)


class VariantEngine(ExploratoryEngine):
    """Baseline, A cost-aware break-even, or B stopped-trade recovery.

    All extra state and events commit in the existing engine's fill transaction.
    Inherited positions keep the original exits. Their later new entries can be
    evaluated under the selected variant, with the same base safety/risk gates.
    """

    def __init__(self, ledger, policy, variant='baseline'):
        if variant not in ('baseline', 'A', 'B'):
            raise ValueError('Unknown paper comparison variant')
        try:
            binding = json.loads(ledger._get('paper_comparison') or 'null')
        except (TypeError, ValueError) as exc:
            raise ValueError('Explicit isolated paper comparison metadata is required') from exc
        if (not isinstance(binding, dict) or binding.get('variant') != variant
                or binding.get('excluded_from_strategy') is not True
                or any(not isinstance(binding.get(k), str) or not binding[k]
                       for k in ('comparison_id', 'source_account_id', 'source_id'))
                or binding['source_account_id'] == ledger._get('account_id')):
            raise ValueError('Explicit isolated paper comparison metadata is required')
        for key in ('started_at', 'entry_deadline'):
            timestamp(binding.get(key))
        if binding['entry_deadline'] <= binding['started_at']:
            raise ValueError('Comparison deadline must follow its start')
        encoded = json_text(binding)
        old = ledger._get('paper_comparison_binding')
        if old is not None and old != encoded:
            raise ValueError('Paper comparison binding changed')
        self.variant, self.binding = variant, binding
        super().__init__(ledger, policy)
        # CREATE TABLE is intentionally confined to an explicitly bound copy.
        ledger.conn.execute('''CREATE TABLE IF NOT EXISTS paper_variant_positions (
            position_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, pool_id TEXT NOT NULL,
            opened_at INTEGER NOT NULL, entry_policy_version TEXT NOT NULL,
            inherited_original_exits INTEGER NOT NULL, stop_threshold TEXT NOT NULL,
            stop_loss_budget TEXT, entry_quote TEXT, armed_at INTEGER, arm_quote TEXT,
            closed_at INTEGER, close_reason TEXT)''')
        with ledger.transaction():
            if old is None:
                for p in ledger._positions():
                    self._insert_position(p, None, inherited=True)
                ledger._set('paper_comparison_binding', encoded)
            for p in ledger._positions():
                if self._position_record(p['position_id']) is None:
                    raise ValueError('Comparison position policy metadata is missing')

    def _position_record(self, position_id):
        row = self.ledger.conn.execute('SELECT * FROM paper_variant_positions WHERE position_id=?',
                                       (position_id,)).fetchone()
        return dict(row) if row else None

    def _insert_position(self, p, quote, *, inherited=False):
        stop = p['entry_price'] * (ONE - self.policy.stop_loss_pct / D(100))
        loss = None if quote is None else max(ZERO, p['total_entry_cost']
                                             - modeled_proceeds(p['qty0'], quote, price=stop))
        self.ledger.conn.execute('''INSERT INTO paper_variant_positions
            (position_id,asset_id,pool_id,opened_at,entry_policy_version,inherited_original_exits,
             stop_threshold,stop_loss_budget,entry_quote) VALUES(?,?,?,?,?,?,?,?,?)''',
            (p['position_id'], p['asset_id'], p['pool_id'], p['opened_at'], self.policy.version,
             int(inherited), str(stop), None if loss is None else str(loss),
             None if quote is None else json_text(quote.as_dict())))
        return stop, loss

    def _previous_close(self, asset_id):
        row = self.ledger.conn.execute('''SELECT * FROM closed_positions WHERE asset_id=?
            ORDER BY closed_at DESC,position_id DESC LIMIT 1''', (asset_id,)).fetchone()
        return dict(row) if row else None

    def _previous_stop(self, closed):
        recorded = self._position_record(closed['position_id'])
        if recorded is not None:
            return decimal(recorded['stop_threshold']), 'comparison_position_policy'
        # Seed history can establish the original threshold from its audited
        # entry fill and the policy saved with that exact entry decision.
        rows = self.ledger.conn.execute('''SELECT f.fill_price,f.event_id,d.payload
            FROM fills f JOIN decisions d ON d.event_id=f.event_id
            WHERE f.position_id=? AND f.action='OPEN' AND f.asset_id=?''',
            (closed['position_id'], closed['asset_id'])).fetchall()
        if len(rows) != 1:
            return None, 'missing_original_entry_policy'
        try:
            payload = json.loads(rows[0]['payload'])
            result = payload['result']
            if (result['verdict'] != 'PAPER_BUY' or result['position_id'] != closed['position_id']
                    or result['asset_id'] != closed['asset_id']):
                raise ValueError('Entry audit mismatch')
            pct = decimal(payload['policy']['stop_loss_pct'])
            price = decimal(rows[0]['fill_price'])
            if not ZERO < pct <= D(100) or price <= ZERO:
                raise ValueError('Invalid original threshold')
            return price * (ONE - pct / D(100)), 'audited_original_entry_fill_and_policy'
        except (KeyError, TypeError, ValueError):
            return None, 'missing_original_entry_policy'

    def _quote_problem(self, quote, asset_id, now, decision_at=None):
        problem = super()._quote_problem(quote, asset_id, now, decision_at)
        if problem:
            return problem
        if quote.route_max_sell_quantity is None or quote.route_max_notional_usd is None:
            return 'comparison_quote_size_coverage_missing'
        return None

    def _additional_entry_check(self, candidate, quote, now):
        if now < self.binding['started_at']:
            return {'reason': 'comparison_before_start'}
        if now >= self.binding['entry_deadline']:
            return {'reason': 'comparison_entry_deadline'}
        if self.variant != 'B':
            return None
        previous = self._previous_close(candidate.asset_id)
        if previous is None or previous['reason'] != 'stop_loss':
            return None
        threshold, scope = self._previous_stop(previous)
        details = {'previous_position_id': previous['position_id'],
                   'previous_exit_reason': previous['reason'], 'previous_closed_at': previous['closed_at'],
                   'previous_stop_threshold': threshold, 'threshold_source': scope,
                   'recovery_quote_price': quote.price_usd}
        if threshold is None:
            return {'reason': 'stop_recovery_evidence_missing', **details}
        if quote.price_usd <= threshold:
            return {'reason': 'stop_recovery_required', **details}
        return None

    def _position_opened(self, p, quote, now):
        stop, loss = self._insert_position(p, quote)
        previous = self._previous_close(p['asset_id'])
        previous_stop, scope = self._previous_stop(previous) if previous else (None, None)
        self.ledger.record_event('comparison:entry:' + p['position_id'], 'comparison_position_entry', {
            'variant': self.variant, 'position_id': p['position_id'], 'asset_id': p['asset_id'],
            'pool_id': p['pool_id'], 'opened_at': now, 'policy_version': self.policy.version,
            'stop_threshold': stop, 'modeled_original_stop_loss': loss,
            'quantity': p['qty0'], 'total_entry_cost': p['total_entry_cost'],
            'quote': quote.as_dict(), 'inherited_original_exits': False,
            'previous_position_id': previous['position_id'] if previous else None,
            'previous_exit_reason': previous['reason'] if previous else None,
            'previous_closed_at': previous['closed_at'] if previous else None,
            'previous_stop_threshold': previous_stop, 'previous_threshold_source': scope,
            'stop_loss_model': 'Original entry quote costs, gas and liquidity held fixed at the recorded stop price; not a future executable quote',
        }, now)

    def _additional_exit(self, p, quote, now):
        if self.variant != 'A' or p['tp_done']:
            return None
        record = self._position_record(p['position_id'])
        if record is None:
            raise ValueError('Comparison position policy metadata is missing')
        if record['inherited_original_exits']:
            return None
        proceeds = modeled_proceeds(p['qty'], quote)
        profit = proceeds - p['remaining_basis']
        loss = decimal(record['stop_loss_budget'])
        if record['armed_at'] is None and profit >= loss:
            self.ledger.conn.execute('UPDATE paper_variant_positions SET armed_at=?,arm_quote=? WHERE position_id=?',
                                     (now, json_text(quote.as_dict()), p['position_id']))
            self.ledger.record_event('comparison:armed:' + p['position_id'], 'comparison_break_even_armed', {
                'variant': 'A', 'position_id': p['position_id'], 'asset_id': p['asset_id'],
                'pool_id': p['pool_id'], 'quantity': p['qty'], 'modeled_original_stop_loss': loss,
                'modeled_liquidation_proceeds': proceeds, 'net_liquidation_profit': profit,
                'remaining_basis': p['remaining_basis'], 'quote': quote.as_dict(),
            }, now)
            record['armed_at'] = now
        if record['armed_at'] is not None and profit <= ZERO:
            return p['qty'], 'break_even_protection'
        return None

    def _position_closed(self, p, quote, now, reason):
        record = self._position_record(p['position_id'])
        if record is None:
            raise ValueError('Comparison position policy metadata is missing')
        self.ledger.conn.execute('UPDATE paper_variant_positions SET closed_at=?,close_reason=? WHERE position_id=?',
                                 (now, reason, p['position_id']))
        self.ledger.record_event('comparison:close:' + p['position_id'], 'comparison_position_close', {
            'variant': self.variant, 'position_id': p['position_id'], 'asset_id': p['asset_id'],
            'pool_id': p['pool_id'], 'reason': reason, 'closed_at': now,
            'quantity': p['qty'], 'remaining_basis': p['remaining_basis'],
            'stop_threshold': record['stop_threshold'], 'modeled_original_stop_loss': record['stop_loss_budget'],
            'armed_at': record['armed_at'], 'inherited_original_exits': bool(record['inherited_original_exits']),
            'quote': quote.as_dict(),
        }, now)

    def decision_context(self, candidate):
        return {**super().decision_context(candidate), 'comparison_id': self.binding['comparison_id'],
                'comparison_variant': self.variant, 'excluded_from_strategy': True}
