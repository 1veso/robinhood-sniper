"""Read-only Radeon account catalogue and complete trade attribution.

No account is created by a read. Copied seed fills never become prospective
comparison trades. Matching uses the candidate event, asset and pinned pool.
"""
from collections import defaultdict
from decimal import Decimal as D
import json
from pathlib import Path
import time

from .ledger import decimal

CATALOG = {
    'primary': ('Primary Radeon', 'primary', 'runtime/exploration'),
    'exit_baseline': ('Original exits · baseline', 'exit', 'runtime/paper-comparison/baseline'),
    'exit_protection': ('Early protection · former A', 'exit', 'runtime/paper-comparison/A'),
    'exit_recovery': ('Recovery re-entry · former B', 'exit', 'runtime/paper-comparison/B'),
    'size_control': ('$5 risk control', 'sizing', 'runtime/radeon-sizing/control'),
    'size_25': ('$25 risk variant', 'sizing', 'runtime/radeon-sizing/usd25'),
}
ENTRY_NAMES = {'whale_follow': 'Whale-follow', 'two_wallet_v3': 'Two-wallet confirmation',
               'observed_wallet_exploration_v1': 'Historical exploratory entry'}


def identity(root, key):
    if key not in CATALOG:
        raise ValueError('Unknown Radeon account')
    label, group, relative = CATALOG[key]
    folder = Path(root) / relative
    if group == 'primary':
        manifest = json.loads((folder / 'trial.json').read_text(encoding='utf-8'))
        account = manifest['account_id']
    else:
        manifest = json.loads((folder.parent / 'manifest.json').read_text(encoding='utf-8'))
        account = manifest['accounts'][folder.name]
    return folder, account, manifest


def totals(trades):
    closed = [t for t in trades if t['state'] == 'CLOSED']
    realized = sum((t['realized_pnl'] for t in trades), D(0))
    unrealized = sum((t['unrealized_pnl'] for t in trades), D(0))
    return {'entries': len(trades), 'closed': len(closed), 'open': len(trades)-len(closed),
            'unresolved': sum(t['state'] == 'UNRESOLVED' for t in trades),
            'wins': sum(t['realized_pnl'] > 0 for t in closed),
            'losses': sum(t['realized_pnl'] < 0 for t in closed),
            'realized_pnl': realized, 'unrealized_pnl': unrealized, 'net_pnl': realized+unrealized,
            'closed_pnl': sum((t['realized_pnl'] for t in closed), D(0)),
            'costs': sum((t['costs'] for t in trades), D(0))}


