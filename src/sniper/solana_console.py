"""Read-only shared view of the existing Solana and Neon authoritative books."""
from decimal import Decimal as D
import hashlib
import json
import sqlite3
from pathlib import Path
import time
from . import paper_events

ROOTS={'solana':'rug_desk','neon':'neon_bot'}

def folder(root,bot):
    if bot not in ROOTS:raise ValueError('Unknown paper bot')
    return Path(root)/ROOTS[bot]

def meta(db,key,default=None):
    r=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else default

def identity(db,root):
    from rug_desk import config
    from rug_desk.book import encode
    value=meta(db,'identity');marker=json.loads((root/'runtime/account.json').read_text(encoding='utf-8-sig'))
    if value!=marker or value['mode']!='paper' or value['config_sha256']!=hashlib.sha256(encode(config.settings()).encode()).hexdigest():
        raise ValueError('Paper account/configuration binding mismatch')
    return value

def view(app,bot='solana',now=None):
    from .console import reader,rows,queue
    from rug_desk import config
    now=int(time.time() if now is None else now);root=folder(app,bot);runtime=root/'runtime'
    report_path=runtime/('status.json' if bot=='neon' else 'report.json')
    report=json.loads(report_path.read_text(encoding='utf-8-sig')) if report_path.exists() else {}
    with reader(runtime/'book.sqlite3') as db:
        account=identity(db,root)
        reported=report.get('account_id') if bot=='neon' else report.get('identity',{}).get('account_id')
        if report and reported!=account['account_id']:raise ValueError('Status account mismatch')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        neon_policy=meta(db,'neon_policy')
        if bot=='neon':
            from .neon import POLICY
            if neon_policy!=POLICY:raise ValueError('Neon policy mismatch')
        trial=meta(db,'trial',{});cost_policy=meta(db,'execution_cost_policy',{})
        fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM fills ORDER BY at,id')]
        positions=[json.loads(r[0]) for r in db.execute('SELECT payload FROM positions')]
        snapshot=paper_events.financial(db,account,root,now,fills=fills,positions=positions)
        runs=rows(db,'SELECT job,started,finished,status FROM runs ORDER BY started DESC LIMIT 30')
        checks=rows(db,'SELECT mint,at,stage,verdict,reason FROM checks ORDER BY at DESC LIMIT 80')
        reasons=rows(db,'SELECT verdict,reason,count(*) count FROM checks GROUP BY verdict,reason ORDER BY count DESC')
        high=paper_events.cursor(db)
        observations=rows(db,"SELECT seq,observed_at,payload FROM observation_events WHERE kind='pnl_observation' ORDER BY seq DESC LIMIT 4000")[::-1] if high else []
        legacy_pnl=[json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_equity ORDER BY at DESC LIMIT 4000')][::-1] if 'neon_equity' in tables else []
        market=rows(db,"SELECT payload FROM observation_events WHERE kind='market_observation' ORDER BY seq DESC LIMIT 30") if high else []
        candidates=[json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_candidates ORDER BY at DESC LIMIT 30')] if 'neon_candidates' in tables else []
        gaps=rows(db,'SELECT from_at AS "from",to_at AS "to",to_at-from_at seconds FROM neon_monitoring_gaps ORDER BY id') if 'neon_monitoring_gaps' in tables else []
        leader=rows(db,'SELECT event_id,at,stage,verdict,reason,payload FROM leader_decisions ORDER BY id DESC LIMIT 80') if 'leader_decisions' in tables else []
    observed=report.get('generated_at');stale=type(observed) is not int or not 0<=now-observed<=(180 if bot=='neon' else 2100)
    blockers=list(report.get('blockers',[])) if bot=='neon' else []
    model=report.get('model',{}) if bot=='neon' else {'enabled':report.get('paid_ai_enabled')}
    if model.get('enabled') is not True:blockers.append('Worker evidence does not enable the required model gate')
    if not trial:blockers.append('Paper trial has not started')
    if stale:blockers.append('Worker readiness observation is missing or stale')
    if not snapshot['api_costs_known']:blockers.append('Model bills are unreconciled')
    phase='EXITS_ONLY' if trial and now>=trial['ends_at'] else 'ACTIVE' if trial else 'SETUP_BLOCKED'
    if phase=='EXITS_ONLY':blockers.append('Original entry deadline passed; exits continue')
    readiness={'state':'STALE' if stale else 'ENTRY_BLOCKED' if phase=='ACTIVE' and blockers else phase,'entry_enabled':False if blockers else report.get('entry_enabled'),
               'observed_at':observed,'age_seconds':now-observed if type(observed) is int else None,
               'blockers':blockers,'scope':'Worker observation; current candidate admission remains authoritative'}
    by_id={p['id']:p for p in positions}
    for p in snapshot['positions']:
        original=by_id[p['id']];entry=D(p['entry_price']);basis=D(p['cost_basis'])
        p.update(qty=p['quantity'],remaining_basis=p['cost_basis'],liquidation_value=p['liquidation_value_usd'],
            mark_status='missing_quote' if p['quote_unavailable'] else 'ok',mark_at=p['last_quote_at'],
            mark_price=(original.get('liquidation_cost_estimate') or {}).get('reference_price'),
            unrealized_pct=str(D(p['unrealized_pnl'])/basis*100) if basis else '0',tp_done=p['steps']>0,
            stop_price=str(entry*(1+D(str(config.EXIT['stop_pct']))/100)),
            take_price=str(entry*(1+D(str(config.EXIT['ladder'][0][0]))/100)),
            trailing_price=str(D(p['peak'])*(1-D(str(config.EXIT['trail_pct']))/100)),mark_liquidity=None)
    display=[]
    for f in fills:
        costs=f.get('execution_costs') or {};buy=f['kind']=='buy';amount=D(f['quantity'])*D(f['price'])
        cash=-(D(costs.get('reference_notional_usd',amount))+D(f['fee'])+D(costs.get('rent_capital_reserved_usd','0'))) if buy else amount-D(f['fee'])
        display.append({**f,'asset_id':f['mint'],'symbol':f['mint'][:8],'observed_at':f['at'],
            'action':'OPEN' if buy else 'EXPENSE' if f['kind']=='expense' else 'PARTIAL' if f['reason'].startswith('take_profit') else 'CLOSE',
            'fill_price':f['price'],'reference_price':costs.get('reference_price'),'cash_delta':str(cash),
            'friction_usd':costs.get('embedded_friction_usd','0'),'gas_usd':f['fee']})
    snapshot['fees_paid']=str(sum((D(f['friction_usd'])+D(f['gas_usd']) for f in display),D(0)))
    assets={p['mint']:{'asset_id':p['mint'],'pool_id':p['pool'],'symbol':p['mint'][:8],'state':'open' if D(p['remaining'])>0 else 'closed'} for p in positions}
    decisions=[{**r,'asset_id':r['mint'],'symbol':r['mint'][:8],'observed_at':r['at']} for r in checks]
    for r in leader:
        payload=json.loads(r.pop('payload'));mint=payload.get('mint','')
        decisions.append({**r,'asset_id':mint,'symbol':mint[:8],'observed_at':r['at']})
    decisions.sort(key=lambda r:r['observed_at'],reverse=True)
    pnl_by_time={r['as_of']:{**r,'observed_at':r['as_of']} for r in legacy_pnl}
    pnl_by_time.update({r['observed_at']:{'observed_at':r['observed_at'],**json.loads(r['payload'])} for r in observations})
    pnl=sorted(pnl_by_time.values(),key=lambda r:r['observed_at'])[-4000:]
    return {'account_id':account['account_id'],'name':'Neon bot' if bot=='neon' else 'RUG DESK · Solana','chain':'Solana Mainnet',
        'status':{'finished_at':max((r['finished'] or 0 for r in runs),default=0),'trial_state':phase},
        'snapshot':snapshot,'trial':trial,'policy':{**config.settings(),'time_stop_seconds':config.EXIT['time_stop_hours']*3600,'time_stop_min_gain_pct':config.EXIT['time_stop_min_pct']},
        'policy_binding':account['config_sha256'],'readiness':readiness,'assets':list(assets.values()),'fills':display[::-1][:500],
        'closed':[p for p in positions if D(p['remaining'])<=0][-200:],
        'equity_history':[{'observed_at':r['observed_at'],'equity':r['equity']} for r in pnl if r.get('equity') is not None],
        'pnl_history':pnl,'cost_policy':cost_policy,'screens':{},'decisions':decisions[:80],'reasons':reasons,'runs':runs,'gaps':gaps,
        'policy_revisions':[],'market':[json.loads(r['payload']) for r in market],'event_counts':report.get('observations',{}),
        'notifications':{'trades':queue(runtime/'notifications.sqlite3')},'fill_count':len(fills),
        'cadence_seconds':60 if bot=='neon' else config.WATCH_SECONDS,'setup_blocked':not trial,
        'blocker':'; '.join(blockers),'blockers':blockers,'paid_ai_enabled':model.get('enabled') is True,
        'fill_model':'Existing cost-aware paper fills; no wallet execution','candidates':candidates,
        'selection':report.get('selection',report.get('leader_selection',{})),'sources':report.get('sources',[]),
        'source_scope':(neon_policy or {}).get('source_scope','Existing screened Solana candidates'),
        'research':{'candidate_count':report.get('candidate_count',len(candidates)),'approved_wallet_count':len(report.get('selection',{}).get('leaders',[]))},
        'error_count':sum(r['status']=='error' for r in runs[:2]),
        'event_stream':{'schema_version':1,'cursor':high,'available':high is not None,'snapshot_consistent':True,
                        'url':'/api/events?bot='+bot,'historical_backfill':False,'ordering':'seq is commit order; observed_at is evidence time'}}

def events(app,bot,after,limit=200):
    from .console import reader
    root=folder(app,bot)
    with reader(root/'runtime/book.sqlite3') as db:
        account=identity(db,root)
        return paper_events.events(db,account['account_id'],after,limit)

def discover(app):
    from .console import reader
    result={}
    for bot in ROOTS:
        root=folder(app,bot)
        try:
            with reader(root/'runtime/book.sqlite3') as db:
                account=identity(db,root)
                finished=db.execute('SELECT max(finished) FROM runs').fetchone()[0]
                result[bot]={'available':True,'account_id':account['account_id'],
                             'last_completed_at':finished,'event_stream_available':paper_events.cursor(db) is not None}
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError):
            result[bot]={'available':False,'account_id':None,'last_completed_at':None}
    return result

def chart(app,bot,asset,pool):
    from .console import reader
    from .adapters.solana import validate_address
    validate_address(asset);validate_address(pool);root=folder(app,bot)
    with reader(root/'runtime/book.sqlite3') as db:
        identity(db,root);points=[]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'observation_events' in tables:
            for r in db.execute("SELECT payload FROM observation_events WHERE kind='market_observation' AND json_extract(payload,'$.mint')=? AND json_extract(payload,'$.pool')=? ORDER BY seq DESC LIMIT 12000",(asset,pool)):
                p=json.loads(r[0]);points.append({'at':p['observed_at'],'price':p['price'],'liquidity':p['liquidity']})
        if bot=='neon' and 'neon_marks' in tables:
            points += [json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_marks WHERE mint=? AND pool=? ORDER BY at DESC LIMIT 12000',(asset,pool))]
        fills=[json.loads(r[0]) for r in db.execute("SELECT payload FROM fills WHERE json_extract(payload,'$.mint')=? AND json_extract(payload,'$.pool')=? ORDER BY at,id",(asset,pool))]
    points=sorted({(p['at'],p['price']):p for p in reversed(points)}.values(),key=lambda p:p['at'])[-12000:]
    markers=[{'observed_at':f['at'],'action':'OPEN' if f['kind']=='buy' else 'PARTIAL' if f['reason'].startswith('take_profit') else 'CLOSE','fill_price':f['price'],'quantity':f['quantity'],'pnl':f['pnl'],'reason':f['reason']} for f in fills]
    return {'asset_id':asset,'pool_id':pool,'points':points,'fills':markers,'sample_limit':12000,'generated_at':int(time.time()),'source':'Recorded pinned-pool observations; gaps unfilled; paper fills are separate'}
