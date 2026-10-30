"""Durable Helium observations and read-only cockpit projections.

Only the writable paper worker migrates or records. A console read never starts a
worker, calls a provider, fabricates historical valuations, or changes an account.
"""
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import time

from .config import encode, number

STAGES = ('scout', 'filter', 'analyst', 'execution', 'guard')
# A watcher observes a mark before it commits an exit; preserve that causal order
# when both source rows belong to the same financial transaction.
SOURCE_TABLES = {'team_events': 'seq', 'marks': 'id', 'decisions': 'id', 'fills': 'id'}
EVENT_LIMIT = 100
HISTORY_LIMIT = 500
REJECTION_WHERE = ("action IN ('SKIP','WATCH','BLOCKED','ERROR') AND NOT "
                   "(action='ERROR' AND coalesce(json_extract(payload,'$.reason'),'')="
                   "'Exit quote unavailable; holding retained')")


def safe_text(value, limit=500):
    text = str(value or '')
    text = re.sub(r'https?://\S+', '[provider URL omitted]', text, flags=re.I)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)\b(api[_-]?key|authorization|password|secret)\s*[:=]\s*\S+', r'\1=[redacted]', text)
    return text[:limit]


def public_reason(value):
    return safe_text(value) or 'Unspecified recorded decision'


def exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def migrate(book, now):
    """Add observation storage without rewriting pre-upgrade trading history."""
    with book.transaction():
        book.db.execute('''CREATE TABLE IF NOT EXISTS cockpit_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,
            kind TEXT NOT NULL,stage TEXT NOT NULL,mint TEXT,payload TEXT NOT NULL)''')
        book.db.execute('''CREATE TABLE IF NOT EXISTS account_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,payload TEXT NOT NULL)''')
        book.db.execute('''CREATE TABLE IF NOT EXISTS market_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,
            at INTEGER NOT NULL,mint TEXT NOT NULL,pool TEXT NOT NULL,payload TEXT NOT NULL)''')
        book.db.execute('CREATE INDEX IF NOT EXISTS helium_cockpit_mint ON cockpit_events(mint,seq)')
        book.db.execute('CREATE INDEX IF NOT EXISTS helium_market_mint ON market_observations(mint,id)')
        if book.get('cockpit_schema_version') is None:
            # Existing fills remain in the original journal. Do not invent a
            # historical stage timeline or trigger old fills as new notifications.
            for table, column in SOURCE_TABLES.items():
                book.set('cockpit_cursor_'+table, book.db.execute(
                    'SELECT coalesce(max('+column+'),0) FROM '+table).fetchone()[0])
            book.set('cockpit_schema_version', 1)
            book.set('cockpit_observations_started_at', now)
            event(book, now, 'account', 'guard', outcome='observed',
                  reason='Cockpit observation recording started; earlier valuations are not backfilled')
            observe(book, now, 'recording_started')


def event(book, at, kind, stage, **payload):
    payload = dict(payload, account_id=book.identity, demo=book.demo)
    for key in ('reason', 'symbol', 'owner', 'provider'):
        if key in payload:
            payload[key] = safe_text(payload[key])
    return book.db.execute('INSERT INTO cockpit_events(at,kind,stage,mint,payload) VALUES(?,?,?,?,?)',
        (int(at), kind, stage, payload.get('mint'), encode(payload))).lastrowid


def market(book, quote, now, source):
    """Store only actual numerical pool observations with their source time."""
    try:
        price = str(number(quote.get('price'), True))
        observed = quote.get('observed_at')
        if type(observed) is not int or observed > now or not quote.get('mint') or not quote.get('pool'):
            return
        payload = {'mint':quote['mint'], 'pool':quote['pool'],
                   'symbol':safe_text(quote.get('symbol'),80), 'price':price,
                   'observed_at':observed, 'recorded_at':now, 'source':source}
        key = encode([source,payload['mint'],payload['pool'],observed,price])
        result = book.db.execute('INSERT OR IGNORE INTO market_observations(event_key,at,mint,pool,payload) VALUES(?,?,?,?,?)',
            (key, observed, payload['mint'],payload['pool'],encode(payload)))
        if result.rowcount:
            event(book, now, 'market', 'guard' if source == 'watch' else 'scout',
                  outcome='observed', reason='Recorded pinned-pool price', **payload)
    except (ValueError, TypeError, KeyError):
        return


