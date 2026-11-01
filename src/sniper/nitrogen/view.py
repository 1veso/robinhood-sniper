"""Read-only projection with unknown values and model reserves kept explicit."""
from contextlib import closing
from decimal import Decimal as D
import json
import sqlite3
import time
from .book import folder_for
from .config import digest
from .judge import BINDING, POLICY


def decoded(db,table,order='rowid DESC',limit=None):
    allowed={'leads','assessments','orders','decisions','fills','runs','providers','outcomes','closed_trades','reports'}
    if table not in allowed:raise ValueError('Unknown view table')
    rows=[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY '+order+(' LIMIT '+str(int(limit)) if limit else ''))]
    for row in rows:
        for key in ('payload','dossier','response','last_dossier'):
            if row.get(key) is not None:row[key]=json.loads(row[key])
    return rows


def snapshot(db,now):
    m={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM meta')};cfg=m['config']
    if m.get('bot_id')!='nitrogen' or m['config_hash']!=digest(cfg) or m['nitrogen_policy']!=BINDING:raise ValueError('NITROGEN identity/policy mismatch')
    billed=reserved=D(0);unknown_bills=0
    for raw, in db.execute('SELECT payload FROM api_requests'):
        a=json.loads(raw);bill=a.get('usage',{}).get('billed_usd')
        if bill is None:
            reserve=D(a.get('reservation_usd','0'));reserved+=reserve;unknown_bills+=reserve>0
        else:billed+=D(bill)
    value=basis=D(0);unresolved=[];positions=[]
    for raw, in db.execute('SELECT payload FROM positions'):
        p=json.loads(raw);mark=p.get('mark') or {};basis+=D(p['basis'])
        fresh=type(mark.get('at')) is int and 0<=now-mark['at']<=cfg['quote_max_age_seconds']
        p['open_value']=mark.get('net_value') if fresh else None
        p['unrealized_pnl']=str(D(mark['net_value'])-D(p['basis'])) if fresh else None
        if fresh:value+=D(mark['net_value'])
        else:unresolved.append(p['mint'])
        positions.append(p)
    fills=decoded(db,'fills');realized=network=friction=D(0)
    for f in fills:
        p=f['payload'];network+=D(p['explicit_fee_usd']);friction+=D(p['embedded_friction_usd'])
        if f['side']=='SELL':realized+=D(p['realized_pnl'])
    closed=decoded(db,'closed_trades');cash=D(m['cash'])-billed-reserved;rent=D(m['rent_reserved'])
    known=cash+rent+value;equity=None if unresolved else known
    runs=decoded(db,'runs',limit=20);providers={r['name']:r['payload'] for r in decoded(db,'providers',limit=100)}
    assessments=decoded(db,'assessments',limit=100)
    for a in assessments:
        if a['response']:
            ans=a['response']['answers'];a['displayed_exit_risk']=ans['exit_risk']['score']+1;a['buy_probability']=ans['action']['probabilities']['buy']
    worker_completions={kind:max((r['finished_at'] or 0 for r in runs if r['kind']==kind),default=0) for kind in ('scan','watch','head')}
    overdue=[kind for kind,at in worker_completions.items() if not at or now-at>3*cfg['poll_seconds']]
    blockers=([] if cfg['tracked_wallets'] else ['No tracked wallets configured'])
    if not m.get('model_gate',{}).get('enabled'):blockers.append('Jev authorization unavailable or disabled; no new model admission')
    if not assessments:blockers.append('No complete current factual dossier')
    provider_errors=[k for k,v in providers.items() if v.get('state')=='ERROR']
    return {'bot':'nitrogen','title':'NITROGEN','purpose':'Jev Judgment Desk','slot':7,'initialized':True,'mode':'paper',
        'account_id':m['account_id'],'config_hash':m['config_hash'],'created_at':m['created_at'],'demo':m['demo'],'assets':[],
        'evidence_label':'synthetic' if m['demo'] else 'real_data_paper','config':cfg,'judge_policy':POLICY,
        'summary':{'starting_cash':cfg['starting_cash_usd'],'cash':str(cash),'equity':str(equity) if equity is not None else None,
            'known_equity_subtotal':str(known),'realized_pnl':str(realized),'unrealized_pnl':None if unresolved else str(value-basis),
            'net_pnl':None if unresolved or unknown_bills else str(known-D(cfg['starting_cash_usd'])),
            'deployed_basis':str(basis),'rent_reserved':str(rent),'network_costs':str(network),'embedded_friction':str(friction),
            'model_billed':str(billed),'model_reserved':str(reserved),'unknown_model_bills':unknown_bills,'unresolved_positions':unresolved,
            'closed_trades':len(closed),'wins':sum(D(r['payload']['realized_pnl'])>0 for r in closed),'losses':sum(D(r['payload']['realized_pnl'])<0 for r in closed),'fill_count':len(fills)},
        'pipeline':dict(db.execute('SELECT state,count(*) FROM leads GROUP BY state')),
        'leads':decoded(db,'leads',limit=100),'assessments':assessments,'orders':decoded(db,'orders',limit=100),
        'positions':positions,'fills':fills[:100],'decisions':decoded(db,'decisions',limit=100),'outcomes':decoded(db,'outcomes',limit=100),
        'runs':runs,'providers':providers,'latest_report':next(iter(decoded(db,'reports',limit=1)),None),
        'status':{'health':'ENTRIES_PAUSED' if m.get('entries_paused') else 'SETUP_BLOCKED' if blockers else 'DEGRADED' if overdue or provider_errors else 'PAPER_RESEARCH','model_gate':m.get('model_gate',{'enabled':False}),
            'error_count':sum(r['state']=='ERROR' for r in runs),'finished_at':max((r['finished_at'] or 0 for r in runs),default=None),
            'source_coverage':'Finalized direct classic-SPL Raydium CP purchases; latest 20 signatures per configured wallet; incomplete historical coverage',
            'blockers':blockers,'overdue_workers':overdue,'worker_completions':worker_completions,'provider_errors':provider_errors},
        'limits':['TypeSafe action probability is not profit probability.','Cost scenarios are not executable quotes.',
            'Reserved rent remains capital, never an assumed refund.','Missing marks/outcomes remain unknown.',
            'Timed holder/social/controller/exit-route evidence requires an authorized, account-bound factual producer.']}


def view(root,now=None):
    path=folder_for(root)/'book.sqlite3'
    if not path.exists():return {'bot':'nitrogen','title':'NITROGEN','purpose':'Jev Judgment Desk','slot':7,'initialized':False,'account_id':None,'assets':[],'status':{'health':'NOT_INITIALIZED'}}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        observed=int(time.time()) if now is None else now
        result=snapshot(db,observed)
        from .cockpit import project
        try:result['cockpit']=project(db,root,result,observed)
        except (OSError,ValueError,sqlite3.Error):result['cockpit']={'account_id':result['account_id'],'unavailable':True,'notes':['Cockpit projection unavailable; account data retained.']}
        return result


def health(root):
    try:
        v=view(root);return {'available':v['initialized'],'account_id':v['account_id'],'slot':7,'last_completed_at':v['status'].get('finished_at')}
    except (ValueError,OSError,sqlite3.Error,KeyError):return {'available':False,'account_id':None,'slot':7,'last_completed_at':None}
