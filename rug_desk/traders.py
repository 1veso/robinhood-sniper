"""Solana leader selection over supplied histories; SQLite is the authority."""
from __future__ import annotations
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from rug_desk import config
from rug_desk.book import atomic, encode, number
from sniper.adapters.solana import validate_address
from sniper.advisor import response_text, _unique_object
from sniper.wallet_analysis import analyse_wallet

POLICY = {'version': 'solana_leaders_v1', 'selection_seconds': 7*86400,
          'update_max_seconds': 180,
          'input_max_age_seconds': 86400, 'event_max_age_seconds': 300,
          'maximum_leaders': 3, 'follower_cost_bps': '100',
          'history_window_days': 30,
          'criteria': 'Existing analyse_wallet: 20 closed positions; win >=55%; top-token positive profit <=50%; hold >=30m; positive cost/open-inventory/follower stress; nonnegative recent week.'}
METRICS = ('closed_positions', 'win_rate', 'top_token_profit_share', 'average_hold_minutes',
           'net_pnl_after_follower_stress_usd', 'last_week_realized_less_open_basis_usd', 'followers')
RANK_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['verdict', 'confidence', 'leaders'], 'properties': {
        'verdict': {'type': 'string', 'enum': ['SELECT', 'SKIP']},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'leaders': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object',
            'additionalProperties': False, 'required': ['handle', 'rank', 'evidence_fields'],
            'properties': {'handle': {'type': 'string'}, 'rank': {'type': 'integer', 'minimum': 1, 'maximum': 3},
                'evidence_fields': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                    'items': {'type': 'string', 'enum': list(METRICS)}}}}}}}


def handle(value):
    if not isinstance(value, str) or not re.fullmatch(r'@?[A-Za-z0-9_]{1,64}', value):
        raise ValueError('Invalid trader handle')
    return value.lstrip('@').lower()


def timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError('Timestamp must be an integer')
    return value


