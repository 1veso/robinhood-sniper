"""Bounded prospective crew collection and isolated comparison coordination.

The existing Xenon book is read-only here. No model, strategy admission, alert,
signing or deployment interface is called. A completed job is not provider health.
"""
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time

from rug_desk.book import atomic, process_lock
from rug_desk.market import Market, validate_quote
from . import crew_comparison as comparisons
from .crew_evidence import EvidenceStore, Producer, enrich, encode, digest
from .crew_review import (persist_review, request_recheck, claim_due_rechecks,
                          finish_recheck, recover_rechecks, review, validate_packet)
from .jev_dossier import build
from .jev_review import Journal
from .adapters.solana import request_budget, validate_address

SCHEMA = 'sniper_crew_v1'
MODE = 'COMPARISON_ONLY'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _source(root):
    return Path(root) / 'rug_desk'


def _reader(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def _stream(source):
    with closing(_reader(source / 'runtime/book.sqlite3')) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()
        if row is None: raise ValueError('Existing source observation stream required')
        return row[0], db.execute('SELECT COALESCE(MAX(seq),0) FROM observation_events').fetchone()[0]


def settings(root):
    path = Path(root) / 'runtime/crew/settings.json'
    if not path.is_file(): return None
    value = _read(path)
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('mode') != MODE
            or type(value.get('enabled')) is not bool):
        raise ValueError('Invalid crew settings')
    return value


