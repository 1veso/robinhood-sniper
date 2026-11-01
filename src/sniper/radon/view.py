"""Read-only Radon projection; separates conditional replay from forward paper results."""
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time

from .config import number, policy_hash
from .policy import sell_plan
from .book import folder_for


def snapshot(db, now):
    meta = {r[0]: json.loads(r[1]) for r in db.execute('SELECT key,value FROM meta')}
    if meta.get('bot_id') != 'radon' or policy_hash(meta['config']) != meta['policy_hash']:
        raise ValueError('Radon view refuses another account or invalid policy')
    cfg = meta['config']
    marks = {r['mint']: json.loads(r['payload']) for r in db.execute('SELECT * FROM marks')}
    episodes = {r['mint']: dict(r) for r in db.execute('SELECT * FROM episodes')}
    positions, unknown = [], []
    value, basis, realized = D(0), D(0), D(meta['realized_pnl'])
    for row in db.execute('SELECT * FROM positions ORDER BY mint,leader'):
        p = dict(row)
        qty = number(p['quantity'])
        if qty == 0:
            continue
        p.update(cap_remaining=str(max(D(0), number(cfg['coin_episode_cap_usd'])-number(episodes[p['mint']]['spent']))),
                 episode=episodes[p['mint']]['number'], mark=marks.get(p['mint']), liquidation_value=None, unrealized_pnl=None)
        basis += number(p['basis'])
        try:
            plan = sell_plan(cfg, p['mark'] or {}, p['mint'], now, qty)
            mark = qty*number(plan['fill_price'])-number(plan['explicit_fee_usd'])
            value += mark
            p.update(liquidation_value=str(mark), unrealized_pnl=str(mark-number(p['basis'])))
        except (ValueError, KeyError, TypeError):
            unknown.append({'leader': p['leader'], 'mint': p['mint']})
        positions.append(p)
    actual_model, unsettled = D(0), D(0)
    for row in db.execute('SELECT payload FROM api_requests'):
        audit = json.loads(row[0]); usage = audit.get('usage', {})
        if usage.get('billed_usd') is None:
            unsettled += number(audit.get('reservation_usd', cfg['model_reservation_usd']))
        else:
            actual_model += number(usage['billed_usd'])
    cash, rent, start = number(meta['cash'])-actual_model-unsettled, number(meta['rent_reserved']), number(cfg['starting_cash_usd'])
    known = cash + rent + value
    equity = None if unknown else known
    research = db.execute('SELECT * FROM research ORDER BY at DESC,rowid DESC LIMIT 1').fetchone()
    shortlist = db.execute('SELECT * FROM shortlists ORDER BY effective_at DESC,rowid DESC LIMIT 1').fetchone()
    source = meta.get('source', {})
    fresh = source.get('state') == 'OK' and 0 <= now-source.get('through', -1) <= cfg['source_max_age_seconds']
    runs = [dict(r, payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 20')]
    return {'bot': 'radon', 'title': 'Radon', 'purpose': 'Copy Trading Desk', 'slot': 8, 'initialized': True,
        'account_id': meta['account_id'], 'demo': meta['demo'], 'mode': 'paper', 'assets': [],
        'evidence_label': 'synthetic' if meta['demo'] else 'prospective_paper', 'config': cfg,
        'status': {'health': 'PAPER_RESEARCH' if fresh else 'SOURCE_UNAVAILABLE', 'source': dict(source, fresh=fresh),
            'model_gate': meta.get('model_gate', {'enabled': False}), 'error_count': sum(r['state']=='ERROR' for r in runs)},
        'summary': {'starting_cash': str(start), 'cash': str(cash), 'equity': str(equity) if equity is not None else None,
            'known_equity_subtotal': str(known), 'buying_turnover': meta['turnover'], 'peak_deployed': meta['peak_deployed'],
            'deployed_basis': str(basis), 'realized_pnl': str(realized), 'open_position_value': str(value) if not unknown else None,
            'unrealized_pnl': str(value-basis) if not unknown else None,
            'net_account_return': str((equity-start)/start) if equity is not None and unsettled == 0 else None,
            'rent_reserved': str(rent), 'network_costs': meta['network_costs'], 'embedded_friction': meta['embedded_friction'],
            'model_billed': str(actual_model), 'model_unsettled': str(unsettled), 'unpriced_positions': unknown},
        'positions': positions, 'shortlist': dict(shortlist, payload=json.loads(shortlist['payload'])) if shortlist else None,
        'research': json.loads(research['payload']) if research else None,
        'decisions': [dict(r, payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 100')],
        'fills': [dict(r, payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM fills ORDER BY at DESC,rowid DESC LIMIT 100')],
        'runs': runs, 'providers': {k[9:]: v for k, v in meta.items() if k.startswith('provider:')},
        'pending_exits': db.execute("SELECT COUNT(*) FROM events WHERE state='EXIT_PENDING'").fetchone()[0],
        'limits': ['Paper execution-cost scenarios are not executable quotes or verified sellability.',
            'Historical replay is conditional on archived numerical evidence; historical X/safety gates are not reconstructed.',
            'Pool age is not token launch time. Distinct wallets do not establish distinct controllers.',
            'Reserved token-account rent is retained capital, not an assumed refund.',
            'Slot seven is reserved. Radon never initializes or trades another bot account.']}


def view(root, now=None):
    path = folder_for(root)/'book.sqlite3'
    if not path.exists():
        return {'bot': 'radon', 'title': 'Radon', 'purpose': 'Copy Trading Desk', 'slot': 8, 'initialized': False,
                'account_id': None, 'assets': [], 'status': {'health': 'NOT_INITIALIZED'}}
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return snapshot(db, int(time.time()) if now is None else now)
    finally:
        db.close()


def health(root):
    try:
        out = view(root)
        return {'available': out['initialized'], 'account_id': out['account_id'], 'slot': 8,
            'last_completed_at': max((r['finished_at'] or 0 for r in out.get('runs', [])), default=None)}
    except (OSError, ValueError, sqlite3.Error, KeyError):
        return {'available': False, 'account_id': None, 'slot': 8, 'last_completed_at': None}
