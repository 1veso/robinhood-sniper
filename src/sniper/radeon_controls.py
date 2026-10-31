"""Account-bound paper entry switches; independent exits never consult these.

Updates serialize with financial writes in the same SQLite ledger. Resuming a
switch only admits subsequently observed candidates; it never replays a backlog.
"""
import json
import sqlite3
import time

from .ledger import json_text

KEY = 'radeon_entry_controls_v1'
PATHS = ('whale_follow', 'two_wallet_v3')


def read_controls(ledger):
    account = ledger._get('account_id')
    raw = ledger._get(KEY)
    if raw is None:
        return {'account_id': account, 'revision': 0, 'enabled': True,
                'paths': {k: True for k in PATHS}, 'resume_after': 0,
                'path_resume_after': {k: 0 for k in PATHS}, 'updated_at': None}
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'account_id','revision','enabled','paths',
            'resume_after','path_resume_after','updated_at'} or value['account_id'] != account
            or type(value['revision']) is not int or value['revision'] < 1
            or type(value['enabled']) is not bool or set(value['paths']) != set(PATHS)
            or any(type(v) is not bool for v in value['paths'].values())
            or set(value['path_resume_after']) != set(PATHS)
            or any(type(v) is not int or v < 0 for v in
                   [value['resume_after'], value['updated_at'], *value['path_resume_after'].values()])):
        raise ValueError('Entry controls are inconsistent; new entries blocked')
    return value


def candidate_time(candidate):
    return candidate.metadata.get('decision_at', candidate.observed_at)


def account_blocker(ledger, candidate=None):
    value = read_controls(ledger)
    if not value['enabled']:
        return 'operator_entries_paused'
    if candidate is not None and (type(candidate_time(candidate)) is not int
            or candidate_time(candidate) <= value['resume_after']):
        return 'candidate_predates_entry_resume'
    return None


def path_enabled(ledger, strategy, candidate):
    value = read_controls(ledger)
    return (value['enabled'] and value['paths'].get(strategy, False)
            and type(candidate_time(candidate)) is int
            and candidate_time(candidate) > max(value['resume_after'], value['path_resume_after'][strategy]))


def update(root, request, now=None):
    from .radeon_portfolios import identity
    now = int(time.time()) if now is None else now
    if not isinstance(request, dict) or set(request) != {'key','account_id','revision','enabled','paths'}:
        raise ValueError('Invalid entry control request')
    if (type(request['revision']) is not int or request['revision'] < 0
            or type(request['enabled']) is not bool or not isinstance(request['paths'], dict)
            or set(request['paths']) != set(PATHS)
            or any(type(v) is not bool for v in request['paths'].values())):
        raise ValueError('Invalid entry switches')
    folder, expected, manifest = identity(root, request['key'])
    if request['account_id'] != expected:
        raise ValueError('Account changed; reload before changing entries')
    # Existing fixed allowlisted account only; no constructor or schema writes.
    c = sqlite3.connect((folder/'ledger.sqlite3').as_uri()+'?mode=rw', uri=True, timeout=2)
    class View:
        def _get(self, key):
            row = c.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
    view = View()
    try:
        c.execute('BEGIN IMMEDIATE')
        if view._get('account_id') != expected or view._get('execution_mode') != 'paper':
            raise ValueError('Paper account binding changed')
        if request['key'] == 'primary':
            if json.loads(view._get('trial_manifest')) != manifest:
                raise ValueError('Primary trial binding changed')
        else:
            bound = json.loads(view._get('paper_comparison'))
            if bound['comparison_id'] != manifest['comparison_id'] or bound['source_id'] != manifest['source_id']:
                raise ValueError('Comparison binding changed')
        previous = read_controls(view)
        if previous['revision'] != request['revision']:
            raise ValueError('Entry switches changed elsewhere; reload and try again')
        if request['key'] != 'primary' and request['paths'] != previous['paths']:
            raise ValueError('Comparison entry hypotheses are fixed; pause or resume the account')
        if request['enabled'] == previous['enabled'] and request['paths'] == previous['paths']:
            c.rollback()
            return previous
        if previous['updated_at'] is not None and now < previous['updated_at']:
            raise ValueError('Control clock moved backwards')
        value = {**previous, 'revision': previous['revision']+1, 'enabled': request['enabled'],
                 'paths': request['paths'], 'updated_at': now,
                 'resume_after': now if request['enabled'] and not previous['enabled'] else previous['resume_after'],
                 'path_resume_after': {k: now if request['paths'][k] and not previous['paths'][k]
                                       else previous['path_resume_after'][k] for k in PATHS}}
        c.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (KEY, json_text(value)))
        c.execute('INSERT INTO events(event_id,kind,observed_at,payload) VALUES(?,?,?,?)',
                  (f'entry-controls:{expected}:{value["revision"]}', 'operator_entry_controls', now,
                   json_text({'previous': previous, 'current': value, 'exits_continue': True,
                              'origin': 'local_operator_console'})))
        c.commit()
        return value
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
