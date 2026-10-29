"""Neon: isolated, cost-aware Solana leader-following paper service. No signer."""
from datetime import datetime
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time

from rug_desk import config
from rug_desk.book import Book, atomic, encode, process_lock, day
from rug_desk.grok import Judge
from rug_desk.leader_signals import process_event, load_records, LeaderGate
from rug_desk.model_gate import status as model_status
from rug_desk.report import notify
from rug_desk.traders import Selections
from rug_desk.watch import watch
from .neon_sources import Research, digest

POLICY = {'version':'neon_leader_following_v1','name':'Neon bot',
    'strategy':'Neon — Evidence-Gated Solana Leader Following','mode':'paper',
    'decision_mode':'grok','starting_cash_usd':'1000','trial_seconds':7*86400,
    'signal_poll_seconds':60,'watch_seconds':600,'selection_refresh_seconds':3600,
    'mirror_exits':False,'live_execution_available':False,
    'source_scope':'Public research leaderboard plus finalized direct classic-SPL Raydium CP swaps; not all Solana trading',
    'history_scope':'Complete cost-accounted supplied histories required; leaderboard totals cannot qualify'}


def meta(book,key,default=None):
    row=book.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def paper_snapshot(fills,positions,now,api_cost='0',api_known=True,*,starting_cash=POLICY['starting_cash_usd']):
    baseline=D(starting_cash) if starting_cash is not None else D(0)
    cash=baseline-D(api_cost);rent=D(0);realized=-D(api_cost)
    for f in fills:
        costs=f.get('execution_costs') or {}
        amount=D(f['quantity'])*D(f['price']);fee=D(f['fee'])
        if f['kind']=='buy':
            amount=D(costs.get('reference_notional_usd',amount))
            reserve=D(costs.get('rent_capital_reserved_usd','0'))
            cash-=amount+fee+reserve;rent+=reserve
        elif f['kind']=='sell':cash+=amount-fee
        realized+=D(f['pnl'])
    opened=[];basis=D(0);liquidation=D(0);unknown=0
    for p in positions:
        if D(p['remaining'])<=0:continue
        remaining_basis=D(p['remaining_cost_basis_usd']);basis+=remaining_basis
        valid=not p.get('feed_error') and p.get('net_liquidation_usd') is not None and 0<=now-p.get('last_observed_at',0)<=780
        value=D(p['net_liquidation_usd']) if valid else D(0)
        liquidation+=value;unknown+=not valid
        opened.append({'id':p['id'],'asset_id':p['mint'],'pool_id':p['pool'],'symbol':p['mint'][:8],
            'quantity':p['remaining'],'entry_price':p['entry_price'],'cost_basis':str(remaining_basis),
            'liquidation_value_usd':str(value),'unrealized_pnl':str(value-remaining_basis),
            'last_quote_at':p.get('last_observed_at'),'opened_at':p['opened_at'],'remaining':p['remaining'],
            'quote_unavailable':not valid,'steps':p['steps'],'peak':p['peak']})
    equity=cash+rent+liquidation
    return {'starting_cash':str(baseline) if starting_cash is not None else None,
        'cash':str(cash) if starting_cash is not None else None,'equity':str(equity) if starting_cash is not None else None,
        'net_pnl':str(equity-baseline),'return_pct':str((equity/baseline-1)*100) if baseline>0 else None,
        'realized_pnl':str(realized),'unrealized_pnl':str(liquidation-basis),'position_basis':str(basis),
        'rent_reserved_usd':str(rent),'api_cost_usd':str(api_cost),'api_costs_known':api_known,
        'open_positions':len(opened),'closed_positions':sum(p.get('closed_at') is not None for p in positions),
        'unresolved_positions':unknown,'marks_complete':not unknown and api_known,'positions':opened,'as_of':now}


