"""Isolated Xenon baseline/crew portfolios over a prospective observation tape.

No provider, notification, signer or strategy-account write is performed here.
The original Book, cost simulation and watcher remain the financial engine.
"""
from contextlib import contextmanager, closing
from decimal import Decimal as D
import hashlib
import html
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from rug_desk import config
from rug_desk.book import Book, atomic, encode, number
from rug_desk.market import check_limits, validate_quote
from rug_desk.watch import apply_quote
from .jev import POLICY, gate, validate_response
from .jev_dossier import digest, validate as validate_dossier
from .jev_lifecycle import code_binding
from .neon import paper_snapshot
from .adapters.solana import validate_address

FORMAT = 'xenon_crew_comparison_v1'
VARIANTS = ('baseline', 'crew')
KINDS = ('candidate', 'watch', 'gap', 'cost', 'trial')


def config_hash():
    return hashlib.sha256(encode(config.settings()).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def reader(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')
    return con


def source_identity(source_root):
    root = Path(source_root)
    with closing(reader(root / 'runtime/book.sqlite3')) as con:
        identity = json.loads(con.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
    if identity != read(root / 'runtime/account.json') or identity['mode'] != 'paper':
        raise ValueError('Source paper identity mismatch')
    if identity['config_sha256'] != config_hash():
        raise ValueError('Source configuration differs from the current approved Book')
    return identity


def source_trial(source_root, account):
    root = Path(source_root)
    with closing(reader(root / 'runtime/book.sqlite3')) as con:
        row = con.execute("SELECT value FROM metadata WHERE key='trial'").fetchone()
        stored = json.loads(row[0]) if row else None
    path = root / 'runtime/trial.json'
    view = read(path) if path.exists() else None
    if view and stored is None:
        raise ValueError('Source trial view has no authoritative ledger record')
    if stored and view and stored != view:
        raise ValueError('Source trial and publication differ')
    trial = stored
    if trial is not None:
        if (trial.get('account_id') != account or type(trial.get('started_at')) is not int
                or type(trial.get('ends_at')) is not int or trial['started_at'] >= trial['ends_at']):
            raise ValueError('Source trial binding invalid')
    return trial


def paid_authority(source_root, now):
    """Read existing authority; never enables a model or sends a request."""
    from rug_desk.model_gate import status
    from .jev_review import settings
    root = Path(source_root)
    grok = status(root, now)
    jev = settings(root.parent)
    return {'observed_at': now, 'grok_enabled': grok['enabled'],
            'jev_enabled': bool(jev['enabled'] and jev['paid_authorized']
                                and D(jev['daily_budget_usd']) > 0),
            'scope': 'Existing account-bound paid model gates; this record grants no spending authority'}


def make_baseline_packet(source_root, assessment, judgment, decided_at):
    """Bind the actual pre-entry assessment, not a caller-supplied eligibility flag."""
    identity = source_identity(source_root)
    body = {'schema_version': 1, 'scope': 'XENON_BASELINE_ADMISSION',
            'source_account_id': identity['account_id'], 'config_sha256': identity['config_sha256'],
            'code_binding': code_binding(Path(source_root).parent), 'decided_at': decided_at,
            'assessment': assessment, 'judgment': judgment,
            'paid_authority': paid_authority(source_root, decided_at)}
    return {**body, 'packet_id': digest(body)}


def validate_baseline(packet, quote, manifest, now):
    if not isinstance(packet, dict):
        raise ValueError('Baseline admission evidence missing')
    body = {k: v for k, v in packet.items() if k != 'packet_id'}
    if (packet.get('packet_id') != digest(body) or packet.get('schema_version') != 1
            or packet.get('scope') != 'XENON_BASELINE_ADMISSION'
            or packet.get('source_account_id') != manifest['source_account_id']
            or packet.get('config_sha256') != manifest['config_sha256']
            or packet.get('code_binding') != manifest['code_binding']
            or type(packet.get('decided_at')) is not int or packet['decided_at'] != now):
        raise ValueError('Baseline packet binding invalid')
    authority = packet['paid_authority']
    if (authority.get('grok_enabled') is not True or authority.get('jev_enabled') is not True
            or authority.get('observed_at') != now):
        raise ValueError('Original paid-model gates are not enabled')
    a = packet['assessment']; d = a['dossier']; b = a['baseline']; result = a['result']
    evidence = validate_dossier(d, now)
    if (a.get('id') != digest({'dossier': d, 'policy': POLICY})
            or a.get('account_id') != manifest['source_account_id']
            or a.get('status') != 'COMPLETE' or a.get('attempted') != 1
            or b.get('account_id') != manifest['source_account_id']
            or b.get('policy_sha256') != manifest['config_sha256']
            or b.get('candidate_id') != d['candidate_id'] or not b.get('prospective')
            or b.get('verdict') != 'BUY' or b.get('eligible') is not True
            or d['account_id'] != manifest['source_account_id']
            or d['mint'] != quote['mint'] or d['pool'] != quote['pool']
            or D(str(d['ticket_usd'])) != D(config.TICKET_USD)
            or evidence['missing_required'] or result.get('policy') != POLICY
            or result.get('proposed') != 'BUY'
            or gate(validate_response(result['response']))[0] != 'BUY'
            or type(a.get('finished')) is not int or not d['snapshot_at'] <= a['finished'] <= now
            or now - a['finished'] > POLICY['max_age_seconds']):
        raise ValueError('Current bound prospective Jev BUY required')
    j = packet['judgment']; binding = j.get('binding') or {}
    if (j.get('verdict') != 'BUY' or j.get('validated') is not True
            or not D(str(config.MIN_CONFIDENCE)) <= number(j.get('confidence')) <= 1
            or type(j.get('judged_at')) is not int or not 0 <= now - j['judged_at'] <= POLICY['max_age_seconds']
            or binding.get('account_id') != manifest['source_account_id']
            or binding.get('settings_sha256') != manifest['config_sha256']):
        raise ValueError('Bound baseline model judgment required')
    if not j.get('request_id') or (j.get('usage') or {}).get('cost_verified') is not True:
        raise ValueError('Audited baseline model request and billed usage required')
    validate_quote(quote, now)
    failure = check_limits(quote, now)
    if failure:
        raise ValueError(failure)
    return a


def require_quantity_coverage(dossier, records, quote, policy, now):
    """The refreshed original-cost buy must fit the independently reviewed exit."""
    from .execution_costs import simulate
    reference = (dossier.get('exit_route') or {}).get('source_ref')
    matched = [r for r in records if 'rug_desk:crew:' + digest(r) == reference]
    if len(matched) != 1:
        raise ValueError('Exact-size exit producer payload missing')
    record = matched[0]
    if (record.get('account_id') != dossier.get('account_id') or record.get('config_sha256') != config_hash()
            or record.get('mint') != dossier.get('mint') or dossier.get('mint') != quote.get('mint')
            or record.get('pool') != dossier.get('pool') or dossier.get('pool') != quote.get('pool')):
        raise ValueError('Exit proof account, configuration or pinned pool differs')
    from .crew_review import require_program_review
    require_program_review(record, now=now)
    route = record['exit']; state = route['state']; mint = quote['mint']
    side = 0 if state.get('mint0') == mint else 1 if state.get('mint1') == mint else None
    if side is None: raise ValueError('Exit proof mint differs from refreshed quote')
    decimals = state['decimals' + str(side)]
    if type(decimals) is not int or not 0 <= decimals <= 18:
        raise ValueError('Exit proof precision invalid')
    coverage = number(route['sell_quantity_raw']) / (10 ** decimals)
    actual = number(simulate(policy, 'buy', quote, config.TICKET_USD, now)['quantity'])
    if actual > coverage:
        raise ValueError('Refreshed paper quantity exceeds independently reviewed exit size')


class ComparisonBook(Book):
    """Savepoints let the existing Book/watcher and input cursor commit together."""
    @contextmanager
    def transaction(self):
        if not self.db.in_transaction:
            with super().transaction():
                yield
            return
        name = 'crew_' + uuid.uuid4().hex
        self.db.execute('SAVEPOINT ' + name)
        try:
            yield
            self.db.execute('RELEASE SAVEPOINT ' + name)
        except BaseException:
            self.db.execute('ROLLBACK TO SAVEPOINT ' + name)
            self.db.execute('RELEASE SAVEPOINT ' + name)
            raise


def initialize(folder, source_root, *, now=None, trial=None):
    """Explicitly seed two separate Books from one source snapshot. Never resets."""
    folder, source_root = Path(folder).resolve(), Path(source_root).resolve()
    now = int(time.time()) if now is None else now
    identity = source_identity(source_root)
    actual_trial = source_trial(source_root, identity['account_id'])
    if trial is not None and trial != actual_trial:
        raise ValueError('A comparison cannot create or change the source trial')
    if actual_trial and now >= actual_trial['ends_at']:
        raise ValueError('Cannot initialize after original entry deadline')
    if folder == source_root or source_root.is_relative_to(folder):
        raise ValueError('Comparison and source roots must be separate')
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Comparison exists or interrupted initialization needs review')
    folder.mkdir(parents=True, exist_ok=True)
    seed_path = folder / 'seed.sqlite3'
    with closing(reader(source_root / 'runtime/book.sqlite3')) as source, closing(sqlite3.connect(seed_path)) as seed:
        source.backup(seed)
    with closing(reader(seed_path)) as seed:
        metadata = dict(seed.execute('SELECT key,value FROM metadata'))
        if json.loads(metadata['identity']) != identity:
            raise ValueError('Source identity changed during seed')
        if 'crew_variant' in metadata:
            raise ValueError('An isolated comparison cannot seed another strategy comparison')
        cost_policy = json.loads(metadata['execution_cost_policy'])
        from .execution_costs import validate_policy
        validate_policy(cost_policy)
        baseline = json.loads(metadata['capital_baseline']) if 'capital_baseline' in metadata else None
        if baseline and (baseline.get('account_id') != identity['account_id'] or not baseline.get('reference')):
            raise ValueError('Source capital baseline is not bound')
        fill_rows = [json.loads(r[0]) for r in seed.execute('SELECT payload FROM fills')]
        seed_fills = [f['id'] for f in fill_rows]
        seed_positions = [json.loads(r[0]) for r in seed.execute('SELECT payload FROM positions')]
        seed_financial = paper_snapshot(fill_rows, seed_positions, now,
            starting_cash=baseline['starting_cash_usd'] if baseline else None)
        manifest = {'format': FORMAT, 'comparison_id': str(uuid.uuid4()),
                    'source_account_id': identity['account_id'], 'source_root': str(source_root),
                    'source_stream_id': metadata.get('observation_stream_id'),
                    'source_highwater': seed.execute('SELECT COALESCE(MAX(seq),0) FROM observation_events').fetchone()[0],
                    'config_sha256': identity['config_sha256'], 'settings': config.settings(),
                    'code_binding': code_binding(source_root.parent), 'cost_policy': cost_policy,
                    'capital_baseline': baseline, 'trial': actual_trial, 'started_at': now,
                    'seed_fill_ids': seed_fills, 'seed_position_ids': [p['id'] for p in seed_positions],
                    'seed_net_pnl': seed_financial['net_pnl'],
                    'excluded_from_strategy': True, 'accounts': {},
                    'intake_scope': 'Prospective inputs reached by the existing Xenon check and crew review. Source selection, early account/model gates, missing evidence and bounded follow-up constrain coverage; not all market opportunities.',
                    'cost_scope': 'Original indicative Solana cost scenario; not actual executed swaps. Unknown model bills keep complete net performance unknown.'}
    for name in VARIANTS:
        target = folder / name / 'runtime'; target.mkdir(parents=True)
        with closing(reader(seed_path)) as seed, closing(sqlite3.connect(target / 'book.sqlite3')) as con:
            seed.backup(con)
            own = {**identity, 'account_id': str(uuid.uuid4()), 'created_at': now}
            manifest['accounts'][name] = own['account_id']
            con.execute("UPDATE metadata SET value=? WHERE key='identity'", (encode(own),))
            con.execute("UPDATE metadata SET value=? WHERE key='observation_stream_id'", (str(uuid.uuid4()),))
            con.execute("INSERT OR REPLACE INTO metadata VALUES('crew_cursor','0')")
            con.execute("INSERT OR REPLACE INTO metadata VALUES('crew_variant',?)", (name,))
            con.execute("INSERT OR REPLACE INTO metadata VALUES('crew_trial',?)", (encode(actual_trial),))
            # These are comparison bindings; never advertise a new strategy trial.
            con.execute("DELETE FROM metadata WHERE key='trial'")
            if baseline:
                con.execute("UPDATE metadata SET value=? WHERE key='capital_baseline'", (encode({**baseline, 'account_id': own['account_id']}),))
            con.executescript('''CREATE TABLE crew_inputs(seq INTEGER PRIMARY KEY,digest TEXT NOT NULL,observed_at INTEGER NOT NULL,processed_at REAL NOT NULL);
                CREATE TABLE crew_decisions(seq INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE crew_costs(request_id TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE crew_equity(seq INTEGER PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);''')
            con.commit()
        atomic(target / 'account.json', encode(own))
    with closing(sqlite3.connect(folder / 'observations.sqlite3')) as con:
        con.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,
                observed_at INTEGER NOT NULL,captured_at REAL NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);''')
        con.execute('INSERT INTO metadata VALUES(?,?)', ('binding', encode(manifest)))
        con.commit()
    atomic(folder / 'manifest.json', encode(manifest))
    return manifest


class Comparison:
    def __init__(self, folder, *, source_account_id):
        self.folder = Path(folder).resolve()
        self.manifest = read(self.folder / 'manifest.json')
        m = self.manifest
        if (m.get('format') != FORMAT or m.get('source_account_id') != source_account_id
                or m.get('excluded_from_strategy') is not True or set(m.get('accounts', {})) != set(VARIANTS)
                or len(set(m['accounts'].values()) | {source_account_id}) != 3
                or m.get('config_sha256') != config_hash()):
            raise ValueError('Crew comparison binding mismatch')
        self._verify_source()
        self.db = sqlite3.connect((self.folder / 'observations.sqlite3').as_uri() + '?mode=rw', uri=True, timeout=2)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0] != encode(m):
            self.db.close(); raise ValueError('Comparison tape binding changed')
        self.books = {}

    def _verify_source(self):
        m = self.manifest
        identity = source_identity(m['source_root'])
        if identity['account_id'] != m['source_account_id'] or identity['config_sha256'] != m['config_sha256']:
            raise ValueError('Comparison source identity changed')
        with closing(reader(Path(m['source_root']) / 'runtime/book.sqlite3')) as con:
            row = con.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()
            if not row or row[0] != m['source_stream_id']:
                raise ValueError('Comparison source observation stream changed')

    def close(self):
        for book in self.books.values(): book.close()
        self.db.close()

    def portfolios(self):
        if not self.books:
            try:
                for name in VARIANTS:
                    book = ComparisonBook(self.folder / name)
                    self.books[name] = book
                    if book.identity['account_id'] != self.manifest['accounts'][name] or book.cost_policy() != self.manifest['cost_policy']:
                        raise ValueError('Comparison portfolio binding changed')
            except BaseException:
                for book in self.books.values(): book.close()
                self.books = {}; raise
        return self.books

    def capture(self, key, kind, observed_at, payload, *, captured_at=None):
        self._verify_source()
        captured_at = time.time() if captured_at is None else captured_at
        if (kind not in KINDS or not isinstance(key, str) or not key or len(key) > 500
                or isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at)
                or type(observed_at) is not int or not self.manifest['started_at'] <= observed_at <= captured_at
                or not isinstance(payload, dict)):
            raise ValueError('Invalid prospective comparison input')
        if kind == 'trial':
            if payload.get('trial') != source_trial(self.manifest['source_root'], self.manifest['source_account_id']):
                raise ValueError('Trial input is not the original source trial')
        encoded = encode(payload); checksum = digest([kind, observed_at, payload])
        with self.db:
            old = self.db.execute('SELECT seq,digest FROM observations WHERE event_key=?', (key,)).fetchone()
            if old:
                if old['digest'] != checksum: raise ValueError('Comparison input reused with changed content')
                return old['seq']
            last = self.db.execute('SELECT MAX(observed_at) FROM observations').fetchone()[0]
            if last is not None and observed_at < last:
                raise ValueError('Out-of-order comparison input; record a gap instead of backdating')
            return self.db.execute('INSERT INTO observations(event_key,observed_at,captured_at,kind,payload,digest) VALUES(?,?,?,?,?,?)',
                                   (key, observed_at, captured_at, kind, encoded, checksum)).lastrowid

    def refresh_trial(self, now):
        trial = source_trial(self.manifest['source_root'], self.manifest['source_account_id'])
        if trial is not None:
            key = 'source-trial:' + digest(trial)
            old = self.db.execute('SELECT seq,payload FROM observations WHERE event_key=?', (key,)).fetchone()
            if old:
                if json.loads(old['payload']) != {'trial': trial}: raise ValueError('Source trial capture changed')
                return old['seq']
            return self.capture(key, 'trial', now, {'trial': trial})
        return None

    def _candidate(self, book, name, row, payload):
        now = row['observed_at']; result = payload.get('result') or {}
        if not isinstance(result, dict): result = {}
        quote = result.get('refreshed_pool') or result.get('pool')
        packet = payload.get('baseline_packet'); review = payload.get('crew_packet')
        original_at = payload.get('original_check_at')
        original_at = original_at if type(original_at) is int and 0 <= original_at <= now else None
        candidate = (packet or {}).get('assessment', {}).get('dossier', {}).get('candidate_id')
        candidate = candidate or self.manifest['source_account_id'] + ':' + str(result.get('mint', 'unknown'))
        decision = {'candidate_id': candidate, 'at': now, 'mint': result.get('mint'),
                    'pool': quote.get('pool') if isinstance(quote, dict) else None,
                    'variant': name, 'entry': False, 'status': 'REJECTED', 'reason': None,
                    'source_stage': result.get('stage'), 'source_observed_at': (packet or {}).get('assessment', {}).get('snapshot_at'),
                    'captured_at': row['captured_at'], 'baseline_packet_id': (packet or {}).get('packet_id'),
                    'crew_packet_id': (review or {}).get('packet_id'),
                    'crew_complete': bool((review or {}).get('crew_complete')),
                    'quote': quote, 'basis_scenario': None, 'original_check_at': original_at,
                    'latency_seconds': now - original_at if original_at is not None else None,
                    'evidence_age_seconds': None}
        try:
            mint = validate_address(result.get('mint'))
            if book.checked(mint): raise ValueError('Mint was already checked in this portfolio')
            book.claim(mint, now)
            if not isinstance(quote, dict): raise ValueError('No recorded candidate quote')
            if quote.get('mint') != mint: raise ValueError('Candidate mint and quote differ')
            validate_quote(quote, now)
            trial = json.loads(book.db.execute("SELECT value FROM metadata WHERE key='crew_trial'").fetchone()[0])
            if trial is None:
                # A source may have legitimately started after comparison setup.
                # Adopt only its ledger-bound original clock, never a new one,
                # and never retroactively authorize an earlier captured input.
                actual = source_trial(self.manifest['source_root'], self.manifest['source_account_id'])
                if actual and actual['started_at'] <= now:
                    trial = actual
                    book.db.execute("UPDATE metadata SET value=? WHERE key='crew_trial'", (encode(trial),))
                    book.db.execute("INSERT OR IGNORE INTO metadata VALUES('crew_trial_adoption',?)",
                                    (encode({'input_seq':row['seq'],'observed_at':now,'source_trial':trial}),))
            if trial is None:
                decision['status'] = 'SETUP_BLOCKED'
                raise ValueError('Original source trial has not started')
            if not trial['started_at'] <= now < trial['ends_at']:
                raise ValueError('Outside the original entry period; exits continue')
            a = validate_baseline(packet, quote, self.manifest, now)
            observed = a['dossier']['snapshot_at']
            decision['source_observed_at'] = observed
            decision['evidence_age_seconds'] = now - observed
            if name == 'crew':
                from .crew_review import validate_packet
                validate_packet(review, a['dossier'], a['baseline'], now=now,
                                producer_records=payload.get('producer_records', ()))
                if review['verdict'] != 'PASS': raise ValueError('Independent crew veto')
                if review['reviewed_at'] > now: raise ValueError('Crew review was unavailable at decision time')
                if review.get('crew_complete') is not True:
                    raise ValueError('Full coordinated crew incomplete; factual subset only')
            judgment = packet['judgment']
            guard = None
            if name == 'crew':
                guard = lambda: require_quantity_coverage(a['dossier'], payload.get('producer_records', ()), quote, book.cost_policy(), now)
            position = book.open_paper(quote, judgment, now, entry_guard=guard)
            decision.update(entry=True, status='PAPER_ENTRY', reason='Original Book paper entry', position_id=position['id'])
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # Only local stable reasons; no provider text is accepted here.
            decision['reason'] = str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__ + ': incomplete admission evidence'
        # Reference scenarios are explicitly not entries or executable liquidity.
        if isinstance(quote, dict):
            try:
                from .execution_costs import simulate
                validate_quote(quote, now)
                decision['basis_scenario'] = simulate(book.cost_policy(), 'buy', quote, config.TICKET_USD, now)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                pass
        book.db.execute('INSERT INTO crew_decisions VALUES(?,?,?,?)', (row['seq'], candidate, now, encode(decision)))
        # Count actual recorded requests even when admission was rejected. Jev token
        # estimates are not invoices; absence of a bill must not become zero cost.
        if isinstance(packet, dict) or isinstance(result.get('judgment'), dict):
            a = (packet or {}).get('assessment') or {}; j = (packet or {}).get('judgment') or result.get('judgment') or {}
            requests = []
            binding = j.get('binding') or {}
            if (j.get('request_id') and binding.get('account_id') == self.manifest['source_account_id']
                    and binding.get('settings_sha256') == self.manifest['config_sha256']):
                u = j.get('usage') or {}
                requests.append({'request_id': j['request_id'], 'scope': 'shared',
                    'billed_usd': u.get('billed_usd') if u.get('cost_verified') is True else None,
                    'estimated_usd': None, 'reference': 'recorded baseline model usage'})
            if a.get('attempted') == 1 and a.get('id'):
                requests.append({'request_id': 'jev:' + a['id'], 'scope': 'shared', 'billed_usd': None,
                    'estimated_usd': (a.get('result') or {}).get('estimated_api_usd'),
                    'reference': 'recorded Jev attempt; provider invoice unresolved'})
            for cost in requests:
                book.db.execute('INSERT OR IGNORE INTO crew_costs VALUES(?,?,?)', (cost['request_id'], now, encode(cost)))
        if (payload.get('ambiguous_model_attempt') is True
                and (payload.get('paid_authority') or {}).get('grok_enabled') is True):
            cost = {'request_id':'ambiguous-model:' + digest([self.manifest['source_account_id'],result.get('mint'),payload.get('source_finished_at')]),
                    'scope':'shared','billed_usd':None,'estimated_usd':None,'attempt_state':'AMBIGUOUS',
                    'reference':'Enabled model stage failed without a returned request/usage record; an attempt or charge is not proven or assumed free.'}
            book.db.execute('INSERT OR IGNORE INTO crew_costs VALUES(?,?,?)', (cost['request_id'],now,encode(cost)))

    def _watch(self, book, payload, now):
        quotes = {(q.get('mint'), q.get('pool')): q for q in payload.get('quotes', []) if isinstance(q, dict)}
        failed = {(q.get('mint'), q.get('pool')) for q in payload.get('failures', []) if isinstance(q, dict)}
        for p in book.positions():
            key = (p['mint'], p['pool'])
            q = quotes.get(key)
            if q is None:
                # A supplemental quote batch does not invalidate unrelated positions.
                if payload.get('supplemental') and key not in failed: continue
                p.update(feed_error=True, last_error_at=now); book._save(p); continue
            try:
                apply_quote(book, p['id'], q, now)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                p.update(feed_error=True, last_error_at=now); book._save(p)

    def process(self, limit=200):
        if type(limit) is not int or not 1 <= limit <= 1000: raise ValueError('Invalid process bound')
        counts = {}
        for name, book in self.portfolios().items():
            cursor = int(book.db.execute("SELECT value FROM metadata WHERE key='crew_cursor'").fetchone()[0])
            rows = self.db.execute('SELECT * FROM observations WHERE seq>? ORDER BY seq LIMIT ?', (cursor, limit)).fetchall()
            for row in rows:
                payload = json.loads(row['payload']); now = row['observed_at']; seq = row['seq']
                if seq != cursor + 1 or row['digest'] != digest([row['kind'], now, payload]):
                    raise ValueError('Comparison observation gap or changed payload')
                with book.transaction():
                    if row['kind'] == 'candidate': self._candidate(book, name, row, payload)
                    elif row['kind'] == 'watch': self._watch(book, payload, now)
                    elif row['kind'] == 'trial':
                        old = json.loads(book.db.execute("SELECT value FROM metadata WHERE key='crew_trial'").fetchone()[0])
                        new = payload['trial']
                        if old and old != new: raise ValueError('Original source deadline cannot change')
                        book.db.execute("UPDATE metadata SET value=? WHERE key='crew_trial'", (encode(new),))
                    elif row['kind'] == 'cost':
                        if payload.get('scope') not in ('shared', 'crew') or not payload.get('request_id'):
                            raise ValueError('Bound model/API cost attribution required')
                        if payload['scope'] == 'shared' or name == 'crew':
                            for key in ('billed_usd', 'estimated_usd'):
                                if payload.get(key) is not None: number(payload[key])
                            old = book.db.execute('SELECT payload FROM crew_costs WHERE request_id=?', (payload['request_id'],)).fetchone()
                            if old and json.loads(old[0]) != payload:
                                prior = json.loads(old[0])
                                if (prior.get('billed_usd') is not None or payload.get('billed_usd') is None
                                        or not payload.get('invoice_reference') or prior['scope'] != payload['scope']):
                                    raise ValueError('Cost request reused without bound invoice reconciliation')
                                book.db.execute('UPDATE crew_costs SET payload=? WHERE request_id=?', (encode(payload), payload['request_id']))
                            else:
                                book.db.execute('INSERT OR IGNORE INTO crew_costs VALUES(?,?,?)', (payload['request_id'], now, encode(payload)))
                    book.db.execute('INSERT INTO crew_inputs VALUES(?,?,?,?)', (seq, row['digest'], now, time.time()))
                    book.db.execute("UPDATE metadata SET value=? WHERE key='crew_cursor'", (str(seq),))
                    book.db.execute('INSERT INTO crew_equity VALUES(?,?,?)', (seq, now, encode(self._financial(book, now))))
                cursor = seq
            counts[name] = len(rows)
        return counts

    def process_pending(self, limit=50):
        return self.process(limit=limit)

    def _financial(self, book, now):
        costs = [json.loads(r[0]) for r in book.db.execute('SELECT payload FROM crew_costs')]
        known = all(x.get('billed_usd') is not None for x in costs)
        billed = sum((D(x['billed_usd']) for x in costs if x.get('billed_usd') is not None), D(0))
        baseline = self.manifest['capital_baseline']
        capital = baseline['starting_cash_usd'] if baseline else None
        out = paper_snapshot(book.fills(), book.positions(all_positions=True), now, str(billed), known, starting_cash=capital)
        out['known_cost_net_pnl'] = out['net_pnl']
        if not known: out['net_pnl'] = out['equity'] = out['return_pct'] = None
        out['period_known_cost_net_pnl'] = str(D(out['known_cost_net_pnl']) - D(self.manifest['seed_net_pnl']))
        out['period_net_pnl'] = out['period_known_cost_net_pnl'] if known else None
        out['model_api_requests'] = len(costs)
        out['ambiguous_model_api_attempts'] = sum(c.get('attempt_state') == 'AMBIGUOUS' for c in costs)
        out['estimated_model_api_usd'] = str(sum((D(x['estimated_usd']) for x in costs if x.get('estimated_usd') is not None), D(0)))
        out['capital_baseline_supported'] = capital is not None
        return out

    def needed_quotes(self, now, limit=2):
        """Bounded rotating union; never makes provider requests."""
        if type(limit) is not int or not 1 <= limit <= 8: raise ValueError('Invalid quote bound')
        needed = {}
        for book in self.portfolios().values():
            for p in book.positions():
                key = (p['mint'], p['pool'])
                needed[key] = max(needed.get(key, D(0)), D(p['remaining']))
            for row in book.db.execute('SELECT payload FROM crew_decisions ORDER BY seq DESC LIMIT 100'):
                d = json.loads(row[0]); basis = d.get('basis_scenario')
                if d['entry'] or not basis or not 0 <= now - d['at'] <= 86400: continue
                key = (d['mint'], d['pool'])
                needed[key] = max(needed.get(key, D(0)), D(basis['quantity']))
        keys = sorted(needed)
        old = self.db.execute("SELECT value FROM metadata WHERE key='quote_rotation'").fetchone()
        offset = int(old[0]) % max(1, len(keys)) if old else 0
        chosen = (keys[offset:] + keys[:offset])[:limit]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('quote_rotation',?)", (str(offset + len(chosen)),))
        return [{'mint': mint, 'pool': pool, 'quantity': str(needed[(mint, pool)])} for mint, pool in chosen]

    def candidate_targets(self, limit=2, now=None):
        return self.needed_quotes(int(time.time()) if now is None else now, limit=limit)

    def _later_outcomes(self, decisions, observations):
        out = []
        indexed = {}
        for row in observations:
            payload = json.loads(row['payload'])
            keys = {(item.get('mint'), item.get('pool'))
                    for item in payload.get('quotes', []) + payload.get('failures', []) if isinstance(item, dict)}
            for key in keys: indexed.setdefault(key, []).append((row, payload))
        for d in decisions:
            if d['entry']: continue
            basis = d.get('basis_scenario'); samples = []; unresolved = 0; seen = set()
            for row, payload in indexed.get((d['mint'], d['pool']), ()):
                if row['observed_at'] <= d['at']: continue
                unresolved += sum((f.get('mint'), f.get('pool')) == (d['mint'], d['pool'])
                                  for f in payload.get('failures', []) if isinstance(f, dict))
                for q in payload.get('quotes', []):
                    if not isinstance(q, dict): continue
                    if (q.get('mint'), q.get('pool')) != (d['mint'], d['pool']): continue
                    quote_id = digest(q)
                    if quote_id in seen: continue
                    seen.add(quote_id)
                    try:
                        validate_quote(q, row['observed_at'])
                        if q['observed_at'] <= d['at']: raise ValueError('Earlier quote')
                        price_return = str((D(q['price']) / D(d['quote']['price']) - 1) * 100)
                        sample = {'at': q['observed_at'], 'price_return_pct': price_return, 'net_hypothetical_pnl': None}
                        e = q.get('exit_evidence') or {}
                        if (basis and e.get('executable') is True and e.get('side') == 'sell'
                                and (e.get('mint'), e.get('pool')) == (d['mint'], d['pool'])
                                and e.get('observed_at') == q['observed_at'] and e.get('source_ref')
                                and D(e['input_quantity']) == D(basis['quantity'])):
                            proceeds = number(e['net_output_usd'])
                            sample['net_hypothetical_pnl'] = str(proceeds - D(basis['reference_notional_usd']) - D(basis['explicit_fee_usd']))
                        samples.append(sample)
                    except (ValueError, KeyError, TypeError, ArithmeticError): unresolved += 1
            out.append({'candidate_id': d['candidate_id'], 'rejected_at': d['at'], 'mint': d['mint'], 'pool': d['pool'],
                        'reason': d['reason'], 'samples': samples, 'invalid_or_missing_samples': unresolved,
                        'status': 'SAMPLED' if samples else 'UNKNOWN',
                        'scope': 'Recorded same-pool later observations only. Net values require the exact hypothetical quantity and net executable sell evidence; no alternative fill, continuous path or eventual winner is inferred.'})
        return out

    def report(self, now):
        high = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM observations').fetchone()[0]
        watches = self.db.execute("SELECT * FROM observations WHERE kind='watch' AND observed_at<=? ORDER BY seq", (now,)).fetchall()
        gaps = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM observations WHERE kind='gap' AND observed_at<=? ORDER BY seq", (now,))]
        variants = {}
        for name, book in self.portfolios().items():
            with book.transaction():
                cursor = int(book.db.execute("SELECT value FROM metadata WHERE key='crew_cursor'").fetchone()[0])
                trial = json.loads(book.db.execute("SELECT value FROM metadata WHERE key='crew_trial'").fetchone()[0])
                financial = self._financial(book, now)
                decisions = [json.loads(r[0]) for r in book.db.execute('SELECT payload FROM crew_decisions ORDER BY seq')]
                fills = [f for f in book.fills() if f['id'] not in self.manifest['seed_fill_ids']]
                history = [json.loads(r[0]) for r in book.db.execute('SELECT payload FROM crew_equity ORDER BY seq')]
                processed = dict(book.db.execute('SELECT seq,processed_at FROM crew_inputs'))
            peak = None; drawdown = D(0); drawdown_complete = True
            for item in history:
                if item['net_pnl'] is None or not item['marks_complete']:
                    drawdown_complete = False
                if item['net_pnl'] is None: continue
                value = D(item['net_pnl']); peak = value if peak is None else max(peak, value)
                drawdown = max(drawdown, peak - value)
            reasons = {}
            for d in decisions:
                if not d['entry']: reasons[d['reason']] = reasons.get(d['reason'], 0) + 1
            latencies = [d['latency_seconds'] for d in decisions if d['latency_seconds'] is not None]
            delays = [max(0, processed[r['seq']] - r['captured_at']) for r in self.db.execute('SELECT seq,captured_at FROM observations WHERE seq<=?', (cursor,)) if r['seq'] in processed]
            variants[name] = {**financial, 'account_id': book.identity['account_id'], 'cursor': cursor,
                'backlog': high - cursor, 'state': 'SETUP_BLOCKED' if trial is None else 'EXITS_ONLY' if now >= trial['ends_at'] else 'ACTIVE',
                'original_trial': trial, 'new_entries': sum(f['kind'] == 'buy' for f in fills), 'new_fills': len(fills),
                'rejections': reasons, 'decisions': len(decisions),
                'observed_max_drawdown_usd': str(drawdown) if drawdown_complete else None,
                'drawdown_complete': drawdown_complete,
                'complete_crew_decisions': sum(d['crew_complete'] for d in decisions),
                'latency_seconds': {'count': len(latencies), 'mean': sum(latencies)/len(latencies) if latencies else None, 'max': max(latencies, default=None)},
                'processing_delay_seconds': {'count':len(delays), 'mean':sum(delays)/len(delays) if delays else None, 'max':max(delays, default=None)},
                'fill_costs_usd': str(sum((D((f.get('execution_costs') or {}).get('embedded_friction_usd', '0')) + D(f['fee']) for f in fills), D(0))),
                'later_rejected_outcomes': self._later_outcomes(decisions, [r for r in watches if r['seq'] <= cursor])}
        return {'format': FORMAT, 'mode': 'ISOLATED_PAPER_COMPARISON', 'comparison_id': self.manifest['comparison_id'],
                'source_account_id': self.manifest['source_account_id'], 'as_of': now, 'excluded_from_strategy': True,
                'variants': variants, 'gaps': gaps, 'intake_scope': self.manifest['intake_scope'],
                'cost_scope': self.manifest['cost_scope'], 'automatic_promotion': False,
                'performance_scope': 'All recorded prospective decisions and portfolio fills, including open and unresolved positions. Seed fills excluded from new-fill counts. No invented bankroll/trial, prices, model bills or full-market coverage.'}

    def publish(self, now):
        result = self.report(now)
        atomic(self.folder / 'report.json', encode(result))
        def show(value):
            return html.escape('UNKNOWN' if value is None else str(value))
        rows = []
        for name, v in result['variants'].items():
            rows.append('<tr>' + ''.join('<td>' + show(x) + '</td>' for x in (
                name, v['state'], v['account_id'], v['decisions'], v['new_entries'], v['new_fills'],
                v['open_positions'], v['unresolved_positions'], v['period_net_pnl'], v['cash'], v['equity'],
                v['return_pct'], v['fill_costs_usd'], v['api_cost_usd'], v['api_costs_known'],
                v['latency_seconds']['mean'], v['cursor'], v['backlog'])) + '</tr>')
        detail = ''.join('<h2>' + show(name) + ' rejected observations</h2><pre>' +
                         html.escape(json.dumps(v['later_rejected_outcomes'], indent=2, ensure_ascii=False)) + '</pre>'
                         for name, v in result['variants'].items())
        heads = ('Portfolio','State','Account','Decisions','Entries','Fills','Open','Unresolved','Period net P&L',
                 'Cash','Equity','Return %','Fill costs','Known model/API bills','All bills known','Mean decision latency s','Cursor','Backlog')
        document = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>Xenon crew comparison</title><style>body{font:15px system-ui;margin:2rem;line-height:1.5;color:#18202b;background:#f5f6f8}'
            'table{border-collapse:collapse;background:white}td,th{padding:.6rem;text-align:left;border:1px solid #ccd1d9}'
            '.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:1rem}</style>'
            '<h1>Xenon baseline and full crew comparison</h1><p>Separate paper portfolios. No automatic promotion or live authority.</p>'
            '<p>As of ' + show(now) + '. Source account ' + show(result['source_account_id']) + '.</p><p>' +
            show(result['performance_scope']) + '</p><p>' + show(result['intake_scope']) + '</p><p>' + show(result['cost_scope']) +
            '</p><p>Full crew entries require all ten supported responsibilities to be complete. A factual review PASS alone is insufficient. '
            'Missing capital or an unstarted source trial remains unknown or blocked.</p><div class="scroll"><table><thead><tr>' +
            ''.join('<th>' + h + '</th>' for h in heads) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>' +
            '<h2>Recorded coverage gaps</h2><pre>' + html.escape(json.dumps(result['gaps'], indent=2, ensure_ascii=False)) + '</pre>' + detail + '</html>')
        atomic(self.folder / 'report.html', document)
        return result
