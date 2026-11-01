"""Radon-only transactions, observed leader inventory, and attributed copy lots."""
from contextlib import contextmanager
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time
import uuid

from rug_desk.book import atomic, process_lock
from .config import encode, digest, number, policy_hash, validate
from .source import validate_bundle
from .policy import buy_plan, sell_plan


def folder_for(root):
    root = Path(root).resolve()
    folder = root / 'radon_bot/runtime'
    if folder.resolve() != folder:
        raise ValueError('Radon runtime may not redirect into another account')
    return folder


def bound_config(root):
    path = folder_for(root) / 'book.sqlite3'
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        meta = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}
        if meta.get('bot_id') != 'radon' or policy_hash(meta['config']) != meta['policy_hash']:
            raise ValueError('Radon account identity/policy is invalid')
        return validate(meta['config'])
    finally:
        db.close()


class Book:
    def __init__(self, root, cfg, *, initialize=False, demo=False, clock=time.time):
        self.root, self.folder, self.cfg, self.clock = Path(root).resolve(), folder_for(root), validate(cfg), clock
        self.demo = demo
        path, marker = self.folder/'book.sqlite3', self.folder/'account.json'
        if initialize:
            with process_lock(self.folder/'init.lock'):
                if path.exists() != marker.exists():
                    raise ValueError('Incomplete Radon initialization requires recovery; never reset silently')
                if not path.exists():
                    identity = {'bot_id': 'radon', 'slot': 8, 'account_id': 'radon:' + uuid.uuid4().hex,
                        'mode': 'paper', 'demo': demo, 'policy_hash': policy_hash(cfg), 'created_at': int(clock())}
                    atomic(marker, encode(identity))
                    db = sqlite3.connect(path)
                    try:
                        db.executescript('''
                        PRAGMA journal_mode=WAL;
                        PRAGMA synchronous=FULL;
                        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE leaders(leader TEXT PRIMARY KEY,sequence INTEGER NOT NULL,at INTEGER NOT NULL,trusted INTEGER NOT NULL,opening_hash TEXT NOT NULL);
                        CREATE TABLE inventory(leader TEXT,mint TEXT,quantity TEXT NOT NULL,PRIMARY KEY(leader,mint));
                        CREATE TABLE events(id TEXT PRIMARY KEY,leader TEXT NOT NULL,sequence INTEGER NOT NULL,at INTEGER NOT NULL,observed_at INTEGER NOT NULL,mint TEXT NOT NULL,side TEXT NOT NULL,state TEXT NOT NULL,fraction TEXT,payload TEXT NOT NULL,hash TEXT NOT NULL,UNIQUE(leader,sequence));
                        CREATE TABLE positions(leader TEXT,mint TEXT,quantity TEXT NOT NULL,basis TEXT NOT NULL,buy_count INTEGER NOT NULL,PRIMARY KEY(leader,mint));
                        CREATE TABLE episodes(mint TEXT PRIMARY KEY,spent TEXT NOT NULL,number INTEGER NOT NULL);
                        CREATE TABLE fills(id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,at INTEGER NOT NULL,side TEXT NOT NULL,leader TEXT NOT NULL,mint TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,at INTEGER NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE shortlists(id TEXT PRIMARY KEY,effective_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE marks(mint TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE runs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,started_at INTEGER NOT NULL,finished_at INTEGER,state TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE api_requests(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                        CREATE TABLE research(id TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
                        ''')
                        meta = {**identity, 'config': cfg, 'cash': cfg['starting_cash_usd'], 'turnover': '0',
                            'realized_pnl': '0', 'network_costs': '0', 'embedded_friction': '0', 'rent_reserved': '0',
                            'peak_deployed': '0', 'source': {'state': 'UNAVAILABLE'}, 'model_gate': {'enabled': False}}
                        db.executemany('INSERT INTO meta VALUES(?,?)', [(k, encode(v)) for k, v in meta.items()])
                        db.commit()
                    finally:
                        db.close()
        if not path.is_file() or not marker.is_file():
            raise ValueError('Initialize Radon explicitly; no account was created')
        # Read identity before acquiring a writable handle to any existing database.
        check = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            meta = {k: json.loads(v) for k, v in check.execute('SELECT key,value FROM meta')}
            identity = json.loads(marker.read_text())
            if meta.get('bot_id') != 'radon' or identity.get('bot_id') != 'radon' or any(meta.get(k) != v for k, v in identity.items()):
                raise ValueError('Radon cannot adopt another bot ledger or mismatched identity marker')
            if meta['demo'] is not demo or meta['policy_hash'] != policy_hash(cfg) or policy_hash(meta['config']) != meta['policy_hash']:
                raise ValueError('Radon policy/demo binding changed; explicit migration required')
            if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Ledger integrity check failed')
        finally:
            check.close()
        self.identity = meta['account_id']
        self.db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, encode(value)))

    def add(self, key, value):
        self.set(key, str(D(self.get(key)) + value))

    def provider(self, name, value=None):
        if value is None:
            return self.get('provider:'+name)
        self.set('provider:'+name, value)

    def decision(self, event, action, reason, now, payload=None):
        self.db.execute('INSERT INTO decisions(event_id,at,action,reason,payload) VALUES(?,?,?,?,?)',
                        (event, now, action, str(reason)[:500], encode(payload or {})))

    def run_start(self, kind, now):
        run = uuid.uuid4().hex
        self.db.execute('INSERT INTO runs VALUES(?,?,?,NULL,?,?)', (run, kind, now, 'RUNNING', '{}'))
        return run

    def run_finish(self, run, now, errors):
        self.db.execute('UPDATE runs SET finished_at=?,state=?,payload=? WHERE id=?',
            (now, 'ERROR' if errors else 'COMPLETE', encode({'errors': errors}), run))

    def ingest(self, bundle, now):
        validate_bundle(bundle, demo=self.demo)
        if bundle['through'] > now:
            raise ValueError('Source claims future coverage')
        with self.transaction():
            source = self.get('source')
            if source.get('source_id') not in (None, bundle['source_id']):
                raise ValueError('Changing source identity requires reconciliation')
            for leader, opening in bundle['openings'].items():
                previous = self.db.execute('SELECT * FROM leaders WHERE leader=?', (leader,)).fetchone()
                fingerprint = digest({'opening': opening, 'at': bundle['opening_at']})
                if previous:
                    if previous['opening_hash'] != fingerprint:
                        raise ValueError('Opening inventory changed; reconcile instead of resetting')
                else:
                    self.db.execute('INSERT INTO leaders VALUES(?,?,?,?,?)', (leader, opening['sequence'], bundle['opening_at'], 1, fingerprint))
                    self.set('opening:'+leader, {'at': bundle['opening_at'], 'evidence': opening})
                    self.db.executemany('INSERT INTO inventory VALUES(?,?,?)', [(leader, mint, str(number(q))) for mint, q in opening['balances'].items()])
            for row in sorted(bundle['events'], key=lambda r: (r['at'], r['leader'], r['sequence'])):
                prior = self.db.execute('SELECT hash FROM events WHERE id=?', (row['id'],)).fetchone()
                if prior:
                    if prior[0] != digest(row):
                        raise ValueError('Previously observed event changed')
                    continue
                leader = self.db.execute('SELECT * FROM leaders WHERE leader=?', (row['leader'],)).fetchone()
                if row['sequence'] != leader['sequence'] + 1 or row['at'] < leader['at']:
                    raise ValueError('Incomplete or out-of-order leader history; no inferred sell fraction')
                inv = self.db.execute('SELECT quantity FROM inventory WHERE leader=? AND mint=?', (row['leader'], row['mint'])).fetchone()
                held = number(inv[0]) if inv else D(0)
                qty, fraction, trusted = number(row['quantity'], positive=True), None, leader['trusted']
                if row['side'] == 'buy':
                    held += qty
                elif row['side'] == 'sell':
                    if not trusted or qty > held or held == 0:
                        raise ValueError('Unknown leader inventory; exit requires reconciliation')
                    fraction = str(qty / held)
                    held -= qty
                else:
                    trusted = 0
                self.db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?,?)', (row['leader'], row['mint'], str(held)))
                self.db.execute('UPDATE leaders SET sequence=?,at=?,trusted=? WHERE leader=?', (row['sequence'], row['at'], trusted, row['leader']))
                state = 'BUY_PENDING' if row['side'] == 'buy' else 'EXIT_PENDING' if row['side'] == 'sell' else 'UNSUPPORTED'
                self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (row['id'], row['leader'], row['sequence'], row['at'], now, row['mint'], row['side'], state, fraction, encode(row), digest(row)))
                if state == 'UNSUPPORTED':
                    self.decision(row['id'], 'SKIP', 'Unsupported activity makes this leader inventory incomplete', now)
            tracked = {r[0] for r in self.db.execute('SELECT DISTINCT leader FROM positions WHERE CAST(quantity AS REAL)>0')}
            missing = tracked - set(bundle['openings'])
            self.set('source', {'state': 'PARTIAL' if missing else 'OK', 'source_id': bundle['source_id'], 'through': bundle['through'],
                'observed_at': now, 'coverage': bundle['coverage'], 'missing_exit_leaders': sorted(missing),
                'evidence_label': bundle['evidence_label'], 'authorization_reference': bundle['authorization_reference'], 'hash': digest(bundle)})

    def shortlist(self, now):
        row = self.db.execute('SELECT * FROM shortlists WHERE effective_at<=? ORDER BY effective_at DESC,rowid DESC LIMIT 1', (now,)).fetchone()
        return dict(row, payload=json.loads(row['payload'])) if row else None

    def publish_shortlist(self, leaders, now, research_id):
        with self.transaction():
            previous = self.shortlist(now)
            old = set(previous['payload']['leaders']) if previous else set()
            identity = uuid.uuid4().hex
            payload = {'leaders': sorted(set(leaders)), 'joined': sorted(set(leaders)-old), 'dropped': sorted(old-set(leaders)),
                'research_id': research_id, 'policy_hash': policy_hash(self.cfg), 'evidence_label': 'synthetic' if self.demo else 'prospective_selection'}
            self.db.execute('INSERT INTO shortlists VALUES(?,?,?,?)', (identity, now, now+self.cfg['shortlist_ttl_seconds'], encode(payload)))
            return identity

    def eligibility(self, event, now):
        row = dict(self.db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone())
        if row['side'] != 'buy' or row['state'] not in ('BUY_PENDING', 'RESEARCHING'):
            raise ValueError('Entry event already terminal or not a buy')
        shortlist = self.shortlist(now)
        if not shortlist or now >= shortlist['expires_at'] or row['leader'] not in shortlist['payload']['leaders']:
            raise ValueError('Leader is not on a current shortlist')
        if row['at'] <= shortlist['effective_at']:
            raise ValueError('Pre-activation signal; no retroactive copies')
        source = self.get('source')
        if source.get('state') != 'OK' or not 0 <= now-source.get('through', -1) <= self.cfg['source_max_age_seconds']:
            raise ValueError('Source unavailable, incomplete or stale')
        if not self.cfg['execution_delay_seconds'] <= now-row['at'] <= self.cfg['signal_expiry_seconds']:
            raise ValueError('Signal outside execution-delay/expiry window')
        if not self.db.execute('SELECT trusted FROM leaders WHERE leader=?', (row['leader'],)).fetchone()[0]:
            raise ValueError('Leader inventory is incomplete')
        if self.db.execute("SELECT 1 FROM events WHERE leader=? AND mint=? AND sequence>? AND side IN ('sell','unsupported')", (row['leader'], row['mint'], row['sequence'])).fetchone():
            raise ValueError('Leader reduced position before research completed')
        if self.db.execute("SELECT 1 FROM events WHERE mint=? AND state='EXIT_PENDING'", (row['mint'],)).fetchone():
            raise ValueError('Unresolved exit for mint; new entries paused')
        return row

    def position(self, leader, mint):
        row = self.db.execute('SELECT * FROM positions WHERE leader=? AND mint=?', (leader, mint)).fetchone()
        return dict(row) if row else {'leader': leader, 'mint': mint, 'quantity': '0', 'basis': '0', 'buy_count': 0}

    def deployed(self):
        return sum((number(r[0]) for r in self.db.execute('SELECT basis FROM positions')), D(0)) + number(self.get('rent_reserved'))

    def available_cash(self):
        liability = D(0)
        for raw, in self.db.execute('SELECT payload FROM api_requests'):
            audit = json.loads(raw)
            billed = audit.get('usage', {}).get('billed_usd')
            liability += number(billed if billed is not None else audit.get('reservation_usd', self.cfg['model_reservation_usd']))
        return number(self.get('cash'))-liability

    def skip(self, event, reason, now, payload=None):
        with self.transaction():
            row = self.db.execute('SELECT state FROM events WHERE id=?', (event,)).fetchone()
            if row and row[0] in ('BUY_PENDING', 'RESEARCHING'):
                self.db.execute("UPDATE events SET state='SKIPPED' WHERE id=?", (event,))
                self.decision(event, 'SKIP', reason, now, payload)

    def buy(self, event, quote, now, evidence, *, replay=False):
        from sniper.paper_window import require_open
        require_open(self.get('paper_trial'), self.identity, now)
        with self.transaction():
            row = self.eligibility(event, now)
            if replay:
                if not self.demo:
                    raise ValueError('Historical execution is confined to an isolated replay account')
            else:
                from .policy import integrity_check
                integrity_check(self.cfg, evidence, row['mint'], now)
                review = evidence.get('x', {})
                request = self.db.execute('SELECT payload FROM api_requests WHERE id=?', (review.get('request_id'),)).fetchone()
                audit = json.loads(request[0]) if request else {}
                if (not review.get('validated') or review.get('verdict') != 'BUY' or review.get('account_id') != self.identity
                    or review.get('policy_hash') != policy_hash(self.cfg) or review.get('event_id') != event
                    or audit.get('state') != 'RECEIVED' or audit.get('result') != review
                    or audit.get('evidence_hash') != digest({k: evidence[k] for k in ('quote','facts','lp')})
                    or not 0 <= now-review.get('judged_at', 0) <= self.cfg['evidence_max_age_seconds']):
                    raise ValueError('Fresh Radon-bound X evidence and billing journal required')
                if not self.demo and (review.get('synthetic') or audit.get('synthetic')):
                    raise ValueError('Synthetic evidence cannot authorize real-data paper entries')
                if quote.get('pool') != evidence['quote'].get('pool'):
                    raise ValueError('Refreshed quote must retain the independently checked pool')
            episode = self.db.execute('SELECT * FROM episodes WHERE mint=?', (row['mint'],)).fetchone()
            spent = number(episode['spent']) if episode else D(0)
            plan = buy_plan(self.cfg, quote, row['mint'], now, cash=self.available_cash(), deployed=self.deployed(),
                episode_spend=spent, route=json.loads(row['payload'])['route'])
            ticket, fee, rent, qty = number(self.cfg['ticket_usd']), number(plan['explicit_fee_usd']), number(plan['rent_capital_reserved_usd']), number(plan['quantity'])
            p = self.position(row['leader'], row['mint'])
            self.db.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?)', (row['leader'], row['mint'], str(number(p['quantity'])+qty), str(number(p['basis'])+ticket+fee), p['buy_count']+1))
            self.db.execute('INSERT OR REPLACE INTO episodes VALUES(?,?,?)', (row['mint'], str(spent+ticket), episode['number'] if episode else 1))
            self.add('cash', -ticket-fee-rent)
            self.add('turnover', ticket)
            self.add('rent_reserved', rent)
            self.set('peak_deployed', str(max(number(self.get('peak_deployed')), self.deployed())))
            self._fill(row, 'buy', plan, now, {'notional': str(ticket), 'basis': str(ticket+fee), 'evidence': evidence})
            self.mark(quote, now)
            return plan

    def sell(self, event, quote, now):
        with self.transaction():
            row = self.db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone()
            if not row or row['state'] != 'EXIT_PENDING':
                return None
            if self.db.execute("SELECT 1 FROM events WHERE leader=? AND mint=? AND sequence<? AND state='EXIT_PENDING'", (row['leader'], row['mint'], row['sequence'])).fetchone():
                raise ValueError('Earlier attributed exit is pending')
            p = self.position(row['leader'], row['mint'])
            if number(p['quantity']) == 0:
                self.db.execute("UPDATE events SET state='NO_POSITION' WHERE id=?", (event,))
                return None
            fraction = number(row['fraction'], positive=True)
            if fraction > 1:
                raise ValueError('Invalid leader sell fraction')
            qty, basis = number(p['quantity'])*fraction, number(p['basis'])*fraction
            plan = sell_plan(self.cfg, quote, row['mint'], now, qty)
            proceeds = qty*number(plan['fill_price']) - number(plan['explicit_fee_usd'])
            if self.available_cash() + proceeds < 0:
                raise ValueError('Insufficient paper cash for exit costs')
            self.db.execute('UPDATE positions SET quantity=?,basis=? WHERE leader=? AND mint=?',
                (str(number(p['quantity'])-qty), str(number(p['basis'])-basis), row['leader'], row['mint']))
            self.add('cash', proceeds)
            self.add('realized_pnl', proceeds-basis)
            if not any(number(r[0]) > 0 for r in self.db.execute('SELECT quantity FROM positions WHERE mint=?', (row['mint'],))):
                self.db.execute("UPDATE episodes SET spent='0',number=number+1 WHERE mint=?", (row['mint'],))
            self._fill(row, 'sell', plan, now, {'proceeds': str(proceeds), 'allocated_basis': str(basis), 'realized_pnl': str(proceeds-basis), 'leader_fraction': str(fraction)})
            self.mark(quote, now)
            return plan

    def _fill(self, row, side, plan, now, detail):
        self.add('network_costs', number(plan['explicit_fee_usd']))
        self.add('embedded_friction', number(plan['embedded_friction_usd']))
        payload = {**detail, 'costs': plan, 'source_at': row['at'], 'observed_at': row['observed_at'], 'fill_at': now,
            'latency_seconds': now-row['at'], 'policy_hash': policy_hash(self.cfg), 'account_id': self.identity,
            'evidence_label': 'synthetic' if self.demo else 'real_data_paper', 'executable': False}
        self.db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?)', (uuid.uuid4().hex, row['id'], now, side, row['leader'], row['mint'], encode(payload)))
        self.db.execute("UPDATE events SET state='FILLED' WHERE id=?", (row['id'],))
        self.decision(row['id'], 'PAPER_'+side.upper(), 'Modeled execution; sellability is unverified', now, payload)

    def mark(self, quote, now):
        from .policy import quote_check
        quote_check(self.cfg, quote, quote['mint'], now)
        self.db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?)', (quote['mint'], quote['observed_at'], encode(quote)))

    def publish(self):
        from .view import snapshot
        with self.transaction():
            value = snapshot(self.db, int(self.clock()))
        atomic(self.folder/'status.json', encode(value))
