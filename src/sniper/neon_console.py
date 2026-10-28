"""Read-only Neon console mapping; no writable account or provider construction."""
import json
from decimal import Decimal as D
import time
from .neon import POLICY, paper_snapshot


def view(root):
    from .console import reader, read_json, rows, queue
    folder=root/'neon_bot/runtime';now=int(time.time())
    saved=read_json(folder/'status.json')
    with reader(folder/'book.sqlite3') as db:
        identity=json.loads(db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
        policy=json.loads(db.execute("SELECT value FROM metadata WHERE key='neon_policy'").fetchone()[0])
        if identity['mode']!='paper' or policy!=POLICY or identity['account_id']!=saved['account_id']:
            raise ValueError('Neon account/policy identity mismatch')
        fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM fills ORDER BY at,id')]
        positions=[json.loads(r[0]) for r in db.execute('SELECT payload FROM positions')]
        runs=rows(db,'SELECT job,started,finished,status FROM runs ORDER BY started DESC LIMIT 30')
        checks=rows(db,'SELECT at,verdict,stage,reason,payload FROM leader_decisions ORDER BY id DESC LIMIT 80')
        reasons=rows(db,'SELECT verdict,reason,count(*) count FROM leader_decisions GROUP BY verdict,reason ORDER BY count DESC')
        equity=[json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_equity ORDER BY at DESC LIMIT 12000')][::-1]
        gaps=rows(db,'SELECT from_at AS "from",to_at AS "to",to_at-from_at seconds FROM neon_monitoring_gaps ORDER BY id')
        candidate_rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_candidates ORDER BY at DESC LIMIT 30')]
    api=saved.get('api_usage',{})
    snapshot=paper_snapshot(fills,positions,now,api.get('billed_usd','0'),api.get('known',False))
    by_id={p['id']:p for p in positions}
    for p in snapshot['positions']:
        original=by_id[p['id']];entry=D(p['entry_price']);basis=D(p['cost_basis'])
        mark=(original.get('liquidation_cost_estimate') or {}).get('reference_price')
        p.update(qty=p['quantity'],remaining_basis=p['cost_basis'],liquidation_value=p['liquidation_value_usd'],
            mark_status='missing_quote' if p['quote_unavailable'] else 'ok',mark_at=p['last_quote_at'],mark_price=mark,
            unrealized_pct=str(D(p['unrealized_pnl'])/basis*100) if basis else '0',
            tp_done=p['steps']>0,stop_price=str(entry*D('.65')),take_price=str(entry*2),
            trailing_price=str(D(p['peak'])*D('.7')),mark_liquidity=None)
    display_fills=[]
    for f in fills:
        costs=f.get('execution_costs') or {};buy=f['kind']=='buy';amount=D(f['quantity'])*D(f['price'])
        cash=-(D(costs.get('reference_notional_usd',amount))+D(f['fee'])+D(costs.get('rent_capital_reserved_usd','0'))) if buy else amount-D(f['fee'])
        display_fills.append({**f,'asset_id':f['mint'],'symbol':f['mint'][:8],'observed_at':f['at'],
            'action':'OPEN' if buy else 'PARTIAL' if f['reason'].startswith('take_profit') else 'CLOSE',
            'fill_price':f['price'],'reference_price':costs.get('reference_price'),
            'cash_delta':str(cash),'friction_usd':costs.get('embedded_friction_usd','0'),'gas_usd':f['fee']})
    snapshot['fees_paid']=str(sum((D(f['friction_usd'])+D(f['gas_usd']) for f in display_fills),D(0)))
    assets={p['mint']:{'asset_id':p['mint'],'pool_id':p['pool'],'symbol':p['mint'][:8],
        'state':'open' if D(p['remaining'])>0 else 'closed'} for p in positions}
    latest=max((r['finished'] or 0 for r in runs),default=0)
    blockers=list(saved.get('blockers',[]))
    if now-saved['generated_at']>180:blockers.append('Neon worker status is stale')
    decisions=[]
    for r in checks:
        payload=json.loads(r['payload']);mint=payload.get('mint','')
        decisions.append({**r,'asset_id':mint,'symbol':mint[:8],'observed_at':r['at']})
    return {'account_id':identity['account_id'],'name':POLICY['name'],'chain':'Solana Mainnet · Neon',
        'status':{'finished_at':latest,'trial_state':saved['trial_state']},'snapshot':snapshot,
        'trial':saved.get('trial') or {},'policy':{'time_stop_seconds':21600,'time_stop_min_gain_pct':20},
        'assets':list(assets.values()),'fills':display_fills[::-1], 'closed':[],
        'equity_history':[{'observed_at':r['as_of'],'equity':r['equity']} for r in equity] if saved.get('trial') else [],
        'screens':{},'decisions':decisions,'reasons':reasons,'runs':runs,'event_counts':saved.get('observations',{}),
        'gaps':gaps,'policy_revisions':[],'market':[],'notifications':{'trades':queue(folder/'notifications.sqlite3')},
        'cadence_seconds':60,'fill_count':len(fills),'setup_blocked':not saved.get('trial'),
        'blocker':'; '.join(blockers),'blockers':blockers,'candidates':candidate_rows,'selection':saved['selection'],
        'sources':saved.get('sources',[]),'source_scope':POLICY['source_scope'],
        'fill_model':'Cost-aware simulated fills; no wallet executions','paid_ai_enabled':saved['model']['enabled'],
        'research':{'candidate_count':len(candidate_rows),'approved_wallet_count':len(saved['selection']['leaders'])},
        'error_count':sum(r['status']=='error' for r in runs[:2])}


def chart(root,asset,pool):
    from .console import reader
    from .adapters.solana import validate_address
    validate_address(asset);validate_address(pool)
    with reader(root/'neon_bot/runtime/book.sqlite3') as db:
        points=[json.loads(r[0]) for r in db.execute('SELECT payload FROM neon_marks WHERE mint=? AND pool=? ORDER BY at DESC LIMIT 12000',(asset,pool))][::-1]
        fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM fills WHERE json_extract(payload,\'$.mint\')=? AND json_extract(payload,\'$.pool\')=? ORDER BY at',(asset,pool))]
    markers=[{'observed_at':f['at'],'action':'OPEN' if f['kind']=='buy' else 'PARTIAL' if f['reason'].startswith('take_profit') else 'CLOSE',
        'fill_price':f['price'],'quantity':f['quantity'],'pnl':f['pnl'],'reason':f['reason']} for f in fills]
    return {'asset_id':asset,'pool_id':pool,'points':points,'fills':markers,'sample_limit':12000,
        'generated_at':int(time.time()),'source':'Recorded pinned-pool Neon watcher observations; ten-minute checks, gaps unfilled'}