def account(root, key, now=None):
    from .console import reader, LedgerView
    from .radeon_exits import binding
    from .radeon_controls import read_controls
    now = int(time.time()) if now is None else now
    folder, expected, manifest = identity(root, key)
    label, group, _ = CATALOG[key]
    with reader(folder / 'ledger.sqlite3') as c:
        view = LedgerView(c)
        if view._get('account_id') != expected or view._get('execution_mode') != 'paper':
            raise ValueError('Radeon account identity mismatch')
        meta = json.loads(view._get('paper_comparison') or '{}')
        if group != 'primary' and (meta.get('comparison_id') != manifest['comparison_id']
                or meta.get('source_account_id') != manifest['source_account_id']
                or meta.get('source_id') != manifest['source_id']):
            raise ValueError('Comparison source identity mismatch')
        policy = json.loads(view._get('policy'))
        seed = meta.get('seed_fill_id', 0)
        fs = [dict(r) for r in c.execute('SELECT * FROM fills ORDER BY id')]
        by_position = defaultdict(list)
        for f in fs:
            by_position[f['position_id']].append(f)
        snapshot = view.snapshot()
        positions = {p['position_id']: p for p in snapshot['positions']}
        closed = {r['position_id']: dict(r) for r in c.execute('SELECT * FROM closed_positions')}
        trades = []
        for pid, fills in by_position.items():
            entry = fills[0]
            if entry['action'] != 'OPEN':
                raise ValueError('Trade has no original entry fill')
            row = c.execute("""SELECT json_extract(payload,'$.result') result,
                json_extract(payload,'$.candidate.metadata.profile') profile,
                json_extract(payload,'$.quote.pool_id') pool_id,
                json_extract(payload,'$.policy') policy FROM decisions WHERE event_id=?""",
                (entry['event_id'],)).fetchone()
            if row is None:
                raise ValueError('Entry decision missing')
            result = json.loads(row['result'])
            spec = binding(c, pid)
            inherited = entry['id'] <= seed
            entry_rule = result.get('initiating_strategy') or entry['strategy']
            exit_name = spec['policy']['name'] if spec else (
                'early-protection-v1' if key == 'exit_protection' and not inherited else 'original-exits')
            p = positions.get(pid)
            if bool(p) == bool(pid in closed):
                raise ValueError('Trade must be either open or closed')
            fresh = p is not None and p['mark_status'] == 'ok' and 0 <= now-p['mark_at'] <= policy['max_quote_age_seconds']
            liquidation = p['liquidation_value'] if fresh else D(0)
            pnl = sum((decimal(f['pnl']) for f in fills), D(0))
            entry_cash = -decimal(entry['cash_delta'])
            remaining = p['remaining_basis'] if p else D(0)
            trade = {'account_key': key, 'account_id': expected, 'position_id': pid,
                'event_id': entry['event_id'], 'asset_id': entry['asset_id'], 'symbol': entry['symbol'],
                'pool_id': row['pool_id'], 'entry_rule': entry_rule,
                'entry_label': ENTRY_NAMES.get(entry_rule, entry_rule),
                'entry_revision': result.get('entry_revision') or result['policy_version'],
                'entry_profile': row['profile'], 'exit_policy': exit_name,
                'exit_revision': spec['revision'] if spec else 'original:' + result['policy_version'],
                'opened_at': entry['observed_at'], 'closed_at': closed[pid]['closed_at'] if pid in closed else None,
                'state': 'CLOSED' if not p else 'OPEN' if fresh else 'UNRESOLVED',
                'copied_seed': inherited, 'entry_cash': entry_cash, 'realized_pnl': pnl,
                'unrealized_pnl': liquidation-remaining, 'remaining_basis': remaining,
                'remaining_quantity': p['qty'] if p else D(0), 'mark_at': p['mark_at'] if p else None,
                'costs': sum((decimal(f['friction_usd'])+decimal(f['gas_usd']) for f in fills), D(0)),
                'return_on_entry_cash_pct': pnl/entry_cash*100 if not p else None,
                'last_reason': fills[-1]['reason'], 'fills': fills,
                'qualified_strategies': result.get('qualified_strategies', []),
                'entry_control_revision': result.get('entry_control_revision', 0)}
            trades.append(trade)
        # Reconcile attribution against the authoritative balance and every fill.
        if abs(sum((t['realized_pnl'] for t in trades), D(0))-snapshot['realized_pnl']) > D('1e-18'):
            raise ValueError('Trade attribution does not reconcile to account P&L')
        prospective = [t for t in trades if not t['copied_seed']]
        groups = defaultdict(list)
        for t in prospective:
            groups[(t['entry_rule'], t['entry_revision'], t['exit_policy'], t['exit_revision'])].append(t)
        cohorts = [{'entry_rule': k[0], 'entry_revision': k[1], 'exit_policy': k[2], 'exit_revision': k[3],
                    **totals(ts)} for k, ts in groups.items()]
        daily = [dict(r) for r in c.execute('SELECT * FROM daily_equity ORDER BY day DESC LIMIT 2')]
        controls = read_controls(view)
        current_revision = view._get('entry_revision')
        current_paths = {sid: totals([t for t in prospective if t['entry_rule'] == sid and t['entry_revision'] == current_revision])
                         for sid in ('whale_follow', 'two_wallet_v3')} if current_revision else {}
        return {'key': key, 'label': label, 'group': group, 'account_id': expected, 'as_of': now,
            'processed_at': int(view._get('last_processed_at') or 0), 'started_at': manifest.get('started_at'),
            'entry_deadline': manifest.get('ends_at', manifest.get('entry_deadline')),
            'source_id': manifest['source_id'], 'controls': controls, 'daily': daily,
            'manual_halt': view._get('entry_halt') or None, 'current_entry_revision': current_revision,
            'active_exit': json.loads(view._get('radeon_exit_policy') or 'null'),
            'policy': policy, 'seed_fill_id': seed, 'excluded_seed_trades': sum(t['copied_seed'] for t in trades),
            'cohort': totals(prospective), 'account_total': totals(trades), 'cohorts': cohorts,
            'current_entry_paths': current_paths, 'cash': snapshot['cash'],
            'conservative_equity': snapshot['cash']+sum((t['remaining_basis']+t['unrealized_pnl'] for t in trades), D(0)),
            'max_observed_drawdown_pct': snapshot['max_drawdown_pct'],
            'gap_count': c.execute("SELECT COUNT(*) FROM events WHERE kind IN ('comparison_gap','monitoring_gap')").fetchone()[0],
            'cursor': int(view._get('comparison_cursor') or 0), 'trades': sorted(trades, key=lambda t:t['opened_at'], reverse=True)}


def matched(accounts):
    sets = [{(t['event_id'], t['asset_id'], t['pool_id']) for t in a['trades']
             if not t['copied_seed'] and t['state'] == 'CLOSED'} for a in accounts]
    common = set.intersection(*sets) if sets else set()
    return {'matched_closed': len(common), 'accounts': [
        {'key': a['key'], 'label': a['label'], **totals([t for t in a['trades']
            if not t['copied_seed'] and t['state'] == 'CLOSED'
            and (t['event_id'],t['asset_id'],t['pool_id']) in common])} for a in accounts],
        'scope': 'Same source candidate and pinned pool, closed in every account. Each account retains its own entry time, size and costs. This subset is not the complete portfolio.'}


def report(root, now=None):
    started = int(time.time()) if now is None else now
    accounts, unavailable = [], []
    for key in CATALOG:
        try:
            accounts.append(account(root, key, now))
        except FileNotFoundError:
            unavailable.append(key)
    return {'as_of': int(time.time()) if now is None else now, 'read_started_at': started,
            'accounts': accounts, 'unavailable': unavailable,
            'matched': {group: matched([a for a in accounts if a['group'] == group])
                        for group in ('exit','sizing')},
            'scope': 'Separate paper accounts. Never add their P&L together. Primary entry rules share one balance; attribution is by the rule that actually opened each position. Comparisons exclude copied starting trades. Open or unavailable marks are conservative unrealized values, never sales.'}
