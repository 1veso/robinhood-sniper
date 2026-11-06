"""Derived DuckDB research data. SQLite remains the only trading authority.

One consistent read transaction per source; collections are NOT simultaneous
cross-account snapshots. No providers, signers, models, or account writers.
"""
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

SCHEMA = 'sniper_analytics_v1'
SOURCES = {
    'radeon': 'runtime/exploration/ledger.sqlite3',
    'xenon': 'rug_desk/runtime/book.sqlite3',
    'neon': 'neon_bot/runtime/book.sqlite3',
    'krypton': 'krypton_bot/runtime/book.sqlite3',
    'helium': 'helium_bot/runtime/book.sqlite3',
    'nitrogen': 'nitrogen_bot/runtime/book.sqlite3',
    'radon': 'radon_bot/runtime/book.sqlite3',
}
for _bot in ('xenon', 'argon', 'neon', 'krypton', 'helium', 'nitrogen', 'radon', 'oxygen'):
    SOURCES['experiment-' + _bot] = 'runtime/experiments/' + _bot + '/book.sqlite3'

# Never recursively copy runtime, secrets, provider caches, or API requests.
TABLES = frozenset(('metadata', 'meta', 'fills', 'positions', 'closed_positions',
                   'decisions', 'checks', 'equity_history', 'neon_equity',
                   'account_observations', 'observation_events', 'market_observations',
                   'marks', 'daily_equity', 'days'))
META_KEYS = frozenset(('account_id', 'identity', 'execution_mode', 'mode', 'bot_id',
                       'demo', 'fixture', 'created_at', 'started_at', 'ends_at',
                       'policy_hash', 'config_hash', 'config_sha256', 'starting_cash',
                       'real_orders_enabled', 'real_money_enabled'))
EQUITY_TABLES = ('equity_history', 'neon_equity', 'account_observations')
MAX_ROWS = 100_000
MAX_BYTES = 32_000_000
READ_SECONDS = 3


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def paths(root):
    root = Path(root).resolve()
    folder = root / 'runtime/analytics'
    # Fixed local destination cannot be redirected into a source account.
    for path in (folder, folder / 'desk.duckdb', folder / 'status.json', folder / 'settings.json'):
        if path.resolve() != path or path.is_symlink():
            raise ValueError('Analytics path may not redirect')
    return folder


def enabled(root):
    try:
        value = json.loads((paths(root) / 'settings.json').read_text(encoding='utf-8'))
        return value == {'schema': SCHEMA, 'enabled': True}
    except (OSError, ValueError):
        return False


def duckdb_module():
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError('Install requirements-analytics.txt in this Python environment') from exc
    return duckdb


def configure(root, active):
    if active:
        duckdb_module()  # Do not enable a job which cannot run.
    from rug_desk.book import atomic
    atomic(paths(root) / 'settings.json', encode({'schema': SCHEMA, 'enabled': bool(active)}))