def source_price_current(position, now, max_age):
    mark=position.get('mark') or {}
    observed=(mark.get('quote') or {}).get('observed_at')
    return type(observed) is int and 0<=now-observed<=max_age


def observe(book, now, reason):
    equity, accounting_unresolved = book.equity(now)
    positions=book.positions()
    source_unresolved=sum(not source_price_current(p,now,book.cfg['quote_max_age_seconds']) for p in positions)
    unresolved=sum(not source_price_current(p,now,book.cfg['quote_max_age_seconds'])
                   or not p.get('mark') or not 0<=now-p['mark']['at']<=book.cfg['quote_max_age_seconds']
                   for p in positions)
    unknown = sum(json.loads(row[0]).get('usage',{}).get('billed_usd') is None
                  for row in book.db.execute('SELECT payload FROM api_requests'))
    pnl = equity-number(book.cfg['starting_cash_usd'])
    value = {'observed_at':now, 'equity':str(equity),
             'chart_equity':str(equity) if not unresolved else None,
             'net_pnl':str(pnl) if not unknown and not unresolved else None,
             'estimated_net_pnl':str(pnl), 'unresolved_positions':unresolved,
             'accounting_unresolved_positions':accounting_unresolved,
             'source_stale_positions':source_unresolved,'valuation_current':not unresolved,
             'unknown_api_bills':unknown, 'cash':book.get('cash'), 'reason':reason,
             'demo':book.demo}
    book.db.execute('INSERT INTO account_observations(at,payload) VALUES(?,?)', (now,encode(value)))


def decision_stage(action, payload, task=None):
    reason = str(payload.get('reason',''))
    if action == 'SELL' or 'Exit quote' in reason:
        return 'guard'
    if action == 'BUY':
        return 'execution'
    if task:
        mapped = {'discovery':'scout','integrity':'filter','analysis':'analyst',
                  'review':'analyst','execution':'execution'}.get(task.get('stage'))
        if mapped:
            return mapped
    if payload.get('review') or payload.get('grok') or payload.get('team_gate'):
        return 'analyst'
    if payload.get('checks'):
        return 'filter'
    return 'scout'


def sync(book):
    """Append observations inside the same transaction as source fills/marks."""
    emitted = []
    for table, column in SOURCE_TABLES.items():
        checkpoint = book.get('cockpit_cursor_'+table) or 0
        rows = book.db.execute('SELECT * FROM '+table+' WHERE '+column+'>? ORDER BY '+column,
                               (checkpoint,)).fetchall()
        for row in rows:
            raw = json.loads(row['payload'])
            at = row['at']
            if table == 'team_events':
                task = book.db.execute('SELECT mint FROM team_tasks WHERE task_id=?',(row['task_id'],)).fetchone()
                stage = {'discovery':'scout','integrity':'filter','analysis':'analyst',
                         'review':'analyst','execution':'execution'}.get(raw.get('stage'))
                # Terminal outcomes are represented by their source decision,
                # avoiding an artificial completion of downstream stages.
                if stage:
                    event(book,at,'stage',stage,mint=task['mint'] if task else None,
                          task_id=row['task_id'],owner=raw.get('owner'),
                          outcome='running',reason=raw.get('reason'),source_id=row[column])
            elif table == 'decisions':
                task = book.db.execute('SELECT * FROM team_tasks WHERE mint=?',(row['mint'],)).fetchone()
                task_context=dict(task) if task else None
                if task_context and task_context['stage']=='complete':
                    previous=book.db.execute("SELECT payload FROM team_events WHERE task_id=? AND json_extract(payload,'$.stage')!='complete' ORDER BY seq DESC LIMIT 1",(task_context['task_id'],)).fetchone()
                    if previous:
                        task_context['stage']=json.loads(previous[0]).get('stage')
                event(book,at,'decision',decision_stage(row['action'],raw,task_context),
                      mint=row['mint'],task_id=raw.get('team_task_id'),decision_id=row['id'],
                      outcome=row['action'].lower(),reason=raw.get('reason',row['action']))
            elif table == 'fills':
                event(book,at,'fill','execution' if row['side']=='BUY' else 'guard',
                      mint=row['mint'],fill_id=row['id'],side=row['side'],outcome='filled',
                      symbol=raw.get('symbol'),price=raw.get('fill_price'),quantity=raw.get('quantity'),
                      realized_pnl=raw.get('realized_pnl'),reason=raw.get('reason','paper_entry'),
                      execution_mode='paper')
            else:
                market(book,raw,at,'watch')
            emitted.append(at)
        if rows:
            book.set('cockpit_cursor_'+table,rows[-1][column])
    if emitted:
        observe(book,max(emitted),'committed_journal_change')


