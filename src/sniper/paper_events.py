"""Derived paper observations in the existing book; never an execution authority."""
from decimal import Decimal as D
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def install(book):
    # Additive, idempotent schema, only after Book validates account/configuration.
    with book.transaction():
        book.db.execute('''CREATE TABLE IF NOT EXISTS observation_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
            observed_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL)''')
        if 'recorded_at_ms' not in {row[1] for row in book.db.execute('PRAGMA table_info(observation_events)')}:
            book.db.execute('ALTER TABLE observation_events ADD COLUMN recorded_at_ms INTEGER')
        book.db.execute('CREATE INDEX IF NOT EXISTS observation_event_time ON observation_events(observed_at,seq)')
        book.db.execute("INSERT OR IGNORE INTO metadata VALUES('observation_stream_id',?)", (str(uuid.uuid4()),))


def emit(book, key, kind, at, payload):
    """Caller owns the transaction. Retransmission has the same stable sequence."""
    if type(at) is not int or at < 0:
        raise ValueError('Invalid observation time')
    body=encode(payload)
    recorded=time.time_ns()//1_000_000
    book.db.execute('INSERT OR IGNORE INTO observation_events(event_key,observed_at,recorded_at,kind,payload,recorded_at_ms) VALUES(?,?,?,?,?,?)',
                    (key,at,recorded//1000,kind,body,recorded))


def stage(book, mint, at, name, status, *, verdict=None, reason=None, entry=False, event_id=None):
    payload={'candidate_id':book.identity['account_id']+':'+mint,'mint':mint,
             'stage':name,'status':status,'verdict':verdict,'reason':str(reason)[:1000] if reason else None,
             'entry':bool(entry),'leader_event_id':event_id}
    key='candidate:'+hashlib.sha256(encode(payload).encode()).hexdigest()
    emit(book,key,'candidate_stage',at,payload)


def market(book, quote, now, source):
    from rug_desk.market import validate_quote
    validate_quote(quote,now)
    at=quote['observed_at']
    payload={k:quote.get(k) for k in ('mint','pool','price','liquidity','observed_at')}
    payload['price'],payload['liquidity']=str(quote['price']),str(quote['liquidity'])
    payload['source']=source
    emit(book,'market:'+hashlib.sha256(encode(payload).encode()).hexdigest(),'market_observation',at,payload)


def costs(root, account_id, *, neon=False):
    """Reuse existing cost attribution; no credentials, budget writes or API calls."""
    root=Path(root).resolve();path=root.parent/'rug_desk/runtime/ai-budget.sqlite3'
    billed=[]
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.execute('BEGIN')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'advisory_request_accounts' in tables:
                billed += [r[0] for r in db.execute('SELECT u.billed_usd FROM advisory_request_accounts a LEFT JOIN advisory_usage u USING(request_id) WHERE a.account_id=?',(account_id,))]
            if not neon and 'advisory_reservations' in tables:
                exclusion=' WHERE r.request_id NOT IN (SELECT request_id FROM advisory_request_accounts)' if 'advisory_request_accounts' in tables else ''
                billed += [r[0] for r in db.execute('SELECT u.billed_usd FROM advisory_reservations r LEFT JOIN advisory_usage u USING(request_id)'+exclusion)]
    from .jev_admission import live_cost_rows
    billed += [r[2] for r in live_cost_rows(root.parent,account_id)]
    return {'billed_usd':str(sum((D(v) for v in billed if v is not None),D(0))),
            'known':all(v is not None for v in billed),'requests':len(billed)}


def financial(db, identity, root, now, *, fills=None, positions=None):
    from .neon import paper_snapshot
    root=Path(root).resolve()
    metadata=dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('neon_policy','capital_baseline')"))
    # Neon already has a persisted explicit baseline. Never assign it to RUG DESK.
    baseline=json.loads(metadata['neon_policy']).get('starting_cash_usd') if 'neon_policy' in metadata else None
    if 'capital_baseline' in metadata:
        explicit=json.loads(metadata['capital_baseline'])
        if explicit.get('account_id')!=identity['account_id'] or not explicit.get('reference'):
            raise ValueError('Unbound capital baseline')
        baseline=explicit['starting_cash_usd']
    if baseline is not None and (not D(baseline).is_finite() or D(baseline)<=0):
        raise ValueError('Invalid capital baseline')
    fills=fills if fills is not None else [json.loads(r[0]) for r in db.execute('SELECT payload FROM fills ORDER BY at,id')]
    positions=positions if positions is not None else [json.loads(r[0]) for r in db.execute('SELECT payload FROM positions')]
    usage=costs(root,identity['account_id'],neon='neon_policy' in metadata)
    snapshot=paper_snapshot(fills,positions,now,usage['billed_usd'],usage['known'],starting_cash=baseline)
    snapshot['known_cost_net_pnl']=snapshot['net_pnl']
    if not usage['known']:snapshot['net_pnl']=snapshot['equity']=snapshot['return_pct']=None
    snapshot['capital_baseline_supported']=baseline is not None
    snapshot['valuation']='Conservative zero for unavailable marks; holding retained, no sale'
    return snapshot


def observe(book, now):
    try:
        snapshot=financial(book.db,book.identity,book.root,now)
        compact={k:v for k,v in snapshot.items() if k!='positions'}
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as exc:
        compact={'as_of':now,'available':False,'error':type(exc).__name__,
                 'net_pnl':None,'equity':None,'reason':'Accounting observation unavailable; consult authoritative book'}
    emit(book,'pnl:'+hashlib.sha256(encode(compact).encode()).hexdigest(),'pnl_observation',now,compact)


def cursor(db):
    row=db.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()
    if not row:return None
    seq=db.execute('SELECT coalesce(max(seq),0) FROM observation_events').fetchone()[0]
    return row[0]+':'+str(seq)


def events(db, account_id, after, limit=200):
    if type(limit) is not int or not 1<=limit<=500:raise ValueError('Invalid event limit')
    head=cursor(db)
    if head is None:return {'available':False,'account_id':account_id,'events':[],'cursor':None,'has_more':False}
    stream,maximum=head.rsplit(':',1);maximum=int(maximum)
    if not isinstance(after,str) or len(after)>100:raise ValueError('Snapshot cursor required')
    source,sequence=after.rsplit(':',1)
    if source!=stream or not sequence.isdecimal() or int(sequence)>maximum:
        raise ValueError('Stream changed or cursor ahead; refetch snapshot')
    items=[]
    columns={r[1] for r in db.execute('PRAGMA table_info(observation_events)')}
    millisecond='recorded_at_ms' if 'recorded_at_ms' in columns else 'NULL'
    for r in db.execute('SELECT seq,observed_at,recorded_at,kind,payload,'+millisecond+' FROM observation_events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?',(int(sequence),maximum,limit)):
        items.append({'id':stream+':'+str(r[0]),'seq':r[0],'account_id':account_id,
                      'previous_cursor':items[-1]['id'] if items else after,
                      'observed_at':r[1],'recorded_at':r[2],'recorded_at_ms':r[5],'type':r[3],'data':public_payload(r[3],json.loads(r[4]))})
    last=items[-1]['seq'] if items else int(sequence)
    return {'schema_version':1,'available':True,'account_id':account_id,'events':items,
            'prev_cursor':after,'event_count':len(items),'cursor':stream+':'+str(last),'high_watermark':head,'has_more':last<maximum}


def public_payload(kind,payload):
    """Public view of immutable observations, never raw provider exception bodies."""
    from .neon_workspace import safe,display_reason
    allow={
        'candidate_stage':('candidate_id','mint','stage','status','verdict','reason','entry','leader_event_id','run_id'),
        'paper_fill':('id','position_id','mint','pool','kind','at','quantity','price','fee','pnl','reason','run_id'),
        'position_changed':('id','mint','pool','remaining','remaining_cost_basis_usd','realized_pnl','steps','peak','opened_at','closed_at','last_observed_at','last_error_at','feed_error','run_id'),
        'worker_state':('run_id','job','status'),
        'source_observation':('source_name','status','reason','source_id','observed_at','run_id'),
        'candidate_intake':('candidate_id','wallet','handle','candidate_kind','source_kind','source_id','observed_at','qualified','reason','run_id'),
        'readiness_changed':('trial_state','entry_enabled','blockers','run_id'),
        'market_observation':('mint','pool','price','liquidity','observed_at','source'),
        'pnl_observation':('as_of','available','net_pnl','equity','cash','starting_cash','realized_pnl','unrealized_pnl','position_basis','rent_reserved_usd','api_cost_usd','api_costs_known','open_positions','closed_positions','unresolved_positions','marks_complete','return_pct','known_cost_net_pnl','capital_baseline_supported','valuation','reason'),
    }.get(kind,())
    result={k:payload[k] for k in allow if k in payload}
    if 'reason' in result:result['reason']=display_reason(kind,result.get('status'),result['reason'])
    for key in ('handle','valuation'):
        if key in result:result[key]=safe(result[key],400)
    if 'blockers' in result:result['blockers']=[safe(v) for v in result['blockers']] if isinstance(result['blockers'],list) else []
    if 'feed_error' in result:result['feed_error']=bool(result['feed_error'])
    return result
