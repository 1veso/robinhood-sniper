"""Account-specific presentation journal and recorded equity; no trading authority."""
from contextlib import closing
from decimal import Decimal as D
import json
import sqlite3
from .book import folder_for
from .config import encode

ROLES = ('SEARCH','RISK','WHALE','SNIPER','EXIT','JUDGE')


def record(book, snapshot, now):
    """Optional presentation sidecar. Never reconstruct historical account equity."""
    path=book.folder/'cockpit.sqlite3'
    if path.is_symlink(): raise ValueError('Cockpit may not redirect')
    with closing(sqlite3.connect(path,timeout=2)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS identity(account TEXT PRIMARY KEY)')
        rows=db.execute('SELECT account FROM identity').fetchall()
        if rows and rows!=[(book.identity,)]: raise ValueError('Cockpit account mismatch')
        db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(book.identity,))
        db.execute('CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL)')
        value={k:snapshot['summary'][k] for k in ('equity','net_pnl','realized_pnl','model_reserved','unresolved_positions')}
        last=db.execute('SELECT at,payload FROM observations ORDER BY id DESC LIMIT 1').fetchone()
        if not last or last!=(now,encode(value)):
            db.execute('INSERT INTO observations(at,payload) VALUES(?,?)',(now,encode(value)))
        db.commit()


def history(root,account):
    path=folder_for(root)/'cockpit.sqlite3'
    if not path.is_file() or path.is_symlink():return []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('SELECT account FROM identity').fetchall()!=[(account,)]:raise ValueError('Cockpit identity mismatch')
        return [dict(json.loads(p),id=i,at=at) for i,at,p in reversed(db.execute('SELECT id,at,payload FROM observations ORDER BY id DESC LIMIT 1500').fetchall())]


def event(row,account,fill_lookup,closed):
    p=json.loads(row['payload']);action=row['action'];role=next((r for prefix,r in [('RISK','RISK'),('WHALE','WHALE'),('JUDGE','JUDGE'),('EXIT','EXIT'),('SEARCH','SEARCH')] if action.startswith(prefix)), 'SNIPER' if action in ('STAGED','FILLED','REJECTED','EXPIRED') else 'JUDGE' if action=='PASS' else 'RISK')
    reason=p.get('reason')
    if isinstance(reason,dict):reason='Tracked on-chain purchase observed'
    if action=='RISK_REJECTED':reason=', '.join(c['name'].replace('_',' ') for c in p.get('checks',[]) if c['state']=='FAIL')+' · rejected'
    if action=='STAGED':reason='Awaiting operator approval'
    if action=='FILLED':reason='Approved paper entry recorded'
    reason=str(reason or {'RISK_PASS':'Required checks passed','WHALE_CONFIRMED':'Tracked purchase confirmed'}.get(action,action)).replace('_',' ')
    fill=next((f for f in fill_lookup.get(p.get('order_id'),[]) if f['at']==row['at'] and (f['side']=='BUY' if action=='FILLED' else f['side']=='SELL' and f['payload'].get('reason')==p.get('reason'))),None) if action in ('FILLED','EXIT') else None
    amount=fill['payload'].get('realized_pnl') if fill else None
    is_closed=action=='EXIT' and any(c['order_id']==p.get('order_id') and c['closed_at']==row['at'] for c in closed)
    title={'STAGED':'Ready for approval','FILLED':'Paper entry filled','REJECTED':'Order rejected','EXPIRED':'Order expired','EXIT':'Position closed' if is_closed else 'Partial take recorded'}.get(action)
    if action=='JUDGE':
        title='Jev rejection' if p.get('status')=='COMPLETE' and p.get('verdict')=='PASS' else None
        reason=(str(p.get('verdict','No answer'))+' · '+reason) if p.get('status')=='COMPLETE' else p.get('status','No answer')+' · '+reason
    return {'seq':row['id'],'id':account+':'+str(row['id']),'account_id':account,'at':row['at'],'role':role,
        'action':action,'mint':row['mint'],'message':reason,'title':title,'order_id':p.get('order_id'),
        'assessment':p.get('assessment'),'amount':amount,'fill_id':fill['id'] if fill else None,
        'outcome':p.get('verdict') or action,'closed':is_closed,'toast':bool(title)}


