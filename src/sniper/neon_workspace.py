"""Read-only NEON presentation derived from its existing authoritative book.

No constructors, model calls, scheduler actions or execution permissions live here.
"""
from decimal import Decimal as D
import json
import re
import time
import threading
from collections import OrderedDict
from urllib.parse import urlsplit

STAGES=('scout','filter','analyst','execution','guard')
LABELS=('SCOUT','FILTER','ANALYST','PAPER EXECUTION','GUARD')
CATEGORIES=('strategy','missing_evidence','provider','portfolio')


def safe(value, maximum=300):
    text=str(value or '')
    text=re.sub(r'https?://\S+', '[provider reference redacted]', text)
    text=re.sub(r'(?i)(bearer\s+|api[_ -]?key\s*[:=]\s*|token\s*[:=]\s*)\S+', '[redacted]', text)
    return text[:maximum]


def finite_decimal(value):
    try:
        number=D(str(value))
        return number if number.is_finite() else None
    except (ValueError,TypeError,ArithmeticError):return None


def stage(name, kind='', data=None):
    if kind in ('source_observation','candidate_intake'):return 'scout'
    if kind in ('position_changed',):return 'guard'
    if kind=='paper_fill':return 'execution' if (data or {}).get('kind')=='buy' else 'guard'
    if kind=='worker_state':return 'guard' if (data or {}).get('job')=='watch' else 'scout'
    if name in ('intake','claim','leader','selection','source','admission','chain','event','signal'):return 'scout'
    if name in ('limits','facts','pool'):return 'filter'
    if name in ('judge','jev'):return 'analyst'
    if name in ('refresh','entry'):return 'execution'
    return 'scout'


def category(reason):
    r=str(reason or '').lower()
    if any(x in r for x in ('stage failed','operation failed','timeout','http','provider failed','feed_unavailable','network','rpc error','interrupted')):return 'provider'
    if any(x in r for x in ('cooldown','maximum','max open','entry limit','daily','cash','portfolio','capital','limit reached')):return 'portfolio'
    if any(x in r for x in ('missing','unavailable','disabled','not started','not active','not current','not configured','unqualified','no current','not a selected','not selected','stale','unverified','unreconciled','not validated','not eligible','evidence','selection')):return 'missing_evidence'
    return 'strategy'


def display_reason(kind,status,reason):
    if (kind=='source_observation' and status=='ERROR') or category(reason)=='provider':
        return 'Provider or pipeline operation failed; diagnostic details remain server-side'
    return safe(reason)


def outcome(kind,payload):
    if kind=='candidate_intake':return 'passed'
    if kind=='source_observation':return 'running' if payload.get('status')=='RUNNING' else 'passed' if payload.get('status')=='OK' else 'error'
    if kind=='readiness_changed':return 'passed' if payload.get('entry_enabled') is True else 'blocked'
    if kind=='paper_fill':return 'filled'
    if kind=='position_changed':return 'error' if payload.get('feed_error') else 'passed'
    status=payload.get('status')
    if kind=='worker_state':return 'running' if status=='running' else 'passed' if status=='ok' else 'error'
    if status=='STARTED':return 'running'
    if status=='PASSED':return 'passed'
    if payload.get('entry'):return 'filled'
    if payload.get('verdict') in ('BUY','SKIP'):
        c=category(payload.get('reason'))
        return 'error' if c=='provider' else 'blocked' if c in ('portfolio','missing_evidence') else 'rejected'
    return 'passed'