def _events(db, account_id, rows):
    result=[]
    for row in rows:
        payload=json.loads(row['payload'])
        result.append(dict(payload,id=account_id+':'+str(row['seq']),seq=row['seq'],
                           at=row['at'],kind=row['kind'],stage=row['stage'],mint=row['mint']))
    return result


def _worker(db, kind, now, cadence):
    row=db.execute('SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1',(kind,)).fetchone()
    finished=db.execute('SELECT max(finished_at) FROM runs WHERE kind=?',(kind,)).fetchone()[0]
    value={'state':'not_started','started_at':None,'finished_at':finished,'overdue':True,'errors':[], 'error_count':0}
    if row:
        errors=[safe_text(e) for e in json.loads(row['payload']).get('errors',[])]
        recent=0<=now-row['started_at']<=max(cadence*3,180)
        overdue=finished is None or now-finished>max(cadence*3,180)
        state='running' if row['finished_at'] is None and recent else 'overdue' if overdue else 'error' if errors else 'idle'
        value.update(state=state,started_at=row['started_at'],finished_at=finished,
                     overdue=overdue,errors=errors,error_count=len(errors))
    return value


def _pipeline(db, result, workers, now):
    counts=result['helium_workspace']['counts']
    count_by_stage={'scout':counts['candidates'],'filter':counts['checked'],
                    'analyst':counts['model_reviews'],'execution':counts['paper_entries'],
                    'guard':counts['open_positions']}
    stages={name:{'id':name,'state':'not_run','mint':None,'task_id':None,
                  'reason':'No recorded stage','updated_at':None,'count':count_by_stage[name]} for name in STAGES}
    task=db.execute('SELECT * FROM team_tasks ORDER BY updated_at DESC,rowid DESC LIMIT 1').fetchone()
    if task:
        task=dict(task)
        events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM team_events WHERE task_id=? ORDER BY seq',(task['task_id'],))]
        reached=[]
        for item in events:
            lane={'discovery':'scout','integrity':'filter','analysis':'analyst','review':'analyst','execution':'execution'}.get(item.get('stage'))
            if lane:
                if reached and reached[-1] != lane:
                    stages[reached[-1]]['state']='passed'
                reached.append(lane)
                stages[lane].update(state='running',mint=task['mint'],task_id=task['task_id'],
                                    reason=safe_text(item.get('reason')),updated_at=item.get('updated_at'))
        if reached and task['state']!='RUNNING':
            lane=reached[-1]
            stages[lane].update(state='filled' if task['state']=='BUY' else
                               'error' if task['state']=='ERROR' else 'blocked' if task['state']=='BLOCKED' else 'rejected',
                               reason=safe_text(task['reason']),updated_at=task['updated_at'])
        elif reached and workers['scan']['state']!='running':
            lane=reached[-1]
            stages[lane].update(state='interrupted' if now-task['updated_at']>180
                                or workers['scan']['state'] in ('error','idle') else 'waiting',
                                reason='Task progress retained; no current running scan owns this stage')
    watch=workers['watch']
    stages['guard'].update(state=watch['state'],reason='Independent stop and take-profit watcher',
                           updated_at=watch['finished_at'])
    if exists(db,'cockpit_events'):
        last=db.execute("SELECT at,mint,payload,kind FROM cockpit_events WHERE stage='guard' AND kind IN ('market','decision','fill') ORDER BY seq DESC LIMIT 1").fetchone()
        if last:
            payload=json.loads(last['payload'])
            stages['guard'].update(mint=last['mint'],reason=safe_text(payload.get('reason')),
                                   updated_at=last['at'])
    return list(stages.values())