def decode(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value  # Radeon metadata stores strings, the other books store JSON.


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def capture(root, source, relative, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES):
    """Read committed WAL data without initializing or repairing a source."""
    root = Path(root).resolve()
    path = root / relative
    if path.resolve() != path or path.is_symlink():
        raise ValueError('Source path may not redirect')
    if not path.is_file():
        raise FileNotFoundError(relative)
    deadline = time.monotonic() + READ_SECONDS
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN')
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta_table = 'metadata' if 'metadata' in present else 'meta'
        if meta_table not in present:
            raise ValueError('Paper metadata missing')
        metadata = {r['key']: decode(r['value']) for r in db.execute(
            'SELECT key,value FROM ' + quoted(meta_table) + ' WHERE key IN (' +
            ','.join('?' for _ in META_KEYS) + ')', sorted(META_KEYS))}
        identity = metadata.get('identity') or {}
        if not isinstance(identity, dict):
            raise ValueError('Malformed identity')
        account_id = identity.get('account_id') or metadata.get('account_id')
        mode = identity.get('mode') or metadata.get('execution_mode') or metadata.get('mode')
        if not isinstance(account_id, str) or not account_id or mode not in ('paper', 'experimental_paper'):
            raise ValueError('Bound paper account identity required')
        if any(k in metadata and metadata[k] is not False for k in ('real_orders_enabled', 'real_money_enabled')):
            raise ValueError('Real execution flag present')
        if identity.get('account_id') and metadata.get('account_id') not in (None, account_id):
            raise ValueError('Conflicting account identities')
        flags = [metadata[k] for k in ('demo', 'fixture') if k in metadata]
        if any(type(flag) is not bool for flag in flags):
            raise ValueError('Invalid fixture provenance')
        fixture = any(flags) if flags else None
        tables, records, used = {}, [], 0
        for name in sorted(present & TABLES):
            info = [dict(r) for r in db.execute('PRAGMA table_info(' + quoted(name) + ')')]
            primary = [r['name'] for r in sorted(info, key=lambda r: r['pk']) if r['pk']]
            order = ','.join(quoted(k) for k in primary) if primary else 'rowid'
            sql = 'SELECT * FROM ' + quoted(name)
            args = ()
            if name in ('metadata', 'meta'):
                sql += ' WHERE key IN (' + ','.join('?' for _ in META_KEYS) + ')'
                args = sorted(META_KEYS)
            count = 0
            for row in db.execute(sql + ' ORDER BY ' + order, args):
                if time.monotonic() > deadline:
                    raise ValueError('Source read deadline exceeded')
                payload = encode(dict(row))  # Money stays as the original decimal strings.
                used += len(payload.encode('utf-8'))
                if len(records) >= max_rows or used > max_bytes:
                    raise ValueError('Source exceeds analytical snapshot limit')
                count += 1
                records.append((name, count, payload))
            tables[name] = {'rows': count, 'columns': info}
        body = {'schema': SCHEMA, 'source': source, 'path': relative,
                'account_id': account_id, 'mode': mode, 'fixture': fixture, 'tables': tables}
        digest = hashlib.sha256(encode(body).encode())
        for name, index, payload in records:
            digest.update(encode([name, index, payload]).encode())
        return dict(body, snapshot_id=digest.hexdigest(), records=records)


DDL = '''
CREATE TABLE IF NOT EXISTS analytics_meta(schema VARCHAR PRIMARY KEY);
CREATE TABLE IF NOT EXISTS collections(
    sequence BIGINT PRIMARY KEY, collection_id VARCHAR UNIQUE, started_at BIGINT, finished_at BIGINT);
CREATE TABLE IF NOT EXISTS snapshots(
    snapshot_id VARCHAR PRIMARY KEY, source VARCHAR, source_path VARCHAR, account_id VARCHAR,
    mode VARCHAR, fixture BOOLEAN, captured_at BIGINT, tables_json JSON);
CREATE TABLE IF NOT EXISTS records(
    snapshot_id VARCHAR, table_name VARCHAR, row_number BIGINT, data JSON,
    PRIMARY KEY(snapshot_id,table_name,row_number));
CREATE TABLE IF NOT EXISTS collection_sources(
    collection_id VARCHAR, source VARCHAR, status VARCHAR, snapshot_id VARCHAR, error VARCHAR,
    read_started_at BIGINT, read_finished_at BIGINT,
    PRIMARY KEY(collection_id,source));
CREATE OR REPLACE VIEW latest_sources AS
    SELECT c.sequence,c.started_at,c.finished_at,s.* FROM collections c
    JOIN collection_sources s USING(collection_id)
    WHERE c.sequence=(SELECT max(sequence) FROM collections);
CREATE OR REPLACE VIEW latest_records AS
    SELECT l.source,s.account_id,s.mode,s.fixture,s.captured_at,r.table_name,r.row_number,r.data
    FROM latest_sources l JOIN snapshots s USING(snapshot_id) JOIN records r USING(snapshot_id)
    WHERE l.status='OK';
'''


def open_archive(root, *, readonly=False):
    folder = paths(root)
    if readonly and not (folder / 'desk.duckdb').is_file():
        raise FileNotFoundError('No analytical collection yet')
    if not readonly:
        folder.mkdir(parents=True, exist_ok=True)
    db = duckdb_module().connect(str(folder / 'desk.duckdb'), read_only=readonly,
                                config={'enable_external_access': False, 'threads': 1,
                                        'memory_limit': '256MB'})
    try:
        existing = db.execute("SELECT table_name FROM information_schema.tables WHERE table_name='analytics_meta'").fetchone()
        if existing and db.execute('SELECT schema FROM analytics_meta').fetchall() != [(SCHEMA,)]:
            raise ValueError('Unsupported analytics schema; preserve and review archive')
        if not readonly:
            db.execute(DDL)
            db.execute('INSERT INTO analytics_meta SELECT ? WHERE NOT EXISTS (SELECT 1 FROM analytics_meta)', [SCHEMA])
        if db.execute('SELECT schema FROM analytics_meta').fetchall() != [(SCHEMA,)]:
            raise ValueError('Unsupported analytics schema; preserve and review archive')
        return db
    except BaseException:
        db.close()
        raise


def collect(root):
    """Publish only committed collections; a failed source cannot masquerade as fresh."""
    from rug_desk.book import atomic, process_lock
    root = Path(root).resolve()
    folder = paths(root)
    with process_lock(folder / 'collect.lock'), closing(open_archive(root)) as db:
        started, collection_id = int(time.time()), str(uuid.uuid4())
        db.execute('BEGIN TRANSACTION')
        try:
            sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM collections').fetchone()[0]
            db.execute('INSERT INTO collections VALUES(?,?,?,NULL)', [sequence, collection_id, started])
            accounts = set()
            for source, relative in SOURCES.items():
                read_started = int(time.time())
                try:
                    value = capture(root, source, relative)
                    read_finished = int(time.time())
                    if value['account_id'] in accounts:
                        raise ValueError('Duplicate account identity in collection')
                    accounts.add(value['account_id'])
                except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                    status = 'MISSING' if isinstance(exc, FileNotFoundError) else 'ERROR'
                    db.execute('INSERT INTO collection_sources VALUES(?,?,?,NULL,?,?,?)',
                               [collection_id, source, status, type(exc).__name__, read_started, int(time.time())])
                    continue
                snapshot_id = value['snapshot_id']
                if not db.execute('SELECT 1 FROM snapshots WHERE snapshot_id=?', [snapshot_id]).fetchone():
                    db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)',
                               [snapshot_id, source, relative, value['account_id'], value['mode'],
                                value['fixture'], int(time.time()), encode(value['tables'])])
                    if value['records']:
                        db.executemany('INSERT INTO records VALUES(?,?,?,?)',
                                       [(snapshot_id, *record) for record in value['records']])
                db.execute("INSERT INTO collection_sources VALUES(?,?,'OK',?,NULL,?,?)",
                           [collection_id, source, snapshot_id, read_started, read_finished])
            db.execute('UPDATE collections SET finished_at=? WHERE collection_id=?',
                       [int(time.time()), collection_id])
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        result = report_connection(db)
        atomic(folder / 'status.json', encode(result))
        return result


