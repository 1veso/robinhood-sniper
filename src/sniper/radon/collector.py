"""Radon's own bounded finalized receipt archive, never a qualified trading feed.

Public leaderboard rows discover candidates only. Address-signature history does
not establish complete token-account activity, opening inventory or historic
quotes. Those gaps must be reconciled before a radon_export_v1 can be produced.
"""
import json
import re
import sqlite3
import time
from urllib.error import HTTPError

from sniper.adapters.solana import request_json
from sniper.neon_sources import BOARD, leaderboard, decode_swap  # Pure decoders; no Neon account access.
from .config import digest, encode, policy_hash

GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
READ_METHODS = frozenset(('getGenesisHash', 'getSignaturesForAddress', 'getTransaction'))
MAX_PENDING = 1000
LIMITS = [
    'Candidates are unqualified public research leads, not verified trader identities.',
    'Wallet-address signatures do not prove complete token-account activity.',
    'Opening inventory, route coverage, historical quotes and cost accounting remain unverified.',
    'This archive cannot authorize entries, populate a shortlist or record fills.',
]


class CollectionBound(Exception):
    pass


class Collector:
    def __init__(self, book, *, transport=request_json, clock=time.time, monotonic=time.monotonic):
        self.book, self.transport, self.clock, self.monotonic = book, transport, clock, monotonic
        self.calls, self.deadline = 0, monotonic()+40
        self.db = None
        config = json.loads((book.folder/'collector.json').read_text(encoding='utf-8-sig'))
        binding = {'bot_id': 'radon', 'account_id': book.identity, 'policy_hash': policy_hash(book.cfg)}
        if config.get('enabled') is not True or config.get('binding') != binding or not str(config.get('authorization_reference', '')).strip():
            raise ValueError('Radon collector requires its own current account-bound authorization')
        if book.demo:
            raise ValueError('Network collector cannot run in a synthetic demo account')
        self.rpc_url = (book.root/'runtime/solana-rpc-url.txt').read_text().strip()
        if not self.rpc_url.startswith('https://'):
            raise ValueError('Read-only HTTPS Solana RPC configuration required')
        path = book.folder/'chain.sqlite3'
        if path.resolve() != path:
            raise ValueError('Radon chain archive may not redirect to another account')
        if path.exists():
            check = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
            try:
                row = check.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
                if not row or json.loads(row[0]) != binding:
                    raise ValueError('Radon chain archive belongs to another account or policy')
            finally:
                check.close()
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(wallet TEXT PRIMARY KEY,payload TEXT NOT NULL,
                head TEXT,head_before TEXT,head_top TEXT,history_before TEXT,
                boundary_reached INTEGER NOT NULL DEFAULT 0,last_polled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS signatures(wallet TEXT NOT NULL,signature TEXT NOT NULL,
                slot INTEGER NOT NULL,at INTEGER,payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0,receipt TEXT,receipt_hash TEXT,decoded TEXT,
                PRIMARY KEY(wallet,signature));
        ''')
        self.set('binding', binding)
        if self.get('history_boundary') is None:
            self.set('history_boundary', int(clock())-book.cfg['lookback_days']*86400)
        self.db.commit()

    def close(self):
        if self.db:
            self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, encode(value)))

    def request(self, url, body=None):
        if self.calls >= 8 or self.monotonic()+6 > self.deadline:
            raise CollectionBound()
        self.calls += 1
        try:
            return self.transport('POST' if body else 'GET', url, body=body,
                headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                         'User-Agent': 'sniper-paper/0.1 RadonCollector'}, timeout=6)
        except Exception as exc:
            # Exception text can contain the RPC credential URL.
            reason = 'HTTP '+str(exc.code) if isinstance(exc, HTTPError) else type(exc).__name__
            raise ValueError('Read provider unavailable: '+reason) from None

    def rpc(self, method, params):
        if method not in READ_METHODS:
            raise ValueError('Only allowlisted read-only RPC methods are permitted')
        value = self.request(self.rpc_url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        if not isinstance(value, dict) or value.get('error') or 'result' not in value:
            raise ValueError('Read-only RPC response error')
        return value['result']

    def refresh_candidates(self):
        now = int(self.clock())
        if now-self.get('candidates_at', 0) < 3600:
            return
        payload = self.request(BOARD)
        rows = leaderboard(payload, now)
        if not rows:
            raise ValueError('Public candidate list is empty')
        # Keep at most ten candidates in this initial cohort. Never silently
        # replace an archive cursor or promote leaderboard performance claims.
        count = self.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
        for row in rows:
            old = self.db.execute('SELECT 1 FROM candidates WHERE wallet=?', (row['wallet'],)).fetchone()
            if old:
                self.db.execute('UPDATE candidates SET payload=? WHERE wallet=?', (encode(row), row['wallet']))
            elif count < 10:
                self.db.execute('INSERT INTO candidates(wallet,payload) VALUES(?,?)', (row['wallet'], encode(row)))
                count += 1
        self.set('candidates_at', now)
        self.set('candidate_snapshot', payload)
        self.db.commit()

    def page(self, wallet, before=None):
        opts = {'limit': 100, 'commitment': 'finalized'}
        if before:
            opts['before'] = before
        rows = self.rpc('getSignaturesForAddress', [wallet, opts])
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Invalid signature page')
        seen, last_slot = set(), None
        for row in rows:
            signature = row.get('signature')
            if not isinstance(signature, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{80,90}', signature):
                raise ValueError('Invalid canonical transaction signature')
            slot, at = row.get('slot'), row.get('blockTime')
            if (signature in seen or signature == before or row.get('confirmationStatus') != 'finalized'
                    or type(slot) is not int or slot < 0 or (last_slot is not None and slot > last_slot)
                    or (at is not None and (type(at) is not int or at > int(self.clock())))):
                raise ValueError('Unfinalized, duplicate or unordered signature page')
            seen.add(signature); last_slot = slot
        for row in rows:
            old = self.db.execute('SELECT slot,at FROM signatures WHERE wallet=? AND signature=?',
                (wallet, row['signature'])).fetchone()
            if old and (old['slot'] != row['slot'] or old['at'] != row['blockTime']):
                raise ValueError('Conflicting signature provenance; cursor was not advanced')
        for row in rows:
            self.db.execute('INSERT OR IGNORE INTO signatures(wallet,signature,slot,at,payload) VALUES(?,?,?,?,?)',
                (wallet, row['signature'], row['slot'], row['blockTime'], encode(row)))
        return rows

    def poll(self):
        row = self.db.execute('SELECT * FROM candidates ORDER BY last_polled,wallet LIMIT 1').fetchone()
        if not row:
            raise ValueError('No public research candidates')
        wallet = row['wallet']
        rows = self.page(wallet, row['head_before'])
        top = row['head_top'] or (rows[0]['signature'] if rows else row['head'])
        caught_up = row['head'] is None or len(rows) < 100 or any(r['signature'] == row['head'] for r in rows)
        self.db.execute('UPDATE candidates SET head=?,head_before=?,head_top=?,last_polled=? WHERE wallet=?',
            (top if caught_up else row['head'], None if caught_up else rows[-1]['signature'],
             None if caught_up else top, int(self.clock()), wallet))
        before = row['history_before']
        if before is None and rows:
            before = rows[-1]['signature']
            self.db.execute('UPDATE candidates SET history_before=? WHERE wallet=?', (before, wallet))
        self.db.commit()
        if not row['boundary_reached']:
            history = self.page(wallet, before) if before else []
            reached = not history or any(type(r['blockTime']) is int and r['blockTime'] <= self.get('history_boundary') for r in history)
            self.db.execute('UPDATE candidates SET history_before=?,boundary_reached=? WHERE wallet=?',
                (history[-1]['signature'] if history else before, int(reached), wallet))
            self.db.commit()

    def receipts(self):
        # Alternate oldest/newest pending receipt on each run so history cannot
        # starve current observations, or vice versa. Missing receipts retry later.
        direction = 'ASC' if self.get('oldest_first', False) else 'DESC'
        self.set('oldest_first', direction == 'DESC'); self.db.commit()
        rows = self.db.execute('SELECT * FROM signatures WHERE state IN (\'PENDING\',\'MISSING\') '
            'AND attempts<3 AND retry_at<=? ORDER BY slot '+direction+' LIMIT 5', (int(self.clock()),)).fetchall()
        for row in rows:
            raw = self.rpc('getTransaction', [row['signature'], {'encoding': 'json',
                'commitment': 'finalized', 'maxSupportedTransactionVersion': 1}])
            decoded = None
            if raw is None:
                state = 'MISSING'
            else:
                if (not isinstance(raw, dict) or raw.get('slot') != row['slot']
                        or (raw.get('transaction') or {}).get('signatures', [None])[0] != row['signature']):
                    raise ValueError('Transaction receipt does not match requested finalized signature')
                state = 'UNSUPPORTED'
                try:
                    decoded = decode_swap(raw, row['signature'], row['wallet'], None, int(self.clock()))
                    state = 'SUPPORTED_SWAP'
                except (ValueError, KeyError, TypeError, IndexError):
                    if isinstance(raw.get('meta'), dict) and raw['meta'].get('err') is not None:
                        state = 'FAILED_TRANSACTION'
            self.db.execute('UPDATE signatures SET state=?,attempts=attempts+1,retry_at=?,receipt=?,receipt_hash=?,decoded=? '
                'WHERE wallet=? AND signature=?', (state, int(self.clock())+3600,
                encode(raw) if raw is not None else None, digest(raw) if raw is not None else None,
                encode(decoded) if decoded else None, row['wallet'], row['signature']))
            self.db.commit()

    def run(self):
        state, error = 'COLLECTING_HISTORY', None
        now = int(self.clock())
        if self.get('retry_after', 0) > now:
            return self.summary('BACKOFF', self.get('last_error'))
        try:
            # The credential URL itself is never persisted or exposed.
            endpoint = digest(self.rpc_url)
            if self.get('verified_endpoint') != endpoint:
                if self.rpc('getGenesisHash', []) != GENESIS:
                    raise ValueError('Configured RPC is not Solana mainnet')
                self.set('verified_endpoint', endpoint); self.db.commit()
            self.refresh_candidates()
            # Drain receipt work before discovering more signatures. Keep every
            # cursor intact: pausing discovery must not silently drop a head gap.
            pending = self.db.execute("SELECT COUNT(*) FROM signatures WHERE state='PENDING'").fetchone()[0]
            if pending < MAX_PENDING:
                self.poll()
            self.receipts()
        except CollectionBound:
            pass  # Durable cursors resume next bounded run, without a completeness claim.
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            state, error = 'ERROR', str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
            self.db.rollback()
            self.set('last_error', error); self.set('retry_after', now+60); self.db.commit()
        return self.summary(state, error)

    def summary(self, state, error=None):
        counts = dict(self.db.execute('SELECT state,COUNT(*) FROM signatures GROUP BY state').fetchall())
        return {'state': state, 'observed_at': int(self.clock()), 'account_id': self.book.identity,
            'requests_this_run': self.calls, 'candidate_count': self.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],
            'signature_count': sum(counts.values()), 'receipt_states': counts,
            'address_history_boundary_reached': self.db.execute('SELECT COUNT(*) FROM candidates WHERE boundary_reached=1').fetchone()[0],
            'archive_complete': False, 'entry_ready': False, 'error': error, 'limits': LIMITS}


def collect(book):
    path = book.folder/'collector.json'
    if not path.exists():
        return {'state': 'NOT_CONFIGURED', 'entry_ready': False}
    collector = Collector(book)
    try:
        result = collector.run()
        book.set('chain_collector', result)
        return result
    finally:
        collector.close()
