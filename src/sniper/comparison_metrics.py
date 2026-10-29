"""Read-only metrics for explicitly isolated, prospective paper comparisons.

Nothing here produces a fill or treats a later quote as an actual exit. Monetary
truth comes from the variant ledger. Missing marks remain conservative unknown
valuations; later-outcome samples require coverage of the quantity actually sold.
"""
from collections import Counter, defaultdict
from decimal import Decimal
import json

from .ledger import BPS, ZERO, decimal, impact_fraction, timestamp
from .models import MarketQuote, json_safe

D = Decimal


def _position_totals(fills, positions):
    totals = {}
    for f in fills:
        row = totals.setdefault(f['position_id'], {
            'position_id': f['position_id'], 'asset_id': f['asset_id'],
            'symbol': f['symbol'], 'realized_pnl': ZERO, 'unrealized_pnl': ZERO,
            'costs': ZERO, 'open': False, 'unresolved': False})
        row['realized_pnl'] += decimal(f['pnl'])
        row['costs'] += decimal(f['friction_usd']) + decimal(f['gas_usd'])
    for p in positions:
        row = totals[p['position_id']]
        row.update(open=True, unresolved=p['mark_status'] != 'ok',
                   unrealized_pnl=p['liquidation_value'] - p['remaining_basis'])
    for row in totals.values():
        row['net_pnl'] = row['realized_pnl'] + row['unrealized_pnl']
    return totals


def _aggregate(rows, key):
    result = {}
    for row in rows:
        value = row[key]
        group = result.setdefault(value, {key: value, 'positions': 0,
            'open_positions': 0, 'closed_positions': 0, 'unresolved_positions': 0,
            'realized_pnl': ZERO, 'unrealized_pnl': ZERO, 'net_pnl': ZERO,
            'modeled_costs': ZERO})
        group['positions'] += 1
        group['open_positions'] += int(row['open'])
        group['closed_positions'] += int(not row['open'])
        group['unresolved_positions'] += int(row['unresolved'])
        for name in ('realized_pnl', 'unrealized_pnl', 'net_pnl'):
            group[name] += row[name]
        group['modeled_costs'] += row['costs']
    return sorted(result.values(), key=lambda row: str(row[key]))