def event_item(row,account_id,stream):
    p=json.loads(row['payload']);kind=row['kind'];lane=stage(p.get('stage'),kind,p)
    mint=p.get('mint') or None;status=outcome(kind,p)
    reason=display_reason(kind,p.get('status'),p.get('reason'))
    if kind=='readiness_changed':message='Entry readiness changed · '+safe(p.get('trial_state'))+' · '+'; '.join(safe(x) for x in p.get('blockers',[]))
    elif kind=='candidate_intake':message='Unqualified trader profile observed · '+safe(p.get('handle') or p.get('wallet'))
    elif kind=='source_observation':message=safe(p.get('source_name'))+' source '+safe(p.get('status'))+(' · '+reason if reason else '')
    elif kind=='paper_fill':
        action='PAPER ENTRY' if p.get('kind')=='buy' else 'PARTIAL EXIT' if reason.startswith('take_profit') else 'CLOSE'
        message=f"{action} recorded · quantity {p.get('quantity')} · modeled price ${p.get('price')}"
        if p.get('kind')!='buy':message+=f" · realized P&L ${p.get('pnl')} · {reason}"
    elif kind=='position_changed':
        message='Price unavailable; holding retained, no exit fill' if p.get('feed_error') else 'Position observation recorded; remaining quantity '+str(p.get('remaining'))
    elif kind=='worker_state':message=safe(p.get('job'))+' worker '+safe(p.get('status'))
    elif status=='running':message=safe(p.get('stage'))+' started; awaiting its result'
    elif status=='passed':message=safe(p.get('stage'))+' passed'
    else:message=reason or 'Decision recorded'
    allowed=('candidate_id','mint','stage','status','verdict','entry','leader_event_id','id','position_id','pool','kind','quantity','price','pnl','reason','run_id','job','remaining','feed_error','source_name','source_id','wallet','handle','source_kind','candidate_kind','qualified','trial_state','entry_enabled')
    data={k:p.get(k) for k in allowed if k in p}
    if 'reason' in data:data['reason']=reason
    if 'feed_error' in data:data['feed_error']=bool(data['feed_error'])
    return {'id':stream+':'+str(row['seq']),'seq':row['seq'],'type':kind,'account_id':account_id,
        'stage':lane,'substage':'Jev admission applicability check' if p.get('stage')=='jev' else p.get('stage') or p.get('job') or kind,'outcome':status,
        'category':'provider' if kind=='source_observation' and status=='error' else category(reason) if status in ('blocked','error','rejected') else None,
        'mint':mint,'symbol':mint[:8] if isinstance(mint,str) else None,
        'candidate_id':p.get('candidate_id') or (account_id+':'+mint if mint else None),
        'position_id':p.get('position_id') or (p.get('id') if kind=='position_changed' else None),
        'observed_at':row['observed_at'],'recorded_at':row['recorded_at'],
        'recorded_at_ms':row['recorded_at_ms'] if 'recorded_at_ms' in row.keys() else None,
        'message':safe(message,600),'data':data}


_INDEX_LOCK=threading.Lock()
_INDEXES=OrderedDict()
_JOURNAL_KINDS=('candidate_stage','paper_fill','position_changed','worker_state','source_observation','candidate_intake','readiness_changed')


def _journal_index(db,account_id):
    stream=db.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()
    if not stream:return '',[]
    maximum=db.execute('SELECT coalesce(max(seq),0) FROM observation_events').fetchone()[0]
    key=(account_id,stream[0])
    with _INDEX_LOCK:
        saved=_INDEXES.get(key)
        if not saved or maximum<saved['last']:
            saved={'last':0,'items':[]};_INDEXES[key]=saved
        if maximum>saved['last']:
            for row in db.execute('SELECT seq,kind,payload,observed_at FROM observation_events WHERE seq>? AND seq<=? ORDER BY seq',(saved['last'],maximum)):
                kind=row['kind']
                if kind not in _JOURNAL_KINDS:continue
                p=json.loads(row['payload']);result=outcome(kind,p);reason=display_reason(kind,p.get('status'),p.get('reason'))
                public_reason=safe(p.get('source_name'))+' source '+safe(p.get('status'))+(' · '+reason if reason else '') if kind=='source_observation' else reason
                category_name='provider' if kind=='source_observation' and result=='error' else category(p.get('reason')) if result in ('blocked','error','rejected') else None
                saved['items'].append({'seq':row['seq'],'kind':kind,'observed_at':row['observed_at'],
                    'lane':stage(p.get('stage'),kind,p),'outcome':result,'category':category_name,
                    'mint':p.get('mint'),'candidate_id':p.get('candidate_id') or p.get('mint'),
                    'status':p.get('status'),'reason':public_reason})
            saved['last']=maximum
        _INDEXES.move_to_end(key)
        while len(_INDEXES)>4:_INDEXES.popitem(last=False)
        # Copies references only; projection rows are immutable. A concurrently
        # newer reader must never advance this transaction's snapshot boundary.
        return stream[0],[r for r in saved['items'] if r['seq']<=maximum]


