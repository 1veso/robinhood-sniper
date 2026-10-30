"""Read-only Helium account and team observations; no initialization or model calls."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from sniper.krypton.view import snapshot as baseline_snapshot
from .config import ROLES, TEAM_POLICY_HASH, digest
from .paper import unsettled_costs


def _gate(meta, key, name):
    value = meta.get(key)
    if not isinstance(value, dict):
        return {'enabled': False, 'blockers': [name+' gate has not been recorded']}
    return dict(value, enabled=value.get('enabled') is True,
                blockers=list(value.get('blockers') or []))


def snapshot(db, now):
    meta = {row[0]: json.loads(row[1]) for row in db.execute('SELECT key,value FROM meta')}
    if meta.get('bot_id') != 'helium':
        raise ValueError('Helium cannot display another bot account')
    if digest(meta.get('config')) != meta.get('config_hash'):
        raise ValueError('Stored trading policy binding is invalid')
    result = baseline_snapshot(db, now)
    stored = meta.get('team_policy')
    policy = stored if isinstance(stored, dict) else {}
    bound = (meta.get('team_policy_hash') == TEAM_POLICY_HASH
             and digest(stored) == TEAM_POLICY_HASH)
    analyst = _gate(meta, 'model_gate', 'Grok')
    reviewer = _gate(meta, 'reviewer_gate', 'Jev')
    team_gate = _gate(meta, 'team_gate', 'Combined model')
    blockers = list(team_gate['blockers'])
    for name, gate in (('Grok', analyst), ('Jev', reviewer)):
        if not gate['enabled']:
            blockers.extend(gate['blockers'] or [name+' is disabled'])
    if not bound:
        blockers.append('Stored team policy differs from this code; new entries are blocked')
    team_gate.update(enabled=team_gate['enabled'] and analyst['enabled']
                     and reviewer['enabled'] and bound,
                     blockers=list(dict.fromkeys(blockers)))
    result['status'].update(model_gate=analyst, reviewer_gate=reviewer, team_gate=team_gate)
    # A recorded gate is the last worker observation, not a fresh provider check.
    if result['status']['health'] != 'DEGRADED':
        result['status']['health'] = 'PAPER_READY' if team_gate['enabled'] else 'SETUP_BLOCKED'
    summary = result['summary']
    gross = Decimal(summary['equity'])
    actual = Decimal(summary['api_spending'])
    unsettled = unsettled_costs(db)
    risk_equity = gross-actual-unsettled
    estimated_pnl = risk_equity-Decimal(summary['starting_cash'])
    summary.update(equity_before_model_costs=str(gross), equity=str(risk_equity),
                   conservative_pnl=str(estimated_pnl), estimated_model_costs=str(unsettled),
                   paper_estimated_net_pnl=str(estimated_pnl),
                   model_cost_estimate_scope='Unbilled usage estimates or unresolved request reservations; not provider invoices')
    if summary['unknown_api_bills']:
        summary['net_pnl'] = None
    result.update(bot='helium', title='Helium', purpose='Autonomous Trading Team')
    result['team'] = {
        'policy': dict(policy, review_model=policy.get('model'),
                       min_probability=policy.get('jev_probability_min')),
        'policy_hash': meta.get('team_policy_hash'),
        'binding_current': bound,
        'roles': [dict(role) for role in ROLES],
        'tasks': [dict(row) for row in db.execute(
            'SELECT * FROM team_tasks ORDER BY updated_at DESC,task_id LIMIT 50')],
        'task_limit': 50,
    }
    result['notifications'] = {'enabled': False, 'reason': 'Console and durable Helium decision log; Telegram is not enabled'}
    result['limits'].extend([
        'The internal team serves only Helium; other bot accounts and tasks are excluded.',
        'Jev answer probabilities are not probabilities of profitable trades.',
        'Conservative equity includes known model bills and unsettled cost allowances; net P&L stays unknown while a bill is unresolved.',
    ])
    from .cockpit import project
    return project(db,result,now)


def view(root, now=None):
    path = Path(root)/'helium_bot/runtime/book.sqlite3'
    if not path.exists():
        return {'bot': 'helium', 'account_id': None, 'title': 'Helium',
                'purpose': 'Autonomous Trading Team', 'initialized': False,
                'status': {'health': 'NOT_INITIALIZED'}, 'assets': []}
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True,
                         timeout=2, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = snapshot(db, int(time.time()) if now is None else int(now))
        result['initialized'] = True
        return result
    finally:
        db.close()


def health(root):
    path = Path(root)/'helium_bot/runtime/book.sqlite3'
    unavailable = {'available': False, 'account_id': None, 'last_completed_at': None}
    if not path.exists():
        return unavailable
    try:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            meta = {key: json.loads(value) for key,value in db.execute(
                "SELECT key,value FROM meta WHERE key IN ('bot_id','account_id')")}
            if meta.get('bot_id') != 'helium' or not meta.get('account_id'):
                return unavailable
            return {'available': True, 'account_id': meta['account_id'],
                    'last_completed_at': db.execute('SELECT max(finished_at) FROM runs').fetchone()[0],
                    'event_stream_available': False,
                    'journal_available': bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cockpit_events'").fetchone())}
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return unavailable