def reference(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise ValueError('Required source reference missing')
    return value


def strict_json(text):
    return json.loads(text, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def screen_trader(record, now, *, replay=False):
    """No leaderboard P&L becomes verified history merely by being supplied."""
    if not isinstance(record, dict) or record.get('chain') != 'solana':
        raise ValueError('Solana trader record required')
    name, wallet = handle(record['handle']), validate_address(record['wallet'])
    source = record['source']
    if source['kind'] not in ('manual_export', 'replay') or (source['kind'] == 'replay' and not replay):
        raise ValueError('Replay records require an isolated replay account')
    reference(source['reference']); reference(record['wallet_source_ref'])
    history = record['history']
    if (history['wallet'] != wallet or history.get('complete') is not True
            or history.get('cost_basis') != 'net_token_flows_plus_network_fees'):
        raise ValueError('Complete cost-accounted history required')
    if not isinstance(history.get('gaps'), list) or not isinstance(history.get('opening_inventory'), list):
        raise ValueError('Explicit gap and opening-inventory coverage required')
    for token in history['opening_inventory']:
        validate_address(token)
    cutoff = timestamp(history['cutoff'])
    if not 0 <= now-cutoff <= POLICY['input_max_age_seconds']:
        raise ValueError('History snapshot is stale or future-dated')
    if timestamp(history['window_start']) != cutoff-30*86400:
        raise ValueError('Explicit 30-day history window required')
    records = history['records']
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        raise ValueError('Bounded transaction history required')
    for item in records:
        reference(item['id']); reference(item['source_ref'])
        if not history['window_start'] <= timestamp(item['timestamp']) <= cutoff:
            raise ValueError('History event outside declared window')
        if type(item['block_number']) is not int or item['block_number'] < 0 or type(item['transaction_index']) is not int or item['transaction_index'] < 0:
            raise ValueError('Invalid history ordering')
        number(item['gas_usd'])  # Missing costs are never treated as zero.
        if item['kind'] not in ('BUY', 'SELL', 'TRANSFER', 'OVERHEAD'):
            raise ValueError('Unsupported history record')
        if item['kind'] in ('BUY', 'SELL'):
            validate_address(item['token'])
            number(item['quantity'], positive=True); number(item['value_usd'], positive=True)
            if item.get('verified') is not True:
                raise ValueError('Source has not reconciled this transaction')
    analysis = analyse_wallet(history, follower_cost_bps=POLICY['follower_cost_bps'])
    if not analysis['performance_qualified']:
        raise ValueError('; '.join(analysis['qualification_blockers']))
    followers = record.get('followers')
    if followers is not None and (type(followers) is not int or followers < 0):
        raise ValueError('Invalid follower count')
    metrics = {k: analysis[k] for k in METRICS if k != 'followers'}
    metrics['followers'] = followers
    return {'handle': name, 'wallet': wallet, 'chain': 'solana', 'as_of': cutoff,
        'source': source, 'wallet_source_ref': record['wallet_source_ref'],
        'history_sha256': hashlib.sha256(encode(history).encode()).hexdigest(),
        'metrics': metrics, 'numerically_eligible': True,
        'evidence_scope': 'Supplied history reconciled arithmetically; source assertions and handle ownership are not independently verified. No controller relationships inferred.',
        'behavioral_pattern_evidence': 'Not supplied to the model; do not infer behavior from aggregate metrics.'}


def validate_rank(response, candidates):
    text = response_text(response)
    if len(text) > 16000:
        raise ValueError('Ranking output too large')
    out = strict_json(text)
    if not isinstance(out, dict) or set(out) != {'verdict', 'confidence', 'leaders'} or out['verdict'] not in ('SELECT', 'SKIP'):
        raise ValueError('Invalid ranking verdict/schema')
    if type(out['confidence']) not in (int, float) or number(out['confidence']) > 1:
        raise ValueError('Ranking confidence outside 0..1')
    leaders = out['leaders']
    if not isinstance(leaders, list) or len(leaders) > 3:
        raise ValueError('At most three leaders permitted')
    if (out['verdict'] == 'SKIP' and leaders) or (out['verdict'] == 'SELECT' and not leaders):
        raise ValueError('Verdict and selection disagree')
    eligible = {c['handle']: c for c in candidates}
    seen = set()
    for index, leader in enumerate(leaders, 1):
        if not isinstance(leader, dict) or set(leader) != {'handle', 'rank', 'evidence_fields'}:
            raise ValueError('Unsupported ranking evidence')
        name = leader['handle']
        if name not in eligible or name in seen or type(leader['rank']) is not int or leader['rank'] != index:
            raise ValueError('Unknown, repeated or invalidly ranked leader')
        seen.add(name)
        fields = leader['evidence_fields']
        if (not isinstance(fields, list) or not fields or any(not isinstance(f, str) for f in fields)
                or len(set(fields)) != len(fields)
                or any(f not in METRICS or eligible[name]['metrics'][f] is None for f in fields)):
            raise ValueError('Ranking cites missing or invented evidence')
    return {**out, 'validated': True}


class Selections:
    def __init__(self, book):
        if book.identity['mode'] != 'paper' or not config.PAPER:
            raise ValueError('Leader integration is paper-only')
        self.book, self.db = book, book.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS leader_selections(
                id TEXT PRIMARY KEY,started INTEGER NOT NULL,completed INTEGER,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leader_events(
                id TEXT PRIMARY KEY,digest TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS leader_decisions(
                id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,at INTEGER NOT NULL,verdict TEXT NOT NULL,
                stage TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL);
        """)
        self.db.commit()

    def replay_account(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='trader_replay_account'").fetchone()
        return bool(row and row[0] == '1')

    def begin(self, now):
        identity = uuid.uuid4().hex
        with self.book.transaction():
            self.db.execute('INSERT INTO leader_selections VALUES (?,?,NULL,?,?)',
                            (identity, now, 'UPDATING', encode({'leaders': [], 'policy': POLICY})))
            self.db.execute("INSERT INTO metadata VALUES ('leader_current',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (identity,))
        self.export(now)
        return identity

    def publish(self, identity, now, status, payload):
        with self.book.transaction():
            current = self.db.execute("SELECT value FROM metadata WHERE key='leader_current'").fetchone()[0]
            if current != identity:
                status = 'SUPERSEDED'
            self.db.execute('UPDATE leader_selections SET completed=?,status=?,payload=? WHERE id=?',
                            (now, status, encode(payload), identity))
        self.export(now)
        return self.status(now)

    def status(self, now):
        return selection_status(self.book, now)

    def export(self, now):
        atomic(self.book.runtime/'leaders.json', encode(self.status(now)))
        decisions = [dict(r) for r in self.db.execute('SELECT * FROM leader_decisions ORDER BY id DESC LIMIT 200')]
        atomic(self.book.runtime/'leader-decisions.json', encode({'decisions': decisions, 'fills_source': 'book.sqlite3 fills; decisions are not fills'}))

    def select(self, records, *, judge=None, clock=time.time):
        now = int(clock()); identity = self.begin(now)
        payload = {'leaders': [], 'policy': POLICY, 'rejected': [], 'eligible': []}
        try:
            records = records() if callable(records) else records
            if not isinstance(records, list) or len(records) > 100:
                raise ValueError('Expected at most 100 trader records')
            names, wallets = set(), set()
            for index, record in enumerate(records):
                try:
                    candidate = screen_trader(record, now, replay=self.replay_account())
                    if candidate['handle'] in names or candidate['wallet'] in wallets:
                        raise RuntimeError('Ambiguous repeated trader handle or wallet')
                    names.add(candidate['handle']); wallets.add(candidate['wallet'])
                    payload['eligible'].append(candidate)
                except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    payload['rejected'].append({'record': index, 'reason': str(exc)[:500]})
            if payload['eligible']:
                from rug_desk.grok import Judge
                ranking = (judge or Judge(self.book.root)).rank_traders(payload['eligible'])
                if (not isinstance(ranking, dict) or ranking.get('validated') is not True
                        or set(ranking)-{'verdict','confidence','leaders','validated','request_id','model'}):
                    raise ValueError('Unvalidated ranking response')
                # Revalidate even a custom model adapter; never merge its statistics.
                out = validate_rank({'status': 'completed', 'output_text': encode({k: ranking[k] for k in ('verdict','confidence','leaders')})}, payload['eligible'])
                payload['ranking'] = ranking
                eligible = {c['handle']: c for c in payload['eligible']}
                if out['verdict'] == 'SELECT' and number(out['confidence']) >= number(config.MIN_CONFIDENCE):
                    payload['leaders'] = [{**eligible[l['handle']], 'rank': l['rank'],
                        'ranking_evidence': {f: eligible[l['handle']]['metrics'][f] for f in l['evidence_fields']}} for l in out['leaders']]
                payload['reason'] = 'completed_model_selection' if payload['leaders'] else 'model_skipped_or_low_confidence'
            else:
                payload['reason'] = 'no_numerically_eligible_traders'
            finished = int(clock())
            payload['expires_at'] = min([finished+POLICY['selection_seconds']] +
                [c['as_of']+POLICY['selection_seconds'] for c in payload['eligible']])
            return self.publish(identity, finished, 'COMPLETE', payload)
        except Exception as exc:
            payload.update(leaders=[], reason='Selection failed: '+type(exc).__name__)
            return self.publish(identity, int(clock()), 'FAILED', payload)


def selection_status(book, now):
    exists = book.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leader_selections'").fetchone()
    if not exists:
        return {'status': 'NOT_CONFIGURED', 'leaders': [], 'entry_enabled': False}
    row = book.db.execute("SELECT * FROM leader_selections WHERE id=(SELECT value FROM metadata WHERE key='leader_current')").fetchone()
    if not row:
        return {'status': 'NOT_CONFIGURED', 'leaders': [], 'entry_enabled': False}
    data = json.loads(row['payload']); status = row['status']
    if data.get('policy') != POLICY:
        status = 'POLICY_MISMATCH'
    elif status == 'UPDATING' and (now < row['started'] or now-row['started'] > POLICY['update_max_seconds']):
        status = 'UPDATE_STALLED'
    if status == 'COMPLETE' and (now < row['completed'] or now >= data['expires_at']):
        status = 'STALE'
    leaders = data.get('leaders', []) if status == 'COMPLETE' else []
    return {**data, 'selection_id': row['id'], 'started_at': row['started'], 'completed_at': row['completed'],
            'status': status, 'leaders': leaders, 'entry_enabled': status == 'COMPLETE' and bool(leaders),
            'empty_selection': status == 'COMPLETE' and not leaders,
            'account_id': book.identity['account_id'], 'source_mode': 'replay' if book.db.execute("SELECT 1 FROM metadata WHERE key='trader_replay_account' AND value='1'").fetchone() else 'manual_export',
            'copyfomo_connection': 'UNCONNECTED: no documented external event/approval/order/fill API verified'}