def project(db,root,s,now):
    account=s['account_id'];rows=[dict(r) for r in db.execute('SELECT * FROM decisions ORDER BY id')]
    fills={}
    for r in db.execute('SELECT * FROM fills ORDER BY id'):
        f=dict(r,payload=json.loads(r['payload']));fills.setdefault(f['payload'].get('order_id'),[]).append(f)
    closed=[dict(r,payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM closed_trades')]
    events=[event(r,account,fills,closed) for r in rows];counts={r:0 for r in ROLES};rejections={};seen=set()
    for row,e in zip(rows,events):
        p=json.loads(row['payload'])
        if row['action'] in ('SEARCH','RISK_REJECTED','WHALE_CONFIRMED','FILLED','EXIT','JUDGE'):counts[e['role']]+=1
        reasons=[]
        if row['action']=='RISK_REJECTED':
            reasons=['quote_freshness' if c['name']=='liquidity' and c.get('value') is not None and D(str(c['value']))>=D(s['config']['min_liquidity_usd']) else c['name'] for c in p.get('checks',[]) if c['state']=='FAIL']
        elif row['action']=='JUDGE' and p.get('status')=='COMPLETE' and p.get('verdict')=='PASS':reasons=[p.get('reason','judge_pass')]
        for reason in reasons:
            key=(p.get('lead'),reason)
            if key not in seen:rejections[reason]=rejections.get(reason,0)+1;seen.add(key)
    vetoes=sum(1 for r in rows if r['action']=='JUDGE' and (p:=json.loads(r['payload'])).get('status')=='COMPLETE' and p.get('verdict')=='PASS')
    tokens={}
    for p in [c['payload'] for c in closed]+s['positions']:
        q=(p.get('mark') or {}).get('quote') or {}
        tokens[p['mint']]={'mint':p['mint'],'symbol':p.get('symbol') or p['mint'][:4]+'…'+p['mint'][-4:], 'price':q.get('price'),'at':q.get('observed_at'),'change_pct':None,'image':None}
    for a in reversed(s['assessments']):
        d=a['dossier'];obs=d['observations'];cur=obs.get('price_now');prev=obs.get('price_then')
        token=tokens.setdefault(d['mint'],{'mint':d['mint'],'symbol':d['mint'][:4]+'…'+d['mint'][-4:],'image':None})
        if isinstance(cur,dict) and cur['observed_at']>=(token.get('at') or 0):
            token.update(price=cur['value'],at=cur['observed_at'],change_pct=float((D(str(cur['value']))/D(str(prev['value']))-1)*100) if isinstance(prev,dict) and D(str(prev['value']))>0 else None)
    cfg=s['config'];labels={'quote_freshness':'Quote stale / future','liquidity':'Liquidity < $'+format(D(cfg['min_liquidity_usd']),',.0f'),'pool_age':'Pool age < '+str(cfg['min_pool_age_minutes'])+'m','max_single_holder':'Largest owner > '+str(D(cfg['max_single_holder'])*100)+'%',
        'top10_share':'Top 10 > '+str(D(cfg['max_top10_share'])*100)+'%','mint_disabled':'Mint authority enabled','freeze_disabled':'Freeze authority enabled','lp_locked':'LP not fully locked','rug_flags':'Dangerous token flags',
        'buy_probability_below_0.70':'P(buy) < 0.70','organic_probability_below_0.65':'P(organic) < 0.65','late_yes_or_tie':'Late yes / tie','displayed_exit_risk_above_3':'Exit risk > 3','second_watch_terminal':'Second WATCH','action_not_buy':'Jev action PASS'}
    kills=[{'rule':k,'label':labels.get(k,k.replace('_',' ')),'count':v} for k,v in sorted(rejections.items(),key=lambda kv:-kv[1])]
    vals=[D(c['payload']['realized_pnl']) for c in closed]
    last_head=next((r for r in s['runs'] if r['kind']=='head'),None)
    recent={kind:next((r for r in s['runs'] if r['kind']==kind),None) for kind in ('scan','watch','head')}
    running=any(r and r['finished_at'] is None and 0<=now-r['started_at']<=cfg['poll_seconds']*3 for r in recent.values())
    worker='paused' if s['status']['health']=='ENTRIES_PAUSED' else 'updating' if running else 'stale' if s['status']['overdue_workers'] else 'waiting' if s['status']['blockers'] else 'updating'
    return {'account_id':account,'cursor':account+':'+str(events[-1]['seq'] if events else 0),'events':list(reversed(events[-100:])),
        'total_events':len(events),'counts':counts,'vetoes':vetoes,'roles':{role:next((e for e in reversed(events) if e['role']==role and e['action']!='PASS'),None) for role in ROLES},
        'tokens':list(tokens.values())[:12],'history':history(root,account),'history_max_gap_seconds':cfg['poll_seconds']*3,
        'period':{'label':'Account lifetime','since':s['created_at']},'rejections':kills,'best_trade':str(max(vals)) if vals else None,'worst_trade':str(min(vals)) if vals else None,
        'worker_state':worker,'last_successful_at':{kind:at for kind,at in db.execute("SELECT kind,max(finished_at) FROM runs WHERE state='OK' GROUP BY kind")},'head_state':'coordinating' if last_head and last_head['finished_at'] is None else 'stale' if last_head and now-(last_head['finished_at'] or 0)>cfg['poll_seconds']*3 else 'waiting' if not last_head else 'blocked' if last_head['state']=='ERROR' else 'report ready' if s['latest_report'] else 'monitoring',
        'observations_available':bool(rows),'pending':db.execute("SELECT count(*) FROM orders WHERE status='PENDING'").fetchone()[0],
        'notes':['Equity observations start when this cockpit is installed; no retrospective curve is inferred.','Ticker symbols use recorded position metadata; unknown symbols use the mint prefix. Token images are unavailable.','Rejection counts are distinct candidate/rule pairs over this account lifetime; unavailable evidence is not a rejection.']}


def journal(root,after=None,before=None,limit=100):
    path=folder_for(root)/'book.sqlite3'
    limit=max(1,min(200,int(limit)))
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,isolation_level=None)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        account=json.loads(db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0])
        kind=json.loads(db.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()[0])
        if kind!='nitrogen':raise ValueError('NITROGEN journal account required')
        seq=0
        if after:
            prefix,tail=after.rsplit(':',1)
            if prefix!=account or not tail.isdigit():raise ValueError('Journal cursor account mismatch')
            seq=int(tail)
        if before is not None:
            rows=db.execute('SELECT * FROM decisions WHERE id<? ORDER BY id DESC LIMIT ?',(int(before),limit)).fetchall()
        elif after:rows=db.execute('SELECT * FROM decisions WHERE id>? ORDER BY id LIMIT ?',(seq,limit)).fetchall()
        else:rows=db.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        fills={}
        for r in db.execute('SELECT * FROM fills'):
            f=dict(r,payload=json.loads(r['payload']));fills.setdefault(f['payload'].get('order_id'),[]).append(f)
        closed=[dict(r) for r in db.execute('SELECT order_id,closed_at FROM closed_trades')]
        events=[event(dict(r),account,fills,closed) for r in rows]
        return {'account_id':account,'prev_cursor':after,'cursor':account+':'+str(max([seq]+[e['seq'] for e in events])), 'events':events,'next_before':min((e['seq'] for e in events),default=None)}