def project(db, result, now):
    """Add presentation data; all prices, events and chart points are recorded."""
    account_id=result['account_id'];cfg=result['config'];summary=result['summary']
    workers={kind:_worker(db,kind,now,cfg.get(kind+'_seconds',60)) for kind in ('scan','watch')}
    gate=result['status']['team_gate']
    blockers=[safe_text(b) for b in gate.get('blockers',[])]
    if result.get('daily',{} ) and result['daily'].get('halted'):
        blockers.append('Berlin-day loss halt: new entries stopped; exit watcher remains available')
    errors=[e for w in workers.values() for e in w['errors']]
    worker_state=('running' if any(w['state']=='running' for w in workers.values()) else
                  'not_started' if all(w['state']=='not_started' for w in workers.values()) else
                  'error' if errors else 'overdue' if any(w['overdue'] for w in workers.values()) else
                  'blocked' if blockers or not gate['enabled'] else 'idle')
    counts={'candidates':db.execute('SELECT count(*) FROM candidates').fetchone()[0],
            'checked':db.execute("SELECT count(*) FROM team_artifacts WHERE kind='evidence'").fetchone()[0],
            'model_reviews':db.execute("SELECT count(*) FROM team_artifacts WHERE kind='review'").fetchone()[0],
            'paper_entries':db.execute("SELECT count(*) FROM fills WHERE side='BUY'").fetchone()[0],
            'paper_exits':db.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0],
            'closed_positions':summary['completed_trades'],'open_positions':len(result['positions']),
            'rejected':sum(result['decision_counts'].get(k,0) for k in ('SKIP','WATCH')),
            'blocked':result['decision_counts'].get('BLOCKED',0),'errors':result['decision_counts'].get('ERROR',0)}
    ready=exists(db,'cockpit_events')
    count=db.execute('SELECT count(*) FROM cockpit_events').fetchone()[0] if ready else 0
    latest=db.execute('SELECT coalesce(max(seq),0) FROM cockpit_events').fetchone()[0] if ready else 0
    events=_events(db,account_id,reversed(db.execute('SELECT * FROM cockpit_events ORDER BY seq DESC LIMIT ?', (EVENT_LIMIT,)).fetchall())) if ready else []
    history=[];tape=[]
    if exists(db,'account_observations'):
        for row in reversed(db.execute('SELECT * FROM account_observations ORDER BY id DESC LIMIT ?', (HISTORY_LIMIT,)).fetchall()):
            history.append(dict(json.loads(row['payload']),id=account_id+':valuation:'+str(row['id'])))
    if exists(db,'market_observations'):
        for row in db.execute('''SELECT * FROM market_observations WHERE id IN
            (SELECT max(id) FROM market_observations GROUP BY mint,pool) ORDER BY at DESC,id DESC LIMIT 20'''):
            payload=json.loads(row['payload'])
            payload['state']='fresh' if 0<=now-payload['observed_at']<=cfg['quote_max_age_seconds'] else 'stale'
            tape.append(payload)
    observed=max((p['observed_at'] for p in tape),default=None)
    data_state='unavailable' if observed is None else 'live' if 0<=now-observed<=cfg['quote_max_age_seconds'] else 'stale'
    if result.get('demo'):
        data_state='replay'
    rejections=[]
    db.create_function('helium_public_reason',1,public_reason,deterministic=True)
    for row in db.execute("SELECT helium_public_reason(json_extract(payload,'$.reason')) AS reason,count(*) AS total FROM decisions WHERE "+REJECTION_WHERE+" GROUP BY 1 ORDER BY total DESC,reason LIMIT 100"):
        ids=[r[0] for r in db.execute("SELECT id FROM decisions WHERE "+REJECTION_WHERE+" AND helium_public_reason(json_extract(payload,'$.reason'))=? ORDER BY id DESC LIMIT 25",(row['reason'],))]
        rejections.append({'reason':row['reason'],'count':row['total'],
                           'decision_ids':ids,'decisions_truncated':row['total']>len(ids)})
    source_unresolved=0
    for position in result['positions']:
        mark=position.get('mark') or {};quote=mark.get('quote') or {}
        entry=number(position['entry_price'])
        source_current=source_price_current(position,now,cfg['quote_max_age_seconds'])
        source_unresolved+=not source_current
        accounting_current=position.get('mark_current') is True
        last_guard=db.execute("SELECT payload FROM cockpit_events WHERE mint=? AND stage='guard' AND kind IN ('market','decision','fill') ORDER BY seq DESC LIMIT 1",(position['mint'],)).fetchone() if ready else None
        guard_payload=json.loads(last_guard[0]) if last_guard else {}
        failed_attempt=guard_payload.get('outcome')=='error'
        position.update(observed_price=quote.get('price'),observed_at=quote.get('observed_at'),
                        accounting_mark_current=accounting_current,
                        accounting_unrealized_pnl=position.get('unrealized_pnl'),
                        source_price_current=source_current,
                        mark_current=accounting_current and source_current,
                        unrealized_pnl=position.get('unrealized_pnl') if source_current else None,
                        stop_price=str(entry*(1-number(cfg['stop_loss_pct']))),
                        take_price=str(entry*(1+number(cfg['take_profit_pct']))),
                        trailing_supported=False,partial_exits_supported=False,
                        latest_quote_failed=failed_attempt,
                        exit_status='full_stop_or_take_profit' if accounting_current and source_current and not failed_attempt else 'quote_unavailable_hold')
    starting=number(summary['starting_cash'])
    summary['source_stale_positions']=source_unresolved
    summary['accounting_unresolved_positions']=summary['unresolved_positions']
    summary['valuation_current']=not source_unresolved and not summary['unresolved_positions']
    summary['accounting_net_pnl']=summary['net_pnl']
    if not summary['valuation_current']:
        summary['net_pnl']=None
    summary['return_pct']=str(Decimal(summary['conservative_pnl'])/starting*100) if starting and summary['valuation_current'] else None
    summary['return_is_estimate']=bool(summary['unknown_api_bills'])
    if source_unresolved and not result.get('demo'):
        data_state='stale'
    result['equity_history']=history
    result['market']=tape
    counts['events']=count
    result['helium_workspace']={
        'version':1,'data':{'state':data_state,'observed_at':observed,
                          'max_age_seconds':cfg['quote_max_age_seconds'],
                          'unresolved_positions':sum(not p.get('mark_current') for p in result['positions']),
                          'source_stale_positions':source_unresolved,
                          'reason':'An open position source quote is stale or missing' if source_unresolved else None},
        'worker':dict(workers,state=worker_state,last_scan_at=workers['scan']['finished_at'],
                      last_watch_at=workers['watch']['finished_at'],blockers=blockers,errors=errors),
        'counts':counts,'events':events,'events_limit':EVENT_LIMIT,'total_events':count,
        'events_truncated':count>len(events),'cursor':account_id+':'+str(latest),
        'rejections':rejections,'rejections_scope':'Recorded skipped, blocked or failed candidates; exit-quote outages stay in worker activity; up to 100 reason groups',
        'history_limit':HISTORY_LIMIT,'history_scope':'Recorded worker valuations; no historical backfill',
        'recording_ready':ready,
        'execution':{'mode':'paper','live_ready':False,'account_id':account_id,
            'blockers':['Helium live execution adapter and account activation are not implemented; paper and live journals must remain separate']}}
    result['helium_workspace']['pipeline']=_pipeline(db,result,workers,now)
    return result


