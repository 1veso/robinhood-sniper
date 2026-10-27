"""Evidence review, never a switch that enables real-money execution."""
from decimal import Decimal as D
from sniper.execution_costs import MODEL


def evaluate(book, now, api_usage=()):
    fills, positions = book.fills(), book.positions(all_positions=True)
    cost_fills = [f for f in fills if (f.get('execution_costs') or {}).get('model') == MODEL]
    closed = [p for p in positions if p.get('closed_at') is not None and p.get('execution_model') == MODEL]
    closed.sort(key=lambda p: p['closed_at'])
    pnl = sum((D(f['pnl']) for f in fills), D(0))
    api_known = all(r.get('billed_usd') is not None for r in api_usage)
    api_cost = sum((D(r['billed_usd']) for r in api_usage if r.get('billed_usd') is not None), D(0))
    embedded = sum((D(f['execution_costs']['embedded_friction_usd']) for f in cost_fills), D(0))
    network = sum((D(f['execution_costs']['explicit_fee_usd']) for f in cost_fills), D(0))
    shock = sum((D(f['execution_costs']['stress_extra_cost_usd']) for f in cost_fills), D(0))
    opened = [p for p in positions if D(p['remaining']) > 0]
    marks_complete = all(not p.get('feed_error') and 'unrealized_pnl' in p
        and 0 <= now - p.get('last_observed_at', 0) <= 780 for p in opened)
    unrealized = sum((D(p['unrealized_pnl']) for p in opened if 'unrealized_pnl' in p), D(0))
    net = pnl + unrealized - api_cost if marks_complete and api_known else None
    reserve = sum((D(p.get('rent_capital_reserved_usd', '0')) for p in positions), D(0))
    trial_row = book.db.execute("SELECT value FROM metadata WHERE key='trial'").fetchone()
    import json
    trial = json.loads(trial_row[0]) if trial_row else None
    costs_start = book.db.execute("SELECT value FROM metadata WHERE key='execution_costs_started_at'").fetchone()
    start = max(trial['started_at'], int(costs_start[0])) if trial and costs_start else None
    results = [D(p['realized_pnl']) for p in closed]
    split = len(results) // 2
    recent_ok = split > 0 and sum(results[:split]) > 0 and sum(results[split:]) > 0
    without_best = sum(results) - max(results) - api_cost if results else D(0)
    gaps = book.db.execute("SELECT count(*) FROM runs WHERE status != 'ok' AND (finished IS NOT NULL OR started < ?)", (now - 1200,)).fetchone()[0]
    checks = {
        'cost_model_active': book.cost_policy() is not None,
        'seven_days_of_cost_aware_trial': start is not None and now - start >= 7 * 86400,
        'at_least_30_closed_positions': len(closed) >= 30,
        'all_fills_use_cost_model': len(cost_fills) == len(fills) and bool(fills),
        'complete_current_open_marks': marks_complete,
        'all_api_costs_reconciled': api_known,
        'positive_net_after_open_losses_and_api_costs': net is not None and net > 0,
        'positive_after_extra_execution_stress': net is not None and net - shock > 0,
        'both_chronological_halves_profitable': recent_ok,
        'positive_without_best_closed_trade': without_best > 0,
        'operational_errors_need_review': gaps == 0,
    }
    return {'real_money_enabled': False, 'automatic_promotion': False,
        'performance_ready_for_human_review': all(checks.values()), 'checks': checks,
        'closed_cost_aware_positions': len(closed), 'realized_after_execution_costs_usd': str(pnl),
        'api_costs_usd': str(api_cost), 'net_including_open_positions_usd': str(net) if net is not None else None,
        'net_after_extra_execution_stress_usd': str(net - shock) if net is not None else None,
        'embedded_fees_impact_slippage_usd': str(embedded), 'explicit_network_and_failure_fees_usd': str(network),
        'rent_capital_reserved_usd': str(reserve), 'additional_stress_cost_usd': str(shock),
        'policy_status': 'Draft review criteria, not a statistically established edge or live authorization',
        'execution_prerequisites': ['Fresh screened-pool buy and sell routes for exact quantities',
            'Independent transaction instruction review and successful simulation',
            'Approved wallet, capital allocation, position limits and loss/kill-switch policy',
            'Reconciled finalized fills, fees, rent and any failed attempts',
            'Explicit operator approval and wallet signature for the live handoff'],
        'stress_definition': 'Additional 100 bps adverse execution per fill plus two extra network-fee equivalents; fixed observed trade path, not a full strategy replay'}