class NeonBook(Book):
    def __init__(self,root,*,initialize=False):
        super().__init__(root,initialize=initialize,mode='paper')
        if initialize:
            with self.transaction():
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('neon_policy',?)",(encode(POLICY),))
        if meta(self,'neon_policy')!=POLICY:
            self.close();raise ValueError('Neon account policy missing or changed; migration required')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS neon_processed_observations(signature TEXT NOT NULL,wallet TEXT NOT NULL,PRIMARY KEY(signature,wallet));
            CREATE TABLE IF NOT EXISTS neon_equity(at INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS neon_marks(mint TEXT NOT NULL,pool TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(mint,pool,at));
            CREATE TABLE IF NOT EXISTS neon_monitoring_gaps(id INTEGER PRIMARY KEY,job TEXT NOT NULL,from_at INTEGER NOT NULL,to_at INTEGER NOT NULL);
        ''')

    def _event(self,event_id,now,message):
        super()._event(event_id,now,message.replace('RUG DESK','NEON'))

    def _save(self,position):
        super()._save(position)
        quote=position.get('liquidation_cost_estimate') or {}
        if position.get('last_observed_at') and not position.get('feed_error') and quote.get('reference_price'):
            self.db.execute('INSERT OR REPLACE INTO neon_marks VALUES(?,?,?,?)',
                (position['mint'],position['pool'],position['last_observed_at'],encode({'at':position['last_observed_at'],'price':quote['reference_price']})))

    def limits(self,mint,now):
        failure=super().limits(mint,now)
        if failure:return failure
        trial=meta(self,'trial')
        if not trial or now<trial['started_at']:return 'Neon trial not started'
        if now>=trial['ends_at']:return 'Neon entry period ended; exits continue'
        cost=usage(self,self.root.parent)
        if not cost['known']:return 'Neon model costs unreconciled'
        snapshot=paper_snapshot(self.fills(),self.positions(all_positions=True),now,cost['billed_usd'])
        if D(snapshot['cash'])<D(config.TICKET_USD):return 'Neon paper cash insufficient'
        return None

    def open_paper(self,quote,judgment,now,**kwargs):
        # Exact fee/rent-aware capital guard is rechecked in the original entry transaction.
        from .execution_costs import simulate
        costs=simulate(self.cost_policy(),'buy',quote,config.TICKET_USD,now)
        previous=kwargs.get('entry_guard')
        def guard():
            if previous:previous()
            cost=usage(self,self.root.parent)
            if not cost['known']:raise ValueError('Unreconciled model costs')
            cash=D(paper_snapshot(self.fills(),self.positions(all_positions=True),now,cost['billed_usd'])['cash'])
            needed=D(costs['reference_notional_usd'])+D(costs['explicit_fee_usd'])+D(costs['rent_capital_reserved_usd'])
            if needed>cash:raise ValueError('Insufficient Neon paper cash after costs/rent')
        return super().open_paper(quote,judgment,now,**{**kwargs,'entry_guard':guard})


def initialize(app):
    app=Path(app);root=app/'neon_bot'
    book=NeonBook(root,initialize=True)
    try:
        book.enable_costs(int(time.time()));Selections(book);Research(book,app)
        paid=book.runtime/'paid-ai.json'
        if not paid.exists():atomic(paid,encode({'enabled':False,'operator_authorized':False,
            'reason':'User preserved Grok gate and disabled paid calls for Neon'}))
        book.event('setup',int(time.time()),'NEON | PAPER SERVICE INSTALLED\nStrategy: Evidence-Gated Solana Leader Following.\nSeparate virtual $1,000 account; $25 ticket, four positions, six entries/day; original exits and modeled costs.\nGrok remains disabled; qualifying histories are missing. Research can run, but entries and the seven-day trial are blocked. No real-money execution.')
        book.export();publish(book,app,int(time.time()))
        return book.identity
    finally:book.close()


def usage(book,app):
    path=Path(app)/'rug_desk/runtime/ai-budget.sqlite3'
    if not path.exists():return {'billed_usd':'0','known':True,'requests':0}
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_request_accounts'").fetchone():
            return {'billed_usd':'0','known':True,'requests':0}
        rows=db.execute('SELECT u.billed_usd FROM advisory_request_accounts a LEFT JOIN advisory_usage u USING(request_id) WHERE a.account_id=?',(book.identity['account_id'],)).fetchall()
        return {'billed_usd':str(sum((D(r[0]) for r in rows if r[0] is not None),D(0))),
            'known':all(r[0] is not None for r in rows),'requests':len(rows)}
    finally:db.close()


def readiness(book,app,now):
    selection=Selections(book).status(now);model=model_status(book.root,now);blockers=[]
    if not model['enabled']:blockers.append('Grok/X paid gate disabled or funded validation missing')
    if not selection['entry_enabled']:blockers.append('No current eligible, model-selected leaders')
    history=book.runtime/'traders.json'
    if not history.exists():blockers.append('Complete cost-accounted trader histories missing')
    proof=meta(book,'neon_operational_preflight',{})
    if proof.get('account_id')!=book.identity['account_id'] or not all(proof.get(x) for x in ('acceptance_passed','persistence_verified','scheduled_completions_verified')):
        blockers.append('Operational acceptance/persistence/scheduled completion proof incomplete')
    ack=False;outbox=book.runtime/'notifications.sqlite3'
    if outbox.exists():
        db=sqlite3.connect(outbox.as_uri()+'?mode=ro',uri=True)
        try:
            row=db.execute('SELECT state,message_id FROM outbox WHERE token=?',(book.identity['account_id']+':setup',)).fetchone()
            ack=bool(row and row[0]=='sent' and type(row[1]) is int)
        finally:db.close()
    if not ack:blockers.append('Telegram setup acknowledgement missing')
    if not usage(book,app)['known']:blockers.append('Model costs unreconciled')
    for leader in selection['leaders']:
        row=book.db.execute('SELECT checked_at,pending FROM neon_cursors WHERE wallet=?',(leader['wallet'],)).fetchone()
        if not row or not 0<=now-row[0]<=180 or json.loads(row[1]):blockers.append('Selected leader RPC watcher is not current');break
    rpc=book.db.execute("SELECT at,status FROM neon_sources WHERE name='rpc'").fetchone()
    if not rpc or rpc[1]!='OK' or not 0<=now-rpc[0]<=180:blockers.append('Current successful RPC collection required')
    return {'ready':not blockers,'blockers':blockers,'model':model,'selection':selection,
        'telegram_setup_acknowledged':ack,'real_money_enabled':False}


def maybe_start(book,app,now):
    existing=meta(book,'trial')
    if existing:return existing
    ready=readiness(book,app,now)
    if not ready['ready']:return None
    with book.transaction():
        existing=meta(book,'trial')
        if existing:return existing
        trial={'account_id':book.identity['account_id'],'started_at':now,'ends_at':now+POLICY['trial_seconds'],
            'strategy':POLICY['strategy'],'policy':POLICY['version'],'mode':'paper','purpose':'Seven days of operational strategy evidence; no automatic live promotion'}
        book.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode(trial),))
        book._event('trial-start',now,'NEON | SEVEN-DAY PAPER TRIAL STARTED\nReal market observations and simulated fills after modeled costs. No real funds or signing. Deadline is fixed; exits continue afterward.')
    atomic(book.runtime/'trial.json',encode(trial));return trial


def publish(book,app,now):
    ready=readiness(book,app,now);cost=usage(book,app)
    with book.transaction():
        fills=book.fills();positions=book.positions(all_positions=True)
        snapshot=paper_snapshot(fills,positions,now,cost['billed_usd'],cost['known'])
    trial=meta(book,'trial');expired=bool(trial and now>=trial['ends_at'])
    if expired:
        book.event('trial-end',now,'NEON | PAPER ENTRY PERIOD ENDED\nNew entries stopped at the original deadline. Existing positions and exits remain active. Review results, costs, gaps and sample size; no live promotion.')
    runs=[dict(r) for r in book.db.execute('SELECT job,started,finished,status FROM runs ORDER BY started DESC LIMIT 30')]
    sources=[dict(r) for r in book.db.execute('SELECT name,at,status,payload FROM neon_sources')]
    candidates=[json.loads(r[0]) for r in book.db.execute('SELECT payload FROM neon_candidates ORDER BY at DESC LIMIT 30')]
    counts={r[0]:r[1] for r in book.db.execute('SELECT status,count(*) FROM neon_observations GROUP BY status')}
    decisions=[dict(r) for r in book.db.execute('SELECT at,verdict,stage,reason,payload FROM leader_decisions ORDER BY id DESC LIMIT 80')]
    state={'account_id':book.identity['account_id'],'name':POLICY['name'],'strategy':POLICY['strategy'],
        'generated_at':now,'mode':'PAPER','real_money_enabled':False,'live_execution_ready':False,
        'trial':trial,'trial_state':'EXITS_ONLY' if expired else 'ACTIVE' if trial else 'SETUP_BLOCKED',
        'entry_enabled':ready['ready'] and bool(trial) and not expired,'blockers':ready['blockers'],
        'snapshot':snapshot,'selection':ready['selection'],'model':ready['model'],'cost_model':book.cost_policy(),
        'candidate_count':book.db.execute('SELECT count(*) FROM neon_candidates').fetchone()[0],
        'candidates':candidates,'observations':counts,'sources':sources,'runs':runs,'decisions':decisions,
        'fills':fills[-500:],'positions':positions,'policy':POLICY,
        'telegram_setup_acknowledged':ready['telegram_setup_acknowledged'],
        'source_scope':POLICY['source_scope'],'copyfomo_execution_connection':'UNCONNECTED',
        'acceptance_excluded':True,'api_usage':cost}
    with book.transaction():
        book.db.execute('INSERT OR IGNORE INTO neon_equity VALUES(?,?)',(now,encode(snapshot)))
        from .paper_events import observe, emit
        observe(book,now)
        readiness_view={k:state[k] for k in ('trial_state','entry_enabled','blockers')}
        previous=book.db.execute("SELECT payload FROM observation_events WHERE kind='readiness_changed' ORDER BY seq DESC LIMIT 1").fetchone()
        if not previous or json.loads(previous[0])!=readiness_view:
            emit(book,'readiness:'+str(now)+':'+digest(readiness_view),'readiness_changed',now,readiness_view)
    atomic(book.runtime/'status.json',encode(state));return state


def cycle(app,job,*,clock=time.time,source=None,judge=None,market=None,rugcheck=None,send=True):
    app=Path(app);root=app/'neon_bot';now=int(clock())
    if job not in ('signals','watch'):raise ValueError('Unknown Neon paper job')
    with process_lock(root/'runtime'/('worker-'+job+'.lock')):
        book=NeonBook(root)
        try:
            # Acquiring this job's lock proves an older RUNNING row is abandoned.
            abandoned=book.db.execute("SELECT id,started FROM runs WHERE job=? AND status='running'",(job,)).fetchall()
            with book.transaction():
                for row in abandoned:
                    book.db.execute("UPDATE runs SET status='interrupted',finished=?,detail=? WHERE id=?",(now,encode({'reason':'Previous process ended without a completed run'}),row['id']))
                    book.db.execute('INSERT INTO neon_monitoring_gaps(job,from_at,to_at) VALUES(?,?,?)',(job,row['started'],now))
            research=source or Research(book,app,clock=clock)
            model=judge or Judge(root,budget_root=app/'rug_desk',billing_account_id=book.identity['account_id'],clock=clock)
            prior=book.db.execute('SELECT finished FROM runs WHERE job=? AND finished IS NOT NULL ORDER BY started DESC LIMIT 1',(job,)).fetchone()
            cadence=60 if job=='signals' else 600
            if prior and now-prior[0]>cadence+180:
                with book.transaction():book.db.execute('INSERT INTO neon_monitoring_gaps(job,from_at,to_at) VALUES(?,?,?)',(job,prior[0],now))
            with book.run(job,now=now) as run:
                if job=='watch':
                    run['exits']=watch(book,market=market,clock=clock)
                    local=datetime.fromtimestamp(int(clock()),__import__('zoneinfo').ZoneInfo(config.TIMEZONE))
                    if (local.hour,local.minute)>=(21,30):
                        cost=usage(book,app)
                        snapshot=paper_snapshot(book.fills(),book.positions(all_positions=True),int(clock()),cost['billed_usd'],cost['known'])
                        trial=meta(book,'trial')
                        state='SETUP_BLOCKED' if not trial else 'EXITS_ONLY' if int(clock())>=trial['ends_at'] else 'ACTIVE'
                        book.event('daily:'+day(int(clock())),int(clock()),
                            f"NEON | PAPER DAILY SUMMARY\nState: {state}\nFills: {len(book.fills())}; open: {snapshot['open_positions']}\nNet P&L after modeled costs: ${snapshot['net_pnl']}\n"+'; '.join(readiness(book,app,int(clock()))['blockers'])[:1500])
                else:
                    run['leaderboard']=research.refresh_board()
                    records=root/'runtime/traders.json';selection=Selections(book).status(now)
                    # Parse only inside Selections.select so invalid input publishes FAILED,
                    # replacing the previous selection instead of silently retaining it.
                    revision=digest(records.read_bytes().hex()) if records.exists() and records.stat().st_size<=10_000_000 else 'missing-or-oversized'
                    last=meta(book,'neon_history_revision',{})
                    if revision!=last.get('digest') or now-last.get('at',0)>=3600 or selection['status'] in ('NOT_CONFIGURED','STALE','UPDATE_STALLED'):
                        run['selection']=Selections(book).select(lambda:load_records(records) if records.exists() else [],judge=model,clock=clock)
                        with book.transaction():book.db.execute("INSERT OR REPLACE INTO metadata VALUES('neon_history_revision',?)",(encode({'digest':revision,'at':now}),))
                    selection=Selections(book).status(int(clock()))
                    _,run['observed_wallets']=research.observe(selection['leaders'])
                    maybe_start(book,app,int(clock()))
                    def admission(moment):
                        if not model.recurring_enabled():raise LeaderGate('Neon Grok gate disabled')
                        trial=meta(book,'trial')
                        if not trial or not trial['started_at']<=moment<trial['ends_at']:
                            raise LeaderGate('Neon trial not active')
                    run['events']=[]
                    pending=book.db.execute('SELECT o.* FROM neon_observations o LEFT JOIN neon_processed_observations p USING(signature,wallet) WHERE o.status=? AND p.signature IS NULL ORDER BY o.at LIMIT 20',('SUPPORTED_SWAP',)).fetchall()
                    for row in pending:
                        event=json.loads(row['payload'])['event']
                        result=process_event(book,event,clock=clock,market=market,rugcheck=rugcheck,judge=model,
                            event_verifier=research.verify,admission_guard=admission)
                        run['events'].append(result)
                        with book.transaction():book.db.execute('INSERT OR IGNORE INTO neon_processed_observations VALUES(?,?)',(row['signature'],row['wallet']))
                if send:run['notifications']=notify(book,runtime=app/'runtime')
            state=publish(book,app,int(clock()))
            return state
        except Exception:
            publish(book,app,int(clock()))
            raise
        finally:book.close()