def _connect(root):
    path=Path(root)/'helium_bot/runtime/book.sqlite3'
    if not path.exists():
        raise ValueError('Helium account is not initialized')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2,isolation_level=None)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        meta={row[0]:json.loads(row[1]) for row in db.execute("SELECT key,value FROM meta WHERE key IN ('bot_id','account_id')")}
        if meta.get('bot_id')!='helium' or not meta.get('account_id'):
            raise ValueError('Helium account identity is unavailable')
        return db,meta['account_id']
    except BaseException:
        db.close();raise


def _decision(row):
    raw=json.loads(row['payload']);checks=[]
    for check in raw.get('checks',[]):
        value=check.get('value')
        checks.append({'name':safe_text(check.get('name'),80),'state':safe_text(check.get('state'),32),
                       'reason':safe_text(check.get('reason')),'scope':safe_text(check.get('scope')),
                       'value':safe_text(value,160) if isinstance(value,str) else
                               value if value is None or type(value) in (bool,int,float) else None})
    return {'id':row['id'],'at':row['at'],'mint':row['mint'],'action':row['action'],
            'reason':safe_text(raw.get('reason')),'checks':checks,'task_id':raw.get('team_task_id')}


def journal(root, after='', limit=200, reason_filter='', stage_filter='', before=None):
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError('Helium journal limit must be between 1 and 500')
    if stage_filter and stage_filter not in STAGES:
        raise ValueError('Unknown Helium pipeline stage')
    if len(reason_filter)>500:
        raise ValueError('Helium reason filter is too long')
    if before is not None and (type(before) is not int or not 1<=before<=2**63-1):
        raise ValueError('Helium decision cursor must be a positive integer')
    db,account_id=_connect(root)
    try:
        ready=exists(db,'cockpit_events')
        highest=db.execute('SELECT coalesce(max(seq),0) FROM cockpit_events').fetchone()[0] if ready else 0
        start=0
        if after:
            prefix,separator,value=after.rpartition(':')
            if not separator or prefix!=account_id or not value.isdecimal() or len(value)>18:
                raise ValueError('Cursor belongs to a different account or is malformed')
            start=int(value)
            if start>highest:
                raise ValueError('Cursor is ahead of this account journal')
        rows=db.execute('SELECT * FROM cockpit_events WHERE seq>? ORDER BY seq LIMIT ?', (start,limit+1)).fetchall() if ready else []
        has_more=len(rows)>limit;rows=rows[:limit]
        items=_events(db,account_id,rows)
        # Cursor advances over all consumed rows, even when presentation filters
        # hide some. It can never skip an unconsumed fill in the next page.
        end=rows[-1]['seq'] if rows else start
        filtered=[item for item in items if (not stage_filter or item['stage']==stage_filter)
                  and (not reason_filter or item.get('reason')==reason_filter)]
        result={'account_id':account_id,'prev_cursor':account_id+':'+str(start),
                'cursor':account_id+':'+str(end),'events':filtered,'items':filtered,
                'event_count':len(filtered),'total_events':highest,'has_more':has_more,
                'recording_ready':ready}
        if reason_filter:
            # Match the exact public redacted group label. The database continues
            # to retain its original evidence and is never modified by a read.
            db.create_function('helium_public_reason',1,public_reason,deterministic=True)
            clause=REJECTION_WHERE+" AND helium_public_reason(json_extract(payload,'$.reason'))=?"
            total=db.execute('SELECT count(*) FROM decisions WHERE '+clause,(reason_filter,)).fetchone()[0]
            params=[reason_filter]
            if before is not None:
                clause+=' AND id<?';params.append(before)
            matches=db.execute('SELECT * FROM decisions WHERE '+clause+' ORDER BY id DESC LIMIT 51',params).fetchall()
            selected=matches[:50]
            result.update(decisions=[_decision(row) for row in selected],total_matching_decisions=total,
                          has_more_decisions=len(matches)>50,
                          next_before=selected[-1]['id'] if len(matches)>50 else None)
        return result
    finally:
        db.close()