def _later_outcomes(fills, observations, entry_pools, max_age, now):
    results = []
    for close in fills:
        if close['action'] != 'CLOSE' or close['reason'] not in ('break_even', 'break_even_protection'):
            continue
        quantity = decimal(close['quantity'])
        actual_proceeds = decimal(close['cash_delta'])
        basis = actual_proceeds - decimal(close['pnl'])
        pool = entry_pools.get(close['position_id'])
        samples, unknown, seen = [], Counter(), set()
        latest_attempt = None
        for event_at, payload in observations:
            raw = payload.get('quote') if isinstance(payload, dict) else None
            if not isinstance(raw, dict) or raw.get('asset_id') != close['asset_id']:
                continue
            if event_at <= close['observed_at'] or event_at > now:
                continue
            latest_attempt = event_at
            try:
                q = MarketQuote.from_dict(raw)
                q.validate()
                key = json.dumps(q.as_dict(), sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                reason = ('pool_unknown' if pool is None else
                    'pool_mismatch' if q.pool_id != pool else
                    'quote_not_executable' if not q.executable else
                    'preclose_quote' if q.observed_at <= close['observed_at'] else
                    'future_quote' if q.observed_at > event_at else
                    'stale_quote' if event_at - q.observed_at > max_age else
                    'route_size_not_covered' if q.route_max_sell_quantity is None or q.route_max_sell_quantity < quantity else None)
                if reason:
                    unknown[reason] += 1
                    continue
                gross = quantity * q.price_usd
                friction = gross * (q.sell_cost_bps / BPS + impact_fraction(gross, q.liquidity_usd))
                proceeds = gross - friction - q.gas_usd
                if proceeds <= ZERO:
                    unknown['unexecutable_costs'] += 1
                    continue
                samples.append({'source_observed_at': q.observed_at, 'recorded_at': event_at,
                    'price': q.price_usd, 'quantity': quantity, 'net_proceeds': proceeds,
                    'holding_net_pnl': proceeds - basis, 'actual_close_pnl': decimal(close['pnl']),
                    'difference_from_recorded_close': proceeds - actual_proceeds,
                    'quote_source': q.source})
            except (ValueError, TypeError, KeyError, AttributeError):
                unknown['invalid_quote'] += 1
        latest = max(samples, key=lambda s: (s['source_observed_at'], s['recorded_at'])) if samples else None
        results.append({'position_id': close['position_id'], 'asset_id': close['asset_id'],
            'pool_id': pool, 'closed_at': close['observed_at'], 'quantity_sold': quantity,
            'status': 'SAMPLED' if samples else 'UNKNOWN', 'valid_unique_samples': len(samples),
            'unknown_observations': dict(unknown), 'latest_attempt_at': latest_attempt,
            'latest_valid': latest,
            'latest_valid_age_seconds': now - latest['source_observed_at'] if latest else None,
            'current_followup_valuation_available': bool(latest and 0 <= now - latest['source_observed_at'] <= max_age),
            'maximum_observed_holding_net_pnl': max((s['holding_net_pnl'] for s in samples), default=None),
            'minimum_observed_holding_net_pnl': min((s['holding_net_pnl'] for s in samples), default=None),
            'scope': 'Hypothetical retention of the quantity sold, valued only at recorded covered quotes. Not a fill, continuous price path, eventual winner, or alternative exit simulation.'})
    return results


def report(ledger, now):
    """Return JSON-safe metrics from one consistent ledger snapshot; no writes."""
    timestamp(now)
    with ledger.transaction():
        raw = ledger._get('paper_comparison')
        if raw is None:
            raise ValueError('Comparison report requires an isolated comparison account')
        meta = json.loads(raw)
        if (meta.get('excluded_from_strategy') is not True or
                meta.get('source_account_id') == ledger._get('account_id')):
            raise ValueError('Comparison account must be separate and excluded from strategy results')
        started = meta['started_at']
        if now < started or now < int(ledger._get('last_processed_at') or 0):
            raise ValueError('Comparison report cannot look ahead from an earlier timestamp')
        seed_fill, seed_equity_row = meta['seed_fill_id'], meta['seed_equity_id']
        seed_equity = decimal(meta['seed_equity'])
        seed_realized = decimal(meta['seed_realized_pnl'])
        seed_fees = decimal(meta['seed_fees_paid'])
        seed_closed = set(meta['seed_closed_ids'])
        policy = json.loads(ledger._get('policy'))
        max_age = policy['max_quote_age_seconds']
        snapshot = ledger._snapshot()
        account_id = ledger._get('account_id')
        positions = snapshot['positions']
        # Read-only report does not let an old 'ok' mark imply a fresh valuation.
        for p in positions:
            if p['mark_status'] == 'ok' and not 0 <= now - p['mark_at'] <= max_age:
                p['mark_status'] = 'stale_quote'
                p['liquidation_value'] = ZERO
        marked = sum((p['liquidation_value'] for p in positions), ZERO)
        equity = snapshot['cash'] + marked
        fills = [dict(r) for r in ledger.conn.execute('SELECT * FROM fills ORDER BY id')]
        new_fills = [f for f in fills if f['id'] > seed_fill]
        closed = [dict(r) for r in ledger.conn.execute('SELECT * FROM closed_positions ORDER BY closed_at,position_id')]
        equity_rows = [dict(r) for r in ledger.conn.execute(
            'SELECT * FROM equity_history WHERE id>? AND observed_at<=? ORDER BY id', (seed_equity_row, now))]
        events = []
        malformed = 0
        for r in ledger.conn.execute('SELECT rowid,* FROM events WHERE rowid>? AND observed_at>=? AND observed_at<=? ORDER BY observed_at,rowid',
                                     (meta.get('seed_event_rowid', 0), started, now)):
            try:
                events.append((r['kind'], r['observed_at'], json.loads(r['payload'])))
            except (ValueError, TypeError):
                malformed += 1
        entry_pools = {}
        for r in ledger.conn.execute("SELECT event_id,payload FROM decisions WHERE verdict='PAPER_BUY'"):
            try:
                payload = json.loads(r['payload'])
                entry_pools['position:' + r['event_id']] = payload['quote']['pool_id']
            except (ValueError, TypeError, KeyError):
                malformed += 1

    totals = _position_totals(fills, positions)
    prior_close, new_position_ids, reentries = {}, set(), []
    for f in fills:
        if f['action'] == 'OPEN' and f['id'] > seed_fill:
            new_position_ids.add(f['position_id'])
            previous = prior_close.get(f['asset_id'])
            if previous:
                reentries.append({**totals[f['position_id']],
                    'previous_exit_reason': previous['reason'],
                    'previous_position_id': previous['position_id'],
                    'previous_closed_at': previous['observed_at']})
        elif f['action'] == 'CLOSE':
            prior_close[f['asset_id']] = f
    new_closed = [p for p in closed if p['position_id'] not in seed_closed]
    new_entry_closed = [p for p in new_closed if p['position_id'] in new_position_ids]
    new_rows = [totals[key] for key in new_position_ids]
    inherited = set(meta.get('inherited_position_ids', []))
    realized_delta = snapshot['realized_pnl'] - seed_realized
    net_delta = equity - seed_equity
    peak, max_drawdown_pct, max_drawdown_usd = seed_equity, ZERO, ZERO
    for value in [decimal(r['equity']) for r in equity_rows] + [equity]:
        peak = max(peak, value)
        loss = peak - value
        max_drawdown_usd = max(max_drawdown_usd, loss)
        max_drawdown_pct = max(max_drawdown_pct, loss / peak * D(100) if peak > ZERO else ZERO)
    token_incremental = None
    if 'seed_positions' in meta:
        token_incremental = defaultdict(lambda: {'cash_flow': ZERO, 'current_liquidation': ZERO, 'seed_liquidation': ZERO})
        for p in meta['seed_positions']:
            token_incremental[p['asset_id']]['seed_liquidation'] += decimal(p['liquidation_value'])
        for p in positions:
            token_incremental[p['asset_id']]['current_liquidation'] += p['liquidation_value']
        for f in new_fills:
            token_incremental[f['asset_id']]['cash_flow'] += decimal(f['cash_delta'])
        token_incremental = [{'asset_id': key, **values, 'net_pnl': values['cash_flow'] + values['current_liquidation'] - values['seed_liquidation']}
                             for key, values in sorted(token_incremental.items())]
    unresolved = [{'position_id': p['position_id'], 'asset_id': p['asset_id'],
        'quantity': p['qty'], 'remaining_basis': p['remaining_basis'], 'mark_status': p['mark_status'],
        'last_quote_at': p['mark_at'], 'quote_age_seconds': now - p['mark_at']}
        for p in positions if p['mark_status'] != 'ok']
    gaps = [{'kind': kind, 'observed_at': at, 'details': payload} for kind, at, payload in events
            if kind in ('comparison_gap', 'monitoring_gap', 'unresolved_position', 'route_observation_failure')]
    gap_counts = Counter(g['kind'] for g in gaps)
    observations = [(at, payload) for kind, at, payload in events if kind == 'comparison_quote_observation']
    result = {
        'mode': 'PAPER_COMPARISON', 'excluded_from_strategy': True,
        'comparison_id': meta['comparison_id'], 'variant': meta['variant'],
        'account_id': account_id, 'source_account_id': meta['source_account_id'],
        'source_id': meta['source_id'], 'started_at': started, 'entry_deadline': meta['entry_deadline'],
        'as_of': now, 'seed_equity': seed_equity, 'conservative_equity': equity,
        'net_pnl': net_delta, 'realized_pnl': realized_delta,
        'unrealized_pnl_change': net_delta - realized_delta,
        'modeled_costs': snapshot['fees_paid'] - seed_fees,
        'max_observed_drawdown_pct': max_drawdown_pct, 'max_observed_drawdown_usd': max_drawdown_usd,
        'new_entries': len(new_position_ids), 'new_fills': len(new_fills),
        'new_closed_positions': len(new_entry_closed),
        'inherited_positions_closed': sum(p['position_id'] in inherited for p in new_closed),
        'closed_win_rate': D(sum(decimal(p['realized_pnl']) > ZERO for p in new_entry_closed)) / D(len(new_entry_closed)) if new_entry_closed else None,
        'closed_win_rate_denominator': len(new_entry_closed),
        'new_position_pnl_by_token': _aggregate(new_rows, 'asset_id'),
        'reentry_pnl_by_previous_exit_reason': _aggregate(reentries, 'previous_exit_reason'),
        'reentries': reentries, 'incremental_pnl_by_token': token_incremental,
        'account_to_date_pnl_by_token': _aggregate(totals.values(), 'asset_id'),
        'inherited_positions': [totals[p] for p in sorted(inherited) if p in totals],
        'unresolved_positions': unresolved, 'valuation_complete': not unresolved,
        'gap_counts': dict(gap_counts), 'gaps': gaps, 'recorded_quote_observations': len(observations),
        'malformed_audit_records': malformed,
        'after_protection_outcomes': _later_outcomes(new_fills, observations, entry_pools, max_age, now),
        'cost_treatment': 'Ledger fill friction and modeled gas are included once. V3 quote costs include pool fees plus 100 bps adverse stress; the engine also charges its depth stress. These are modeled costs, not paid execution bills.',
        'modeled_costs_scope': 'Costs charged by committed paper fills since the seed. Open-position net liquidation separately deducts the modeled future sale friction and gas; these are not charged twice to cash.',
        'cost_assumptions': {'quoted_pool_fees_and_route_impact': 'Included in quoted buy/sell basis points',
            'adverse_execution_stress_bps': '100', 'additional_depth_stress': '2 * notional / liquidity',
            'modeled_gas': '2 * (quoter gas + 100000) at observed gas price and ETH/USD',
            'actual_api_bills': 'Not represented by fill friction; no actual provider-spending claim'},
        'scope': 'Prospective isolated comparison from the recorded seed, excluding copied fills from new-entry statistics. Account-to-date token totals retain inherited historical losses. Unknown/stale liquidation marks are zero only for conservative unrealized risk; no sale is inferred. Drawdown is observed, not continuous. Small samples and source/quote gaps limit comparison.'}
    return json_safe(result)