def initialize(root, *, authorization_reference, now=None):
    """Explicit opt-in only. Existing or interrupted comparisons cannot be reset."""
    root = Path(root).resolve(); now = int(time.time()) if now is None else now
    if not isinstance(authorization_reference, str) or not authorization_reference.strip():
        raise ValueError('Explicit comparison authorization reference required')
    folder = root / 'runtime/crew'
    with process_lock(folder / 'worker.lock'):
        if (folder / 'settings.json').exists() or (folder / 'coordinator.sqlite3').exists():
            raise ValueError('Existing coordinator cannot be reset')
        identity = comparisons.source_identity(_source(root))
        manifest = comparisons.initialize(root / 'runtime/crew-comparison', _source(root), now=now)
        stream, current_highwater = _stream(_source(root))
        if stream != manifest['source_stream_id']: raise ValueError('Source stream changed during initialization')
        highwater = manifest['source_highwater']
        value = {'schema': SCHEMA, 'mode': MODE, 'enabled': True,
                 'source_account_id': identity['account_id'], 'source_config_sha256': identity['config_sha256'],
                 'source_stream_id': stream, 'source_highwater': highwater,
                 'comparison_id': manifest['comparison_id'], 'initialized_at': now,
                 'authorization_reference': authorization_reference.strip(),
                 'execution_authority': False, 'paid_authorization_changed': False}
        db = sqlite3.connect(folder / 'coordinator.sqlite3', timeout=.25)
        try:
            db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE imported(seq INTEGER PRIMARY KEY,event_digest TEXT NOT NULL,
                    tape_seq INTEGER,kind TEXT NOT NULL,reason TEXT,completed_at INTEGER NOT NULL);
                CREATE TABLE targets(mint TEXT NOT NULL,pool TEXT NOT NULL,first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,last_collected INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,
                    PRIMARY KEY(mint,pool));
                CREATE TABLE runs(id INTEGER PRIMARY KEY,started INTEGER NOT NULL,finished INTEGER,
                    status TEXT NOT NULL,payload TEXT NOT NULL);''')
            with db:
                db.executemany('INSERT INTO metadata VALUES(?,?)', [('binding', encode(value)), ('cursor', str(highwater))])
        finally: db.close()
        if current_highwater > highwater:
            with closing(comparisons.Comparison(root / 'runtime/crew-comparison',source_account_id=identity['account_id'])) as comparison:
                comparison.capture('initialization-coverage','gap',now,
                    {'reason':'source_advanced_while_comparison_initialized','after_seed_seq':highwater,
                     'before_opt_in_seq':current_highwater,'scope':'No missing source fills or inputs reconstructed'},
                    captured_at=max(now,time.time()))
        # Publish opt-in last. No source inputs are admitted before this exists.
        atomic(folder / 'settings.json', encode(value))
        return value


class Coordinator:
    def __init__(self, root, value, *, clock=time.time, monotonic=time.monotonic):
        self.root = Path(root).resolve(); self.value = value
        self.clock, self.monotonic = clock, monotonic
        self.identity = comparisons.source_identity(_source(self.root))
        if (self.identity['account_id'] != value['source_account_id']
                or self.identity['config_sha256'] != value['source_config_sha256']):
            raise ValueError('Crew source binding changed')
        stream, _ = _stream(_source(self.root))
        if stream != value['source_stream_id']: raise ValueError('Crew stream changed')
        self.db = sqlite3.connect((self.root / 'runtime/crew/coordinator.sqlite3').as_uri() + '?mode=rw', uri=True, timeout=.25)
        self.db.row_factory = sqlite3.Row
        saved = json.loads(self.db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0])
        # Disabling is reversible, but cannot change any identity or highwater.
        if {k:v for k,v in saved.items() if k != 'enabled'} != {k:v for k,v in value.items() if k != 'enabled'}:
            self.db.close(); raise ValueError('Crew coordinator binding changed')
        self.comparison = comparisons.Comparison(self.root / 'runtime/crew-comparison', source_account_id=self.identity['account_id'])
        if self.comparison.manifest['comparison_id'] != value['comparison_id']:
            self.close(); raise ValueError('Comparison identity changed')

    def close(self):
        self.comparison.close(); self.db.close()

    def cursor(self):
        return int(self.db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])

    def _gap(self, key, reason, source_event=None):
        payload = {'reason': reason, 'source_event': source_event,
                   'scope': 'Missing or unusable prospective input; no reconstructed fill'}
        old = self.comparison.db.execute('SELECT * FROM observations WHERE event_key=?',(key,)).fetchone()
        if old is not None:
            prior = json.loads(old['payload'])
            if any(prior.get(k)!=v for k,v in payload.items()):raise ValueError('Gap source changed on replay')
            return self.comparison.capture(key,'gap',old['observed_at'],prior,captured_at=old['captured_at'])
        last = self.comparison.db.execute('SELECT MAX(observed_at) FROM observations').fetchone()[0]
        source_at = (source_event or {}).get('observed_at')
        if type(source_at) is not int or not 0 <= source_at <= int(self.clock()):source_at=0
        at = max(source_at, last or 0, self.comparison.manifest['started_at'])
        payload['detected_at']=int(self.clock())
        return self.comparison.capture(key,'gap',at,payload,captured_at=max(self.clock(),at))

    def _target(self, result, at):
        if not isinstance(result, dict): return
        quote = result.get('refreshed_pool') or result.get('pool')
        if not isinstance(quote, dict): return
        try:
            mint, pool = validate_address(result['mint']), validate_address(quote['pool'])
            if quote.get('mint') != mint: return
        except (KeyError, ValueError, TypeError): return
        self.db.execute('''INSERT INTO targets(mint,pool,first_seen,last_seen,payload) VALUES(?,?,?,?,?)
            ON CONFLICT(mint,pool) DO UPDATE SET last_seen=excluded.last_seen,payload=excluded.payload''',
            (mint,pool,at,at,encode(result)))
        self.db.execute('DELETE FROM targets WHERE rowid NOT IN (SELECT rowid FROM targets ORDER BY last_seen DESC,rowid DESC LIMIT 8)')

    def import_inputs(self, journal, *, deadline, limit=32):
        if type(limit) is not int or not 1 <= limit <= 32: raise ValueError('Bounded intake required')
        cursor = self.cursor(); source = _source(self.root) / 'runtime/book.sqlite3'
        with closing(_reader(source)) as source_db:
            source_db.set_progress_handler(lambda: int(self.monotonic() >= deadline), 1000)
            stream = source_db.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()[0]
            if stream != self.value['source_stream_id']: raise ValueError('Source stream changed')
            highwater = source_db.execute('SELECT COALESCE(MAX(seq),0) FROM observation_events').fetchone()[0]
            if highwater < cursor:
                raise ValueError('Source sequence regressed')
            rows = source_db.execute('SELECT * FROM observation_events WHERE seq>? ORDER BY seq LIMIT ?', (cursor,limit)).fetchall()
        imported = gaps = 0
        for row in rows:
            if self.monotonic() >= deadline: break
            row = dict(row); seq = row['seq']; key = stream + ':' + str(seq)
            # observation_events uses AUTOINCREMENT plus INSERT OR IGNORE.
            # Deduplicated emits can consume IDs without adding rows: numeric
            # holes alone are not evidence of missing source observations.
            # Stream binding, highwater regression and explicit capture gaps
            # remain authoritative; old recorded gaps are never rewritten.
            kind, reason, tape_seq = row['kind'], None, None
            mapped = {'crew_candidate_input':'candidate','crew_market_input':'watch','crew_capture_gap':'gap'}.get(kind)
            if mapped:
                try:
                    payload = json.loads(row['payload'])
                    if mapped != 'gap' and (payload.get('schema') != 'crew_source_input_v1'
                            or payload.get('source_account_id') != self.identity['account_id']
                            or payload.get('source_config_sha256') != self.identity['config_sha256']
                            or payload.get('comparison_id') != self.value['comparison_id']
                            or not isinstance(payload.get('input'), dict)):
                        raise ValueError('Input binding invalid')
                    at, captured = row['observed_at'], row['recorded_at']
                    if type(at) is not int or type(captured) is not int or not self.value['initialized_at'] <= at <= captured <= int(self.clock()):
                        raise ValueError('Input time invalid')
                    data = payload if mapped == 'gap' else payload['input']
                    existing = self.comparison.db.execute('SELECT seq FROM observations WHERE event_key=?', (key,)).fetchone()
                    last = self.comparison.db.execute('SELECT MAX(observed_at) FROM observations').fetchone()[0]
                    if existing is None and last is not None and at < last:
                        reason = 'late_source_input'
                    elif mapped == 'gap': reason = 'source_capture_gap'
                    else:
                        if mapped == 'candidate':
                            packet = data.get('crew_packet')
                            if packet is not None:
                                assessment = (data.get('baseline_packet') or {}).get('assessment')
                                dossier = assessment['dossier'] if assessment else data['dossier']
                                baseline = assessment['baseline'] if assessment else data['baseline']
                                result = data.get('result')
                                if not isinstance(result,dict): raise ValueError('Candidate result must be a mapping')
                                facts = result.get('facts')
                                if facts is not None and not isinstance(facts,dict):
                                    raise ValueError('Candidate facts must be a mapping')
                                for field in ('refreshed_pool','pool'):
                                    if result.get(field) is not None and not isinstance(result[field],dict):
                                        raise ValueError('Candidate quote must be a mapping')
                                quote = result.get('refreshed_pool') or result.get('pool')
                                # A pool-stage rejection can retain its screened
                                # pool in facts before any quote was returned.
                                # Preserve that rejected candidate, without
                                # manufacturing a quote or admission authority.
                                pool = (facts or {}).get('pool') if quote is None else quote.get('pool')
                                if (dossier.get('account_id') != self.identity['account_id']
                                        or dossier.get('mint') != result.get('mint')
                                        or dossier.get('pool') != pool):
                                    raise ValueError('Research packet target binding invalid')
                                validate_packet(packet, dossier, baseline, now=at,
                                                producer_records=data.get('producer_records', ()))
                                persist_review(journal.db, packet)
                                request_recheck(journal.db, packet['packet_id'], now=int(self.clock()))
                            tape = data
                        else:
                            validate_quote(data, at)
                            tape = {'quotes':[data], 'failures':[], 'supplemental':True, 'source_event':key}
                        tape_seq = self.comparison.capture(key, mapped, at, tape, captured_at=captured)
                except (ValueError, KeyError, TypeError, ArithmeticError, json.JSONDecodeError):
                    reason = 'invalid_source_input'
                if reason:
                    tape_seq = self._gap('gap:' + key, reason,
                        {'id':key,'kind':kind,'observed_at':row['observed_at'],'recorded_at':row['recorded_at'],'payload_sha256':digest(row['payload'])})
                    gaps += 1
            with self.db:
                if mapped == 'candidate' and reason is None: self._target(data.get('result'), row['observed_at'])
                self.db.execute('INSERT INTO imported VALUES(?,?,?,?,?,?)',
                    (seq,digest(row),tape_seq,kind,reason,int(self.clock())))
                self.db.execute("UPDATE metadata SET value=? WHERE key='cursor'", (str(seq),))
            cursor = seq; imported += 1
        return {'imported':imported,'gaps':gaps,'cursor':cursor,'source_snapshot_highwater':highwater,'source_backlog':highwater-cursor}

    def target(self, candidate=None):
        rows = self.db.execute('SELECT * FROM targets WHERE last_seen>=? ORDER BY last_collected,first_seen,mint,pool',
                               (int(self.clock())-86400,)).fetchall()
        if candidate:
            for row in rows:
                expected = 'rug_desk:' + self.identity['account_id'] + ':' + row['mint'] + ':' + row['pool']
                if expected == candidate: return dict(row)
            return None
        return dict(rows[0]) if rows else None

    def recheck(self, journal, job, target, store, quote=None):
        now = int(self.clock())
        old = json.loads(journal.db.execute('SELECT payload FROM crew_packets WHERE packet_id=?', (job['packet_id'],)).fetchone()[0])
        if target is None: return {'state':'UNAVAILABLE','request_id':job['request_id'],'reason':'exact_pinned_target_missing'}
        raw = json.loads(target['payload']); raw.pop('judgment',None); raw.pop('refreshed_pool',None)
        raw.update(stage='crew_research',entry=False)
        if quote is not None: raw['pool'] = quote
        row = {'mint':target['mint'],'at':now,'stage':'crew_research','reason':'bounded_evidence_recheck','payload':encode(raw)}
        d, b = build('rug_desk',self.identity,row)
        if d['candidate_id'] != old['candidate_id']: raise ValueError('Recheck target changed')
        facts = raw.get('facts') or {}; facts_at = facts.get('observed_at')
        if type(facts_at) is int and 0 <= facts_at <= now:
            reference = 'rug_desk:crew-recheck-facts:' + digest(facts)
            d['source_refs'][reference] = {'provider':'recorded_provider_report','reference':reference,
                'observed_at':facts_at,'payload_sha256':digest(facts)}
            d['provider_checks_source_ref'] = reference
            for name in ('reported_top10_pct','reported_dev_pct','risk_score'):
                if d['observations'].get(name):d['observations'][name]['source_ref']=reference
        else:
            d['provider_checks'] = {k:None for k in d['provider_checks']}
            d['limitations'].append('Original provider safety observation time is unknown; no fresh safety check is asserted.')
        b.update(prospective=False,eligible=False,verdict='SKIP',analyst_verdict=None)
        d = enrich(d,store,now=now)
        d['limitations'].append('Research recheck only; original provider facts retain original observation times and no prior model judgment is reused.')
        packet = review(d,b,now=now,producer_records=store.records_for(d))
        persist_review(journal.db,packet); finish_recheck(journal.db,job['request_id'],packet['packet_id'],now=now)
        return {'state':'COMPLETE','request_id':job['request_id'],'packet_id':packet['packet_id'],
                'owners':job['owners'],'entry_authority':False}


def run(root, *, deadline=None, clock=time.time, monotonic=time.monotonic, producer_factory=Producer, market=None):
    root = Path(root).resolve(); value = settings(root)
    if value is None or not value['enabled']: return {'status':'DISABLED','mode':MODE,'orders_created':0}
    started = int(clock()); start_mono = monotonic()
    if deadline is None: deadline = start_mono + 15
    if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline): raise ValueError('Finite deadline required')
    deadline = min(deadline,start_mono+15)
    if deadline <= start_mono: return {'status':'DEFERRED','reason':'worker_budget','orders_created':0}
    stats = {'mode':MODE,'started_at':started,'errors':[],'provider_status':{},'orders_created':0,
             'signing_calls':0,'broadcast_calls':0,'paid_calls':0,'execution_authority':False}
    folder = root / 'runtime/crew'
    with process_lock(folder/'worker.lock'), closing(Coordinator(root,value,clock=clock,monotonic=monotonic)) as c:
        with c.db:
            c.db.execute("UPDATE runs SET finished=?,status='INTERRUPTED' WHERE status='RUNNING'", (started,))
            run_id = c.db.execute("INSERT INTO runs(started,status,payload) VALUES(?,'RUNNING','{}')", (started,)).lastrowid
        stats['run_id'] = run_id
        try:
            with closing(Journal(root)) as journal, closing(EvidenceStore(root)) as store:
                journal.bind('rug_desk',c.identity['account_id'])
                recover_rechecks(journal.db,now=int(clock()))
                stats['intake'] = c.import_inputs(journal,deadline=deadline-1)
                jobs = claim_due_rechecks(journal.db,now=int(clock()),limit=1) if monotonic()<deadline-2 else []
                candidate = None
                if jobs:
                    old = journal.db.execute('SELECT candidate_id FROM crew_packets WHERE packet_id=?',(jobs[0]['packet_id'],)).fetchone()
                    candidate = old[0] if old else None
                target = c.target(candidate); quotes = {}
                base_market = market or Market(clock=clock,costs=True)
                class RecordedMarket:
                    def quote(self, mint, pool):
                        q = base_market.quote(mint,pool); quotes[(mint,pool)] = q; return q
                if target and monotonic()<deadline-3:
                    seconds = min(10,deadline-monotonic()-2)
                    try:
                        result = producer_factory(root,store=store,market=RecordedMarket(),clock=clock,budget_seconds=seconds,max_rpc_calls=20).capture(
                            c.identity,target['mint'],target['pool'],now=int(clock()))
                        p = result['payload']
                        stats['provider_status'] = {'record_id':result['id'],'errors':p['errors'],
                            'holder_coverage':p['holder_coverage']['status'],'venue':p['venue']['status'],'exit':p['exit']['status'],
                            'received_at':p['received_at'],'rpc_calls':p['rpc_calls']}
                    except Exception as exc: stats['errors'].append({'stage':'evidence','type':type(exc).__name__})
                    finally:
                        with c.db: c.db.execute('UPDATE targets SET last_collected=? WHERE mint=? AND pool=?',(int(clock()),target['mint'],target['pool']))
                if jobs:
                    stats['recheck'] = c.recheck(journal,jobs[0],c.target(candidate),store,quotes.get((target['mint'],target['pool'])) if target else None)
                if monotonic()<deadline-1:
                    # All source intake precedes optional current quotes and trial observations.
                    if stats['intake']['source_backlog']==0:c.comparison.refresh_trial(int(clock()))
                    stats['processed'] = c.comparison.process_pending(limit=50)
                if stats['intake']['source_backlog']==0 and monotonic()<deadline-2:
                    requests = c.comparison.needed_quotes(int(clock()),limit=1)
                    if requests:
                        item=requests[0]; q=None
                        try:
                            with request_budget(clock()+max(.1,deadline-monotonic()-1),clock=clock):
                                q = RecordedMarket().quote(item['mint'],item['pool'])
                                validate_quote(q,int(clock()))
                            data={'quotes':[q],'failures':[],'supplemental':True,'requested_quantity':item['quantity']}
                        except Exception as exc:
                            data={'quotes':[],'failures':[{'mint':item['mint'],'pool':item['pool'],'type':type(exc).__name__}],'supplemental':True}
                        at=int(clock())
                        c.comparison.capture('supplemental:'+str(run_id),'watch',at,data,captured_at=clock())
                        stats['supplemental_quote']={'mint':item['mint'],'pool':item['pool'],'ok':q is not None and not data['failures']}
                if monotonic()<deadline:
                    report = c.comparison.publish(int(clock()))
                    keep = ('account_id','state','cursor','backlog','new_fills','new_entries','decision_count',
                            'net_pnl','period_net_pnl','period_known_cost_net_pnl','marks_complete','model_api_requests')
                    stats['comparison'] = {'comparison_id':report['comparison_id'],'as_of':report['as_of'],
                        'report_path':'runtime/crew-comparison/report.json','gap_count':len(report['gaps']),
                        'variants':{name:{k:v[k] for k in keep if k in v} for name,v in report['variants'].items()}}
                else: stats['publication']='DEFERRED_BUDGET'
        except Exception as exc:
            stats['errors'].append({'stage':'coordinator','type':type(exc).__name__})
        finally:
            stats['finished_at']=int(clock()); stats['duration_seconds']=max(0,monotonic()-start_mono)
            stats['cursor']=c.cursor(); stats['status']='ERROR' if stats['errors'] else 'OK'
            with c.db: c.db.execute('UPDATE runs SET finished=?,status=?,payload=? WHERE id=?',
                (stats['finished_at'],stats['status'],encode(stats),run_id))
            atomic(folder/'status.json',encode(stats))
    return stats


def status(root):
    root=Path(root).resolve(); value=settings(root)
    if value is None:return {'status':'NOT_INITIALIZED','mode':MODE,'execution_authority':False}
    path=root/'runtime/crew/status.json'
    out=_read(path) if path.is_file() else {'status':'INITIALIZED_NO_COMPLETION'}
    return {'settings':value,**out}