def report_connection(db):
    collection = db.execute('SELECT collection_id,started_at,finished_at FROM collections ORDER BY sequence DESC LIMIT 1').fetchone()
    sources = []
    for row in db.execute('''SELECT l.source,l.status,l.error,s.account_id,s.mode,s.fixture,
                            s.captured_at,s.tables_json,l.read_started_at,l.read_finished_at FROM latest_sources l
                            LEFT JOIN snapshots s USING(snapshot_id) ORDER BY l.source''').fetchall():
        name, status, error, account, mode, fixture, captured, tables, read_started, read_finished = row
        tables = json.loads(tables) if tables else {}
        sources.append({'source': name, 'status': status, 'error': error, 'account_id': account,
                        'mode': mode, 'fixture': fixture, 'snapshot_first_captured_at': captured,
                        'read_started_at': read_started, 'read_finished_at': read_finished,
                        'rows': {key: value['rows'] for key, value in tables.items()},
                        'equity_history_rows': sum(tables.get(k, {}).get('rows', 0) for k in EQUITY_TABLES)})
    return {'schema': SCHEMA, 'collection_id': collection[0] if collection else None,
            'started_at': collection[1] if collection else None, 'finished_at': collection[2] if collection else None,
            'sources': sources, 'aliases': {'argon': 'xenon'},
            'unsupported': {'oxygen-original': 'JSON/CSV article desk; no SQLite adapter in v1',
                            'comparison-accounts': 'Existing comparison reports remain separate in v1'},
            'execution_authority': False,
            'scope': 'Derived snapshots of allowlisted paper tables, read consistently per source. Not a backup, simultaneous portfolio valuation, uptime proof, or profitability verdict. Original ledgers remain authoritative. Unknown fixture provenance remains null.'}


def report(root):
    with closing(open_archive(root, readonly=True)) as db:
        return report_connection(db)


def main(root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'report', 'enable', 'disable'))
    args = parser.parse_args()
    try:
        if args.command in ('enable', 'disable'):
            configure(root, args.command == 'enable')
            print(encode({'enabled': enabled(root), 'execution_authority': False}))
        else:
            print(encode(collect(root) if args.command == 'collect' else report(root)))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