def filtered_journal(db,account_id, *, stage_filter='',token='',outcome_filter='',since=0,category_filter='',reason_filter='',before=None,limit=100):
    if stage_filter and stage_filter not in STAGES:raise ValueError('Unknown stage')
    if outcome_filter and outcome_filter not in ('running','passed','rejected','blocked','error','filled'):raise ValueError('Unknown outcome')
    if category_filter and category_filter not in CATEGORIES:raise ValueError('Unknown category')
    if len(token)>100 or len(reason_filter)>600 or type(since) is not int or since<0 or type(limit) is not int or not 1<=limit<=200:raise ValueError('Invalid journal scope')
    if before is not None and (type(before) is not int or before<=0):raise ValueError('Invalid journal cursor')
    stream,index=_journal_index(db,account_id)
    matching=[r for r in reversed(index) if r['observed_at']>=since
        and (not stage_filter or r['lane']==stage_filter)
        and (not token or token.lower() in str(r['mint'] or '').lower())
        and (not outcome_filter or r['outcome']==outcome_filter)
        and (not category_filter or r['category']==category_filter)
        and (not reason_filter or r['reason']==reason_filter)]
    rejections={};seen=set()
    for row in matching:
        if row['kind']=='source_observation' and row['outcome']=='error':
            group=('provider',row['reason']);rejections[group]=rejections.get(group,0)+1;continue
        if row['kind']!='candidate_stage' or row['status']!='COMPLETED' or row['outcome'] not in ('rejected','blocked','error'):continue
        key=(row['candidate_id'],row['reason'])
        if key in seen:continue
        seen.add(key);group=(row['category'],row['reason']);rejections[group]=rejections.get(group,0)+1
    bounded=[r for r in matching if before is None or r['seq']<before]
    keys=[r['seq'] for r in bounded[:limit]]
    raw=db.execute('SELECT * FROM observation_events WHERE seq IN ('+','.join('?' for _ in keys)+') ORDER BY seq DESC',keys).fetchall() if keys else []
    items=[event_item(row,account_id,stream) for row in raw]
    return {'schema_version':1,'account_id':account_id,'items':items,'total':len(matching),
        'next_before':items[-1]['seq'] if len(bounded)>limit else None,
        'scope':{'since':since,'stage':stage_filter,'token':token,'outcome':outcome_filter,'category':category_filter,'reason':reason_filter,
            'count_basis':'Matching durable journal records since observation-stream installation; rejection bars count distinct candidate/reason or source failure observations'},
        'rejections':[{'category':k[0],'reason':k[1],'count':v} for k,v in sorted(rejections.items(),key=lambda x:-x[1])]}


def journal(app,**kwargs):
    from .console import reader
    from .solana_console import folder,identity
    root=folder(app,'neon')
    with reader(root/'runtime/book.sqlite3') as db:
        account=identity(db,root)
        return filtered_journal(db,account['account_id'],**kwargs)


