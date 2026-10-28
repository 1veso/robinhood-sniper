"""Isolated shadow journal. Reads existing books; cannot write orders or fills."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

from .jev import POLICY, MODEL, JevClient, gate, validate_response
from .jev_dossier import build, validate, digest, encode
from .advisor import DailyBudget

DEFAULT_SETTINGS = {'enabled': True, 'mode': 'SHADOW', 'paid_authorized': False, 'daily_budget_usd': '0'}


class BudgetBlocked(Exception):
    pass


def reader(path):
    c = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')
    return c


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.folder = self.root/'runtime/jev-review'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder/'assessments.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS assessments(
                id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,account_id TEXT NOT NULL,
                snapshot_at INTEGER NOT NULL,started INTEGER NOT NULL,finished INTEGER,
                status TEXT NOT NULL,dossier TEXT NOT NULL,baseline TEXT NOT NULL,
                result TEXT NOT NULL,attempted INTEGER NOT NULL DEFAULT 0,watch_count INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS assessments_candidate ON assessments(candidate_id,started);
            CREATE TABLE IF NOT EXISTS source_bindings(source TEXT PRIMARY KEY,account_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started INTEGER NOT NULL,finished INTEGER,status TEXT NOT NULL,detail TEXT NOT NULL);
        ''')
        settings = self.folder/'settings.json'
        if not settings.exists():
            # Exclusive creation never overwrites an operator's disabled state.
            try:
                with settings.open('x', encoding='utf-8') as stream: stream.write(encode(DEFAULT_SETTINGS))
            except FileExistsError:
                pass

    def close(self): self.db.close()

    def bind(self, source, account):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO source_bindings VALUES(?,?)', (source, account))
            if self.db.execute('SELECT account_id FROM source_bindings WHERE source=?', (source,)).fetchone()[0] != account:
                raise ValueError('Source account identity changed')

    def recover(self, now):
        with self.db:
            self.db.execute("UPDATE assessments SET status='INTERRUPTED',finished=?,result=? WHERE status='IN_FLIGHT' AND started<?",
                (now, encode({'proposed': 'PASS', 'reason': 'ambiguous_request_not_retried', 'policy': POLICY}), now-60))

    def get(self, identifier):
        row = self.db.execute('SELECT * FROM assessments WHERE id=?', (identifier,)).fetchone()
        if row is None: return None
        out = dict(row)
        for k in ('dossier','baseline','result'): out[k] = json.loads(out[k])
        return out

    def assess(self, dossier, baseline, *, now, client=None, reserve=None):
        identifier = digest({'dossier': dossier, 'policy': POLICY})
        candidate = dossier.get('candidate_id', 'invalid:' + identifier)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            existing = self.get(identifier)
            if existing:
                self.db.rollback(); return existing
            if (not isinstance(baseline, dict) or type(baseline.get('eligible')) is not bool
                or baseline.get('verdict') not in ('BUY','SKIP')
                or baseline.get('account_id') != dossier.get('account_id')
                or baseline.get('candidate_id') != candidate):
                raise ValueError('Invalid baseline binding')
            prior = self.db.execute('SELECT * FROM assessments WHERE candidate_id=? AND attempted=1 ORDER BY started DESC LIMIT 1', (candidate,)).fetchone()
            count, status, reason = 0, 'IN_FLIGHT', None
            try:
                evidence = validate(dossier, now)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                evidence = {'missing_required': [], 'computed': {}}
                status, reason = 'INVALID_DATA', 'dossier_validation_failed'
            if status == 'IN_FLIGHT' and not baseline['eligible']:
                status, reason = 'HARD_REJECTION', 'baseline_entry_checks_not_passed'
            if status == 'IN_FLIGHT' and evidence['missing_required']:
                status, reason = 'INSUFFICIENT_EVIDENCE', 'required_observations_missing_or_stale'
            if status == 'IN_FLIGHT' and prior:
                previous = json.loads(prior['result']); old = json.loads(prior['dossier'])
                if (previous.get('proposed') != 'WATCH' or prior['watch_count'] >= 1
                    or prior['finished'] is None or now < previous['recheck_at']
                    or dossier['snapshot_at'] < previous['recheck_at']
                    or (dossier['window']['end'] or 0) <= (old['window']['end'] or 0)):
                    status, reason = 'REFRESH_BLOCKED', 'one_refresh_requires_due_time_and_fresh_evidence'
                else:
                    count = 1
            if status == 'IN_FLIGHT' and client is None:
                status, reason = 'DISABLED', 'external_calls_disabled_or_credential_unavailable'
            state = {**dossier, **evidence}
            result = {'policy': POLICY, 'proposed': 'PASS', 'reason': reason,
                'evidence': evidence, 'filter_would_admit': False,
                'probability_meaning': 'Probability of the action label, never probability of profit',
                'comparison_disagreement': None, 'request_state': state}
            self.db.execute('INSERT INTO assessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,candidate,baseline['account_id'],dossier.get('snapshot_at',0),now,
                 None if status=='IN_FLIGHT' else now,status,encode(dossier),encode(baseline),encode(result),
                 int(status=='IN_FLIGHT'),count))
            self.db.commit()
        except Exception:
            self.db.rollback(); raise
        if status != 'IN_FLIGHT': return self.get(identifier)
        try:
            if reserve is not None:
                try: reserve(identifier, now)
                except Exception as exc: raise BudgetBlocked() from exc
            raw = client.ask(state)
            response = validate_response(raw)
            proposed, reason = gate(response, watch_count=count)
            result.update(response=response, proposed=proposed, reason=reason,
                filter_would_admit=proposed=='BUY' and baseline['eligible'] and baseline['verdict']=='BUY',
                comparison_disagreement=(baseline['verdict']=='BUY') != (proposed=='BUY'),
                estimated_api_usd=str(Decimal(response['usage']['input_tokens'])*Decimal(POLICY['price_per_million_input_usd'])/Decimal(1000000)),
                api_cost_scope='Documented token-rate estimate, not a vendor invoice; reserved spending remains retained')
            if proposed == 'WATCH': result['recheck_at'] = now + POLICY['watch_delay_seconds']
            status = 'COMPLETE'
        except BudgetBlocked:
            status, reason = 'BUDGET_BLOCKED', 'reservation_failed_no_provider_call'
        except TimeoutError:
            status, reason = 'TIMEOUT', 'request_outcome_unknown_no_automatic_retry'
        except urllib.error.HTTPError as exc:
            status, reason = 'API_ERROR', 'http_' + str(exc.code)
        except urllib.error.URLError as exc:
            status = 'TIMEOUT' if isinstance(exc.reason, TimeoutError) else 'API_ERROR'
            reason = 'transport_failed_no_automatic_retry'
        except (ValueError, KeyError, TypeError, ArithmeticError):
            status, reason = 'MALFORMED_RESPONSE', 'typed_contract_failed'
        except Exception:
            status, reason = 'API_ERROR', 'request_failed_no_automatic_retry'
        if status != 'COMPLETE': result.update(proposed='PASS', reason=reason)
        # Response cannot choose clocks, watch_count, identity, settings or orders.
        with self.db:
            self.db.execute('UPDATE assessments SET finished=?,status=?,result=? WHERE id=?',
                (int(time.time()),status,encode(result),identifier))
        return self.get(identifier)


def sources(root, *, include_checks=True):
    root = Path(root)
    for source, folder in (('rug_desk',root/'rug_desk/runtime'),('neon',root/'neon_bot/runtime')):
        path = folder/'book.sqlite3'
        if not path.exists(): continue
        with closing(reader(path)) as db:
            identity = json.loads(db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
            marker = json.loads((folder/'account.json').read_text())
            if identity != marker or identity['mode'] != 'paper': raise ValueError('Paper source identity mismatch')
            checks = [dict(r) for r in db.execute('SELECT mint,at,stage,verdict,reason,payload FROM checks ORDER BY at,mint')] if include_checks else []
            positions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM positions')]
        yield source, identity, checks, positions


def settings(root):
    path = Path(root)/'runtime/jev-review/settings.json'
    out = json.loads(path.read_text()) if path.exists() else DEFAULT_SETTINGS.copy()
    if (set(out) != set(DEFAULT_SETTINGS) or out.get('mode') != 'SHADOW'
        or type(out.get('enabled')) is not bool or type(out.get('paid_authorized')) is not bool):
        raise ValueError('Invalid shadow settings')
    limit = Decimal(out['daily_budget_usd'])
    if not limit.is_finite() or limit < 0: raise ValueError('Invalid review budget')
    return out


def run(root, *, now=None):
    root = Path(root); now = int(time.time()) if now is None else now
    from rug_desk.book import process_lock, atomic
    folder = root/'runtime/jev-review'; folder.mkdir(parents=True, exist_ok=True)
    with process_lock(folder/'worker.lock'), closing(Journal(root)) as journal:
        journal.recover(now)
        with journal.db:
            run_id = journal.db.execute('INSERT INTO runs(started,status,detail) VALUES(?,?,?)', (now,'RUNNING','{}')).lastrowid
        config = DEFAULT_SETTINGS.copy()
        budget, client = None, None
        stats = {'mode':'SHADOW','enabled':config['enabled'],'external_calls_enabled':False,'sources':{},'errors':[], 'orders_created':0}
        try:
            config = settings(root)
            stats['enabled'] = config['enabled']
            if config['enabled'] and config['paid_authorized'] and Decimal(config['daily_budget_usd']) > 0:
                key_file = root/'runtime/typesafe-api-key.txt'
                if key_file.exists():
                    client = JevClient(key_file.read_text(encoding='utf-8-sig').strip())
                    budget = DailyBudget(folder/'budget.sqlite3', Decimal(config['daily_budget_usd']))
                    stats['external_calls_enabled'] = True
            if config['enabled']:
                called = False
                for source, identity, checks, positions in sources(root):
                    journal.bind(source, identity['account_id'])
                    stats['sources'][source] = {'account_id':identity['account_id'],'checks_seen':len(checks)}
                    for row in checks:
                        try:
                            dossier, baseline = build(source, identity, row)
                            if journal.get(digest({'dossier':dossier,'policy':POLICY})):
                                continue
                            assessment = journal.assess(dossier, baseline, now=now, client=client,
                                reserve=(lambda key, at: budget.reserve(key, Decimal('.01'), at)) if budget else None)
                            if assessment['attempted']:
                                called = True
                                break  # Leave other snapshots queued for a later bounded pass.
                        except Exception:
                            # Never print provider exception strings or raw input payloads.
                            stats['errors'].append({'source':source,'candidate_mint':row['mint'],'status':'INVALID_DATA'})
                    if called: break
        except Exception as exc:
            stats['errors'].append({'status':'SOURCE_OR_SETTINGS_ERROR','type':type(exc).__name__})
        finally:
            if budget: budget.close()
            stats['finished_at'] = int(time.time())
            with journal.db:
                journal.db.execute('UPDATE runs SET finished=?,status=?,detail=? WHERE id=?',
                    (stats['finished_at'],'ERROR' if stats['errors'] else 'OK',encode(stats),run_id))
            atomic(folder/'status.json', encode(stats))
        return stats


def comparison(assessments, positions):
    """Matched-fill subset replay, no reinvestment and no hypothetical ledger writes."""
    records, exclusions = [], {'unassessed':0,'late_assessment':0,'open_position':0}
    for source, account, p in positions:
        matches = [a for a in assessments if a['account_id']==account and a['baseline'].get('position_id')==p['id'] and a['status']=='COMPLETE']
        if not matches: exclusions['unassessed']+=1; continue
        a = min(matches, key=lambda x:x['finished'])
        if a['finished'] > p['opened_at']:
            exclusions['late_assessment']+=1; continue
        if not p.get('closed_at'): exclusions['open_position']+=1; continue
        net = Decimal(p['realized_pnl'])
        if not net.is_finite(): continue
        keep = a['result']['filter_would_admit'] and a['baseline']['verdict']=='BUY'
        records.append({'account_id':account,'position_id':p['id'],'assessment_id':a['id'],
            'baseline_net_pnl':str(net),'filtered_net_pnl':str(net if keep else Decimal(0)),
            'retained':keep,'entry_at':p['opened_at'],'closed_at':p['closed_at']})
    by_account = {}
    for r in records:
        v = by_account.setdefault(r['account_id'], {'matched_closed':0,'baseline_net_pnl':Decimal(0),'filtered_net_pnl':Decimal(0)})
        v['matched_closed']+=1
        for k in ('baseline_net_pnl','filtered_net_pnl'): v[k]+=Decimal(r[k])
    for v in by_account.values():
        for k in ('baseline_net_pnl','filtered_net_pnl'): v[k]=str(v[k])
    return {'mode':'HYPOTHETICAL_MATCHED_FILL_SUBSET','by_account':by_account,'records':records,
        'exclusions':exclusions,'performance_available':bool(records),
        'method':'Same recorded candidates, entry times, tickets, costs and exits. Filter only removes baseline trades; freed capital is not redeployed. Account results remain separate. Post-entry assessments excluded to avoid look-ahead.',
        'limitations':'This is a subset comparison, not a re-optimized portfolio simulation. Model costs are separate estimates. No 24h return is invented for skipped candidates.'}


def view(root):
    root=Path(root); folder=root/'runtime/jev-review'; items=[]; bindings=[]; runs=[]; completed=[]
    if (folder/'assessments.sqlite3').exists():
        with closing(reader(folder/'assessments.sqlite3')) as db:
            for row in db.execute('SELECT * FROM assessments ORDER BY snapshot_at DESC,started DESC,id LIMIT 200'):
                item=dict(row)
                for k in ('dossier','baseline','result'): item[k]=json.loads(item[k])
                items.append(item)
            bindings=[dict(r) for r in db.execute('SELECT * FROM source_bindings')]
            runs=[dict(r) for r in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 10')]
            counts=dict(db.execute('SELECT status,count(*) FROM assessments GROUP BY status'))
            # Evaluation uses the entire completed cohort, not the board's display limit.
            for row in db.execute("SELECT * FROM assessments WHERE status='COMPLETE'"):
                item=dict(row)
                for k in ('dossier','baseline','result'): item[k]=json.loads(item[k])
                completed.append(item)
    else: counts={}
    positions=[]
    for source, identity, checks, ps in sources(root, include_checks=False):
        positions.extend((source,identity['account_id'],p) for p in ps)
    try: config=settings(root)
    except Exception: config={**DEFAULT_SETTINGS,'enabled':False}
    paired=comparison(completed,positions)
    summaries=[]
    for item in items:
        summaries.append({k:item[k] for k in ('id','candidate_id','account_id','snapshot_at','started','finished','status','baseline','watch_count')} |
            {'result':{k:v for k,v in item['result'].items() if k!='request_state'}})
    return {'name':'Pump / Jev Review','mode':'SHADOW','assets':[],'cadence_seconds':60,'account_id':'Linked baseline accounts; no fourth balance',
        'enabled':config['enabled'],'external_calls_enabled':config['enabled'] and config['paid_authorized'] and Decimal(config['daily_budget_usd'])>0 and (root/'runtime/typesafe-api-key.txt').is_file(),
        'model':MODEL,'policy':POLICY,'assessments':summaries,'counts':counts,'source_bindings':bindings,
        'runs':runs,'comparison':paired,'orders_created':0,
        'pump_feed':{'connected':False,'coverage':'Existing Solana candidate journals only; no launch-feed coverage',
            'dependency':'PumpPortal documents creation/migration streams; token/account trade subscriptions require a key and funded paid data access. Not enabled.'},
        'status':{'finished_at':runs[0]['finished'] if runs else None},
        'blocker':'Paid Jev disabled; complete historical holders, independent-controller evidence, verified venue phase and executable exit-capacity evidence are not connected.'}


def detail(root, identifier):
    if not isinstance(identifier,str) or len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('Invalid assessment identifier')
    with closing(reader(Path(root)/'runtime/jev-review/assessments.sqlite3')) as db:
        row=db.execute('SELECT * FROM assessments WHERE id=?',(identifier,)).fetchone()
        if row is None: raise ValueError('Unknown assessment')
        out=dict(row)
        for k in ('dossier','baseline','result'):out[k]=json.loads(out[k])
        return out


def links(root, account):
    path=Path(root)/'runtime/jev-review/assessments.sqlite3'
    if not path.exists(): return {}
    with closing(reader(path)) as db:
        rows=db.execute('SELECT id,status,dossier,result FROM assessments WHERE account_id=? ORDER BY started DESC LIMIT 200',(account,)).fetchall()
    out={}
    for row in rows:
        mint=json.loads(row['dossier'])['mint']
        out.setdefault(mint,{'id':row['id'],'status':row['status'],'proposed':json.loads(row['result']).get('proposed')})
    return out