def candidate(root, mint):
    from sniper.adapters.solana import validate_address
    validate_address(mint)
    db,account_id=_connect(root)
    try:
        task=db.execute('SELECT * FROM team_tasks WHERE mint=?',(mint,)).fetchone()
        decisions=[_decision(row) for row in db.execute('SELECT * FROM decisions WHERE mint=? ORDER BY id DESC LIMIT 50',(mint,))]
        fills=[]
        for row in db.execute('SELECT * FROM fills WHERE mint=? ORDER BY id DESC LIMIT 50',(mint,)):
            payload=json.loads(row['payload'])
            for field in ('symbol','reason','label'):
                if field in payload:
                    payload[field]=safe_text(payload[field])
            fills.append(dict(row,payload=payload))
        events=_events(db,account_id,reversed(db.execute('SELECT * FROM cockpit_events WHERE mint=? ORDER BY seq DESC LIMIT 100',(mint,)).fetchall())) if exists(db,'cockpit_events') else []
        artifacts=[]
        if task:
            for row in db.execute('SELECT kind,at,hash,payload FROM team_artifacts WHERE task_id=?',(task['task_id'],)):
                raw=json.loads(row['payload'])
                artifacts.append({'kind':row['kind'],'at':row['at'],'hash':row['hash'],
                                  'risk':raw.get('risk'),'decision':raw.get('decision'),
                                  'validated':raw.get('validated') is True})
        public_task=dict(task,reason=safe_text(task['reason'])) if task else None
        return {'account_id':account_id,'mint':mint,'task':public_task,
                'checks':decisions[0]['checks'] if decisions else [],'artifacts':artifacts,
                'decisions':decisions,'fills':fills,'events':events,
                'limits':{'decisions':50,'fills':50,'events':100},'read_only':True}
    finally:
        db.close()