def workspace(db,account,report,positions,fills,now):
    account_id=account['account_id'];tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    terminal=[dict(r) for r in db.execute('SELECT mint,stage,verdict,reason FROM checks')]
    tokens={r['mint'] for r in terminal}
    trader_count=db.execute('SELECT count(*) FROM neon_candidates').fetchone()[0] if 'neon_candidates' in tables else 0
    if 'leader_events' in tables:
        for row in db.execute("SELECT json_extract(payload,'$.mint') FROM leader_events"):
            if row[0]:tokens.add(row[0])
    closed=[p for p in positions if D(p['remaining'])==0]
    winners=sum(D(p.get('realized_pnl','0'))>0 for p in closed)
    runs=[dict(r) for r in db.execute('SELECT id,job,started,finished,status FROM runs ORDER BY started DESC LIMIT 30')]
    operational=db.execute("SELECT count(*) FROM runs WHERE status IN ('error','interrupted')").fetchone()[0]
    completed=dict(db.execute('SELECT job,max(finished) FROM runs WHERE finished IS NOT NULL GROUP BY job'))
    job_at=lambda name:completed.get(name)
    pending_runs=[r for r in runs if r['status']=='running' and r['finished'] is None]
    running=[r for r in pending_runs if 0<=now-r['started']<=(780 if r['job']=='watch' else 180)]
    generated=report.get('generated_at');fresh=type(generated) is int and 0<=now-generated<=180
    sources=[]
    if 'neon_sources' in tables:sources=[dict(r) for r in db.execute('SELECT name,at,status FROM neon_sources')]
    source_at=max((s['at'] for s in sources if s['status']=='OK'),default=0) or None
    live_data=source_at is not None and 0<=now-source_at<=180 and not any(s['status']!='OK' and now-s['at']<=180 for s in sources)
    blockers=[safe(x) for x in report.get('blockers',[])]
    state='disconnected' if not fresh and not running else 'degraded' if any(s['status'] not in ('OK','RUNNING') for s in sources) or len(pending_runs)>len(running) else 'running' if running else 'blocked' if blockers else 'idle'
    stream=db.execute("SELECT value FROM metadata WHERE key='observation_stream_id'").fetchone()
    journal_page=filtered_journal(db,account_id) if stream else {'items':[],'total':0,'rejections':[],'scope':{}}
    provider_failures=db.execute("SELECT count(*) FROM observation_events WHERE kind='source_observation' AND json_extract(payload,'$.status')='ERROR'").fetchone()[0] if stream else 0
    candidate_failures=len({(r['mint'],r['reason']) for r in terminal if category(r['reason'])=='provider'})
    latest=db.execute("SELECT * FROM observation_events WHERE kind='candidate_stage' AND json_extract(payload,'$.mint') IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone() if stream else None
    mint=json.loads(latest['payload']).get('mint') if latest else None
    candidate_events=[]
    if mint:
        latest_payload=json.loads(latest['payload'])
        precheck=latest_payload.get('leader_event_id') and latest_payload.get('stage') in ('leader','chain','event','selection','admission','source','seen_once','deduplication')
        if precheck:
            # A duplicate has the SAME leader-event ID as its earlier successful
            # entry. Only this terminal precheck describes the new attempt.
            scoped=db.execute('SELECT * FROM observation_events WHERE seq=?',(latest['seq'],))
        else:
            anchor=db.execute("SELECT max(seq) FROM observation_events WHERE kind='candidate_stage' AND json_extract(payload,'$.mint')=? AND json_extract(payload,'$.stage')='claim'",(mint,)).fetchone()[0] or 0
            scoped=db.execute("SELECT * FROM observation_events WHERE kind='candidate_stage' AND json_extract(payload,'$.mint')=? AND seq>=? ORDER BY seq",(mint,anchor))
        candidate_events=[event_item(r,account_id,stream[0]) for r in scoped]
    pipeline=[]
    for key,label in zip(STAGES,LABELS):
        matching=[e for e in candidate_events if e['stage']==key]
        last=matching[-1] if matching else None
        item={'id':key,'label':label,'state':last['outcome'] if last else 'not_run',
            'substage':last['substage'] if last else 'Not run for this candidate' if mint else 'Idle; no eligible token candidate',
            'mint':mint,'candidate_id':account_id+':'+mint if mint else None,
            'started_at':last['observed_at'] if last and last['outcome']=='running' else None,
            'updated_at':last['observed_at'] if last else None,'count':0,'count_label':'account to date'}
        if key=='scout':
            item['count']=trader_count;item['count_label']='unqualified trader profiles · account to date'
            item['token_count']=len(tokens)
            scan=next((r for r in running if r['job']=='signals'),None)
            later=any(e['stage']!='scout' and (last is None or e['seq']>last['seq']) for e in candidate_events)
            if later:item.update(state='passed',substage='Candidate intake completed',started_at=None)
            if scan and (not mint or scan['started']>max((e['observed_at'] for e in candidate_events),default=0)):
                item.update(state='running',substage='Collecting leader/source observations',started_at=scan['started'],updated_at=scan['started'])
            elif not mint:item.update(state='blocked' if blockers else 'not_run',substage=blockers[0] if blockers else 'Idle; waiting for the next scheduled collection',updated_at=job_at('signals'))
        elif key=='filter':item['count']=len(terminal);item['count_label']='unique tokens checked · account to date'
        elif key=='analyst':item['count']=db.execute("SELECT count(*) FROM checks WHERE json_extract(payload,'$.judgment') IS NOT NULL").fetchone()[0];item['count_label']='token judgments · account to date'
        elif key=='execution':item['count']=sum(f['kind']=='buy' for f in fills);item['count_label']='committed paper entries · account to date'
        elif key=='guard':
            item.update(mint=None,candidate_id=None,count=sum(D(p['remaining'])>0 for p in positions),count_label='positions with quantity remaining',started_at=None)
            watch=next((r for r in running if r['job']=='watch'),None)
            if watch:item.update(state='running',substage='Refreshing held positions and evaluating original exits',started_at=watch['started'],updated_at=watch['started'])
            else:item.update(state='error' if any(p.get('feed_error') and D(p['remaining'])>0 for p in positions) else 'passed' if positions else 'not_run',substage='Price unavailable; holdings retained' if any(p.get('feed_error') and D(p['remaining'])>0 for p in positions) else 'Monitoring original exits' if item['count'] else 'Idle; no open positions',updated_at=job_at('watch'))
        if item['state']=='filled':item['state']='passed'
        if item['state']=='running' and item['started_at'] is not None and now-item['started_at']>(780 if key=='guard' else 180):
            item.update(state='blocked',substage='Last observed running; completion is unavailable or stale',started_at=None)
        pipeline.append(item)
    replay=db.execute("SELECT value FROM metadata WHERE key='ui_replay'").fetchone()
    replay=json.loads(replay[0]) if replay else None
    replay={'label':'REPLAY','synthetic':True,'excluded_from_strategy':True,'account_id':account_id} if isinstance(replay,dict) and replay.get('account_id')==account_id and replay.get('synthetic') is True and replay.get('excluded_from_strategy') is True else None
    return {'schema_version':1,'account_id':account_id,'replay':replay,
        'worker':{'state':state,'running_jobs':running,'last_observed_running':pending_runs,'last_scan_at':job_at('signals'),'last_watch_at':job_at('watch'),
                  'next_scan_at':None,'next_watch_at':None,'schedule_observed_at':None},
        'data':{'state':'live' if live_data else 'stale' if source_at else 'unavailable','source_observed_at':source_at,
                'age_seconds':now-source_at if source_at else None,'mode_label':'LIVE DATA · PAPER EXECUTION' if live_data else 'PAPER EXECUTION · DATA '+('STALE' if source_at else 'UNAVAILABLE'),
                'sources':sources},'blockers':blockers,
        'counts':{'scope':'ACCOUNT TO DATE','unique_candidates':len(tokens),'token_candidates':len(tokens),
            'research_traders':trader_count,
            'paper_entries':sum(f['kind']=='buy' for f in fills),'closed_positions':len(closed),'closed_wins':winners,
            'closed_win_rate_pct':str(D(winners)/len(closed)*100) if closed else None,
            'operational_failures':operational+provider_failures+candidate_failures,
            'provider_failures':provider_failures,'worker_failures':operational,'candidate_provider_failures':candidate_failures,
            'failure_scope':'Recorded worker failures, source failures and distinct failed candidate checks; these may describe the same incident'},
        'pipeline':pipeline,'journal':journal_page,'rejections':journal_page['rejections'],
        'source_scope':'Token candidates and trader research are separate; no Pump launch feed is inferred'}


def candidate(app,mint):
    from .adapters.solana import validate_address
    from .console import reader
    from .solana_console import folder,identity
    from rug_desk import config
    validate_address(mint);root=folder(app,'neon')
    with reader(root/'runtime/book.sqlite3') as db:
        account=identity(db,root)
        row=db.execute('SELECT at,payload FROM checks WHERE mint=?',(mint,)).fetchone()
        result=json.loads(row['payload']) if row else {};at=row['at'] if row else None
        facts=result.get('facts') if isinstance(result.get('facts'),dict) else {};quote=result.get('pool') if isinstance(result.get('pool'),dict) else {}
        evidence=facts.get('checks') if isinstance(facts.get('checks'),dict) else {};details=facts.get('details') if isinstance(facts.get('details'),dict) else {};observed=facts.get('observed_at')
        positions=[json.loads(r[0]) for r in db.execute("SELECT payload FROM positions WHERE mint=?",(mint,))]
        fills=[json.loads(r[0]) for r in db.execute("SELECT payload FROM fills WHERE json_extract(payload,'$.mint')=? ORDER BY at,id",(mint,))]
        rows=[]
        definitions=[('not_rugged','Rug status','Not reported rugged'),('authority_revoked','Mint authority','Revoked'),('freeze_revoked','Freeze authority','Revoked'),('no_danger_flags','Danger flags','None'),('risk_score','Risk score','≤ '+str(config.FACTS['max_risk_score'])),('lp_locked','LP locked','≥ '+str(config.FACTS['min_lp_locked_pct'])+'%'),('holders_distributed','Top 10 reported holders','≤ '+str(config.FACTS['max_top10_pct'])+'%'),('creator_holding','Creator share','≤ '+str(config.FACTS['max_dev_pct'])+'%'),('no_insiders','Insiders','≤ '+str(config.FACTS['max_insiders']))]
        for key,name,threshold in definitions:
            status='not run' if key not in evidence else 'missing' if evidence[key] is None else 'passed' if evidence[key] is True else 'rejected'
            value=details.get(key)
            if value is None and key in evidence and evidence[key] is not None:value='Check passed' if evidence[key] else 'Check rejected'
            if isinstance(value,dict):value={k:safe(v) for k,v in value.items() if k in ('pct','exclusions','coverage')}
            elif value is not None:value=safe(value)
            rows.append({'id':key,'name':name,'value':value,'threshold':threshold,'result':status,'source':'Rugcheck provider report','observed_at':observed,
                'missing':'Not evaluated after an earlier rejection or before provider response' if status=='not run' else 'Required evidence missing or malformed' if status=='missing' else None})
        pool_at=quote.get('observed_at');failure=result.get('reason','');order=['pool_liquidity','ticket_share','pool_age','hourly_sell_activity']
        liquidity=finite_decimal(quote.get('liquidity'));valid_at=type(pool_at) is int and pool_at>=0
        values=[str(liquidity) if liquidity is not None and liquidity>=0 else None,str(D(config.TICKET_USD)/liquidity) if liquidity is not None and liquidity>0 else None,
                str((D(pool_at)*1000-D(quote['created_at_ms']))/60000) if valid_at and type(quote.get('created_at_ms')) is int else None,
                quote.get('h1') if isinstance(quote.get('h1'),dict) and all(type(quote['h1'].get(k)) is int and quote['h1'][k]>=0 for k in ('buys','sells')) else None]
        thresholds=['≥ $'+str(config.FACTS['min_liquidity_usd']),'≤ '+str(config.FACTS['max_ticket_share']),str(config.FACTS['min_age_min'])+'–'+str(config.FACTS['max_age_hours']*60)+' minutes','Reject ≥20 buys and zero sells in 1h']
        names=['Pinned-pool liquidity','Ticket / pool share','Pool age','Hourly sell activity']
        failure_index=next((i for i,k in enumerate(order) if failure.startswith(k+':')),None)
        reached_pool=bool(quote)
        for i,(key,name,value,threshold) in enumerate(zip(order,names,values,thresholds)):
            status='not run' if not reached_pool or (failure_index is not None and i>failure_index) else 'missing' if value is None else 'rejected' if failure_index==i else 'passed'
            if result.get('stage')=='pool' and 'stage failed' in failure and failure_index is None:status='missing'
            rows.append({'id':key,'name':name,'value':value,'threshold':threshold,'result':status,'source':'Pinned DexScreener pool observation; indicative, not executable','observed_at':pool_at,'missing':'Not reached' if status=='not run' else 'Required pool evidence unavailable' if status=='missing' else None})
        judged=result.get('judgment') if isinstance(result.get('judgment'),dict) else {};confidence=finite_decimal(judged.get('confidence'));valid_confidence=confidence is not None and 0<=confidence<=1
        validated=judged.get('validated') is True and valid_confidence and judged.get('verdict') in ('BUY','SKIP')
        confidence=str(confidence) if valid_confidence else None
        rows.append({'id':'grok_judgment','name':'Grok/X judgment','value':{'verdict':judged.get('verdict'),'confidence':confidence} if judged else None,
            'threshold':'Validated BUY and confidence ≥ '+str(config.MIN_CONFIDENCE),'result':'not run' if not judged else 'missing' if not valid_confidence else 'passed' if validated and judged.get('verdict')=='BUY' and D(confidence)>=D(str(config.MIN_CONFIDENCE)) else 'rejected',
            'source':'Validated model assessment, separate from deterministic facts','observed_at':judged.get('judged_at'),
            'missing':'Invalid or missing finite confidence' if judged and not valid_confidence else None if judged else 'No model judgment was recorded; earlier gates may have blocked it'})
        refs=[]
        for ref in judged.get('sources',judged.get('citations',[])) if isinstance(judged.get('sources',judged.get('citations',[])),list) else []:
            url=ref if isinstance(ref,str) else ref.get('url') if isinstance(ref,dict) else None
            if isinstance(url,str):
                parsed=urlsplit(url)
                if parsed.scheme=='https' and parsed.hostname in ('x.com','twitter.com','www.x.com','www.twitter.com') and re.fullmatch(r'/[A-Za-z0-9_]+/status/[0-9]+/?',parsed.path):refs.append('https://'+parsed.hostname+parsed.path)
        judgment={k:judged.get(k) for k in ('verdict','confidence','validated','judged_at')}
        judgment['confidence']=confidence;judgment['validated']=validated
        judgment.update(why=safe(judged.get('why')),main_risk=safe(judged.get('main_risk')),x_references=refs)
        page=filtered_journal(db,account['account_id'],token=mint,limit=100)
    # All position arithmetic continues through the shared Python accounting view.
    from .solana_console import view
    current=view(app,'neon')
    displayed=[p for p in current['snapshot']['positions'] if p['asset_id']==mint]
    return {'account_id':account['account_id'],'candidate_id':account['account_id']+':'+mint,'mint':mint,
        'symbol':safe(quote.get('symbol') or mint[:8],40),'pool':facts.get('pool') or quote.get('pool'),
        'observed_at':at,'checks':rows,'judgment':judgment,'positions':displayed,
        'closed_positions':[{k:p.get(k) for k in ('id','mint','pool','remaining','opened_at','closed_at','entry_price','realized_pnl','steps')} for p in positions if D(p['remaining'])==0],
        'fills':[{k:f.get(k) for k in ('id','position_id','mint','pool','kind','at','quantity','price','fee','pnl','reason')} for f in fills],
        'events':page['items'],'entry_recorded':bool(fills and any(f['kind']=='buy' for f in fills)),
        'limitation':'First-failure evaluation; unavailable downstream evidence is not a pass. Provider classifications do not prove independent wallet controllers or executable liquidity.'}
