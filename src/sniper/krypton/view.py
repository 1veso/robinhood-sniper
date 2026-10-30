"""Read-only views. Opening the console never initializes an account or calls providers."""
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time


def snapshot(db,now):
    meta={r[0]:json.loads(r[1]) for r in db.execute('SELECT key,value FROM meta')}
    cfg=meta['config'];positions=[];cash=D(meta['cash']);equity=cash;unresolved=0
    for row in db.execute('SELECT payload FROM positions'):
        p=json.loads(row[0]);mark=p.get('mark');fresh=bool(mark and 0<=now-mark['at']<=cfg['quote_max_age_seconds'])
        p['mark_current']=fresh;p['liquidation_value']=mark['net_value'] if fresh else '0'
        p['unrealized_pnl']=str(D(p['liquidation_value'])-D(p['basis']));equity+=D(p['liquidation_value'])
        unresolved+=not fresh;positions.append(p)
    fills=[];realized=D(0);closed=[];stress=D(0);network=D(0);friction=D(0)
    for row in db.execute('SELECT id,at,mint,side,payload FROM fills ORDER BY id'):
        f=dict(row);f['payload']=json.loads(f['payload']);p=f['payload'];fills.append(f)
        stress+=D(p['stress_extra_cost_usd']);network+=D(p['explicit_fee_usd']);friction+=D(p['embedded_friction_usd'])
        if f['side']=='SELL':realized+=D(p['realized_pnl']);closed.append(D(p['realized_pnl']))
    api=D(0);unknown=0;attempts=0
    for row in db.execute('SELECT payload FROM api_requests'):
        attempts+=1;usage=json.loads(row[0]).get('usage',{});bill=usage.get('billed_usd')
        if bill is None:unknown+=1
        else:api+=D(bill)
    decisions=[dict(r) for r in db.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 50')]
    for d in decisions:d['payload']=json.loads(d['payload'])
    counts=dict(db.execute('SELECT action,count(*) FROM decisions GROUP BY action'))
    checks={}
    # SQL grouping avoids loading the complete decision journal into memory.
    for name,state,count in db.execute("SELECT json_extract(j.value,'$.name'),json_extract(j.value,'$.state'),count(*) FROM decisions,json_each(decisions.payload,'$.checks') j GROUP BY 1,2"):
        checks.setdefault(name,{})[state]=count
    runs=[]
    for kind in ('scan','watch'):
        for row in db.execute('SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 2',(kind,)):
            r=dict(row);r['payload']=json.loads(r['payload']);runs.append(r)
    completed=[r for r in runs if r['finished_at'] is not None]
    latest=max(completed,key=lambda r:r['finished_at']) if completed else {}
    daily=db.execute('SELECT * FROM days ORDER BY day DESC LIMIT 1').fetchone()
    wins=[p for p in closed if p>0];losses=[p for p in closed if p<0]
    status={'finished_at':latest.get('finished_at'),'cadence_seconds':60,'error_count':len(latest.get('payload',{}).get('errors',[])),
        'health':'DEGRADED' if unresolved or latest.get('state')=='ERROR' else 'SETUP_BLOCKED',
        'model_gate':meta.get('model_gate',{'enabled':False,'blockers':['Paid Grok calls disabled']}),
        'signing_calls':0,'broadcast_calls':0,'external_model_attempts':attempts,'live_execution_ready':False}
    return {'account_id':meta['account_id'],'config_hash':meta['config_hash'],'created_at':meta['created_at'],
        'demo':meta['demo'],'execution_mode':'paper','title':'Krypton','purpose':'Solana New-Token Scanner',
        'status':status,'config':cfg,'cost_policy':meta['cost_policy'],'positions':positions,'decisions':decisions,
        'fills':list(reversed(fills[-50:])),'decision_counts':counts,'check_counts':checks,'runs':runs,
        'providers':{r[0]:json.loads(r[1]) for r in db.execute('SELECT name,payload FROM providers')},
        'candidate_counts':dict(db.execute('SELECT state,count(*) FROM candidates GROUP BY state')),
        'daily':dict(daily) if daily else None,'assets':[],
        'summary':{'starting_cash':cfg['starting_cash_usd'],'cash':str(cash),'equity':str(equity),
            'conservative_pnl':str(equity-D(cfg['starting_cash_usd'])),'realized_pnl':str(realized),
            'rent_reserved':meta['rent_reserved'],'unresolved_positions':unresolved,'fill_count':len(fills),'completed_trades':len(closed),
            'win_rate':len(wins)/len(closed) if closed else None,'average_win':str(sum(wins)/len(wins)) if wins else None,
            'average_loss':str(sum(losses)/len(losses)) if losses else None,'worst_trade':str(min(closed)) if closed else None,
            'network_costs':str(network),'embedded_friction':str(friction),'stress_extra_costs':str(stress),
            'api_spending':str(api),'unknown_api_bills':unknown,
            'net_pnl':str(equity-D(cfg['starting_cash_usd'])-api) if not unknown else None},
        'limits':['Profiles are a limited discovery feed, not all new launches.',
            'Owner aggregation is not verified deployer history or controller independence.',
            'LP passes are pinned-pool provider reports, not independent chain verification.',
            'Paper fills model costs from indicative prices; they are not executable swap quotes.',
            'Unknown marks are conservative zero valuations, never zero-price sales.',
            'Reserved rent is not spendable cash and no refund is assumed.'],
        'notifications':{'enabled':False,'reason':'Console and durable decision log; Telegram not enabled for Krypton'},
        'trial':{'started_at':None,'ends_at':None,'state':'ONGOING_PAPER_RESEARCH','note':'No inherited trial deadline; setup and demo are excluded from trade statistics'}}


def view(root):
    path=Path(root)/'krypton_bot/runtime/book.sqlite3'
    if not path.exists():return {'account_id':None,'title':'Krypton','status':{'health':'NOT_INITIALIZED'},'initialized':False,'assets':[]}
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2,isolation_level=None);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        result=snapshot(db,int(time.time()));result['initialized']=True;return result
    finally:db.close()


def health(root):
    path=Path(root)/'krypton_bot/runtime/book.sqlite3'
    if not path.exists():return {'available':False,'account_id':None,'last_completed_at':None}
    try:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            return {'available':True,'account_id':json.loads(db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0]),
                'last_completed_at':db.execute('SELECT max(finished_at) FROM runs').fetchone()[0],'event_stream_available':False}
        finally:db.close()
    except (OSError,sqlite3.Error,ValueError,TypeError):return {'available':False,'account_id':None,'last_completed_at':None}
