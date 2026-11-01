"""Isolated paper ledger, durable candidate cycles and atomic operator approvals."""
from contextlib import closing, nullcontext
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

from rug_desk.book import atomic, process_lock
from sniper.krypton.paper import Paper as BasePaper, bound_config as base_config, day
from sniper.krypton.checks import hard_checks, all_pass
from sniper.execution_costs import simulate
from .config import validate, number, digest, encode
from . import dossier, judge


def folder_for(root):
    folder=Path(root).resolve()/'nitrogen_bot/runtime'
    if folder.resolve()!=folder: raise ValueError('NITROGEN runtime may not redirect to another account')
    return folder


def bound_config(root):
    return validate(base_config(folder_for(root)))


class Book(BasePaper):
    account_kind='nitrogen'

    def __init__(self,root,cfg,*,initialize=False,demo=False,clock=time.time):
        self.root=Path(root).resolve();folder=folder_for(root);cfg=validate(cfg)
        path=folder/'book.sqlite3'
        if path.is_symlink(): raise ValueError('NITROGEN database may not redirect')
        if path.exists():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as check:
                row=check.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()
                if not row or json.loads(row[0])!='nitrogen': raise ValueError('NITROGEN cannot adopt another bot ledger')
        with (process_lock(folder/'init.lock') if initialize else nullcontext()):
            super().__init__(folder,cfg,initialize=initialize,demo=demo,clock=clock)
            try:
                if initialize:
                    self.db.executescript('''
                    CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY,lead_id TEXT NOT NULL,mint TEXT NOT NULL,pool TEXT NOT NULL,first_seen INTEGER NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL,watch_count INTEGER NOT NULL,due_at INTEGER,last_dossier TEXT,UNIQUE(mint,pool));
                    CREATE TABLE IF NOT EXISTS assessments(id TEXT PRIMARY KEY,lead TEXT NOT NULL,started INTEGER NOT NULL,finished INTEGER,status TEXT NOT NULL,dossier TEXT NOT NULL,response TEXT,result TEXT NOT NULL,reason TEXT NOT NULL,watch_count INTEGER NOT NULL,request_id TEXT);
                    CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,lead TEXT UNIQUE NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,approved_at INTEGER,filled_at INTEGER);
                    CREATE TABLE IF NOT EXISTS observations(hash TEXT PRIMARY KEY,mint TEXT NOT NULL,pool TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS purchases(signature TEXT PRIMARY KEY,wallet TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS outcomes(assessment TEXT PRIMARY KEY,due_at INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS closed_trades(order_id TEXT PRIMARY KEY,closed_at INTEGER NOT NULL,payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
                    ''')
                    if self.get('nitrogen_policy') is None:
                        self.set('nitrogen_policy',judge.BINDING);self.set('cost_policy',cfg['cost_policy']);self.costs=cfg['cost_policy']
                if self.get('nitrogen_policy')!=judge.BINDING or self.costs!=cfg['cost_policy']: raise ValueError('NITROGEN policy migration required')
            except BaseException:self.close();raise

    def enter(self,*args,**kwargs):
        raise ValueError('NITROGEN requires a durable order and explicit approve --id')

    def model_costs(self):
        billed=reserved=D(0)
        for raw, in self.db.execute('SELECT payload FROM api_requests'):
            a=json.loads(raw);bill=a.get('usage',{}).get('billed_usd')
            if bill is None:reserved+=number(a.get('reservation_usd','0'))
            else:billed+=number(bill)
        return billed,reserved

    def equity(self,now):
        value,unknown=super().equity(now)
        return value+number(self.get('rent_reserved'))-self.model_costs()[1],unknown

    def daily(self,now):
        equity,unknown=self.equity(now)
        if unknown:
            prior=self.db.execute('SELECT * FROM days WHERE day=?',(day(now),)).fetchone()
            return {'day':day(now),'equity':None,'baseline':prior['baseline'] if prior else self.cfg['starting_cash_usd'],'halted':bool(prior and prior['halted']),'unresolved':True}
        return super().daily(now)

    def available(self):return number(self.get('cash'))-sum(self.model_costs())

    def ticket(self,q,now):
        self.valid_quote(q,now)
        equity,unknown=self.equity(now)
        if unknown: raise ValueError('Unpriced open position blocks new sizing')
        cap=min(self.available(),(equity-number(self.cfg['model_reservation_usd']))*number(self.cfg['max_position_pct']))
        if cap<=0:raise ValueError('Insufficient cash')
        preliminary=simulate(self.costs,'buy',q,cap,now)
        amount=cap-number(preliminary['explicit_fee_usd'])-number(preliminary['rent_capital_reserved_usd'])
        if amount<=0:raise ValueError('Insufficient ticket after costs')
        return amount

    def discover_purchase(self,p,now):
        if p.get('wallet') not in self.cfg['tracked_wallets'] or p.get('source')!='solana_finalized_cp_swap' or p.get('action')!='BUY':raise ValueError('Qualifying tracked purchase required')
        id=dossier.cycle_id(self.identity,p['mint'],p['pool'])
        with self.transaction():
            pinned=self.db.execute('SELECT pool FROM leads WHERE mint=?',(p['mint'],)).fetchone()
            if pinned and pinned[0]!=p['pool']:raise ValueError('Mint already has a pinned candidate cycle; pool changes cannot reset WATCH')
            old=self.db.execute('SELECT payload FROM purchases WHERE signature=?',(p['transaction_signature'],)).fetchone()
            if not old:self.db.execute('INSERT INTO purchases VALUES(?,?,?,?)',(p['transaction_signature'],p['wallet'],p['occurred_at'],encode(p)))
            reason={'kind':'tracked_wallet_purchase','transaction_signature':p['transaction_signature'],'observed_at':p['observed_at'],'source_ref':p['source_ref']}
            inserted=self.db.execute("INSERT OR IGNORE INTO leads VALUES(?,?,?,?,?,?,'SEARCH',0,NULL,NULL)",(id,'N-'+id[:12],p['mint'],p['pool'],now,encode(reason)))
            if inserted.rowcount:self.decision(p['mint'],'SEARCH',{'lead':id,'reason':reason},now)
        return id

    def lead(self,id):
        row=self.db.execute('SELECT * FROM leads WHERE id=?',(id,)).fetchone()
        if not row:raise ValueError('Unknown candidate cycle')
        return dict(row,reason=json.loads(row['reason']))

    def transition(self,id,stage,reason,now,payload=None):
        lead=self.lead(id)
        self.db.execute('UPDATE leads SET state=? WHERE id=?',(stage,id))
        self.decision(lead['mint'],stage,dict(payload or {},lead=id,reason=reason),now)

    def import_evidence(self,value):
        if value.get('account_id')!=self.identity or value.get('demo') is not self.demo:raise ValueError('Account/evidence label mismatch')
        # Full factual validation occurs with current checks and size before admission.
        if type(value.get('snapshot_at')) is not int:raise ValueError('Snapshot timestamp required')
        dossier.cycle_id(self.identity,value['mint'],value['pool'])
        with self.transaction():
            prior=self.db.execute('SELECT hash FROM observations WHERE mint=? AND pool=? AND at=?',(value['mint'],value['pool'],value['snapshot_at'])).fetchone()
            if prior and prior[0]!=digest(value):raise ValueError('Immutable snapshot conflict')
            self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',(digest(value),value['mint'],value['pool'],value['snapshot_at'],encode(value)))

    def recover(self,now):
        with self.transaction():
            for row in self.db.execute("SELECT id,lead FROM assessments WHERE status='IN_FLIGHT' AND started<?",(now-120,)).fetchall():
                self.db.execute("UPDATE assessments SET status='INTERRUPTED',finished=?,reason='ambiguous_request_not_retried' WHERE id=?",(now,row['id']))
                self.transition(row['lead'],'PASS','interrupted_assessment_no_retry',now)
            self.expire_orders(now)

    def expire_orders(self,now):
        for row in self.db.execute("SELECT id,lead FROM orders WHERE status='PENDING' AND expires<=?",(now,)).fetchall():
            self.db.execute("UPDATE orders SET status='EXPIRED' WHERE id=?",(row['id'],))
            self.transition(row['lead'],'EXPIRED','pending_order_expired',now)

    def assess(self,id,e,model,now,clock=None):
        from .model import BudgetBlocked
        clock=clock or self.clock
        lead=self.lead(id)
        if lead['watch_count'] and now<(lead['due_at'] or 0):return 'WATCH_NOT_DUE'
        if lead['state'] not in ('SEARCH','WATCH','EVIDENCE_BLOCKED','WHALE_BLOCKED'):return lead['state']
        if self.get('entries_paused') is True:return 'ENTRIES_PAUSED'
        if now-lead['first_seen']>self.cfg['candidate_expiry_seconds']:
            with self.transaction():self.transition(id,'PASS','candidate_expired',now)
            return 'PASS'
        q=e['quote']
        if q['mint']!=lead['mint'] or q['pool']!=lead['pool'] or e.get('demo') is not self.demo:raise ValueError('Evidence scope mismatch')
        checks=hard_checks(self.cfg,q,e['facts'],e['lp'],now)
        if not all_pass(checks):
            stage='RISK_REJECTED' if any(c['state']=='FAIL' for c in checks) else 'EVIDENCE_BLOCKED'
            with self.transaction():self.transition(id,stage,'risk_failed_or_unknown',now,{'checks':checks})
            return stage
        with self.transaction():self.decision(lead['mint'],'RISK_PASS',{'lead':id,'checks':checks},now)
        if not dossier.whale_checks(e,self.cfg,now):
            with self.transaction():self.transition(id,'WHALE_BLOCKED','qualifying_tracked_purchase_missing',now)
            return 'WHALE_BLOCKED'
        with self.transaction():self.decision(lead['mint'],'WHALE_CONFIRMED',{'lead':id,'purchases':dossier.whale_checks(e,self.cfg,now)},now)
        ticket=self.ticket(q,now);d=dossier.build(self,lead,e,ticket,now);v=dossier.validate(d,self.cfg,now)
        if lead['watch_count']:
            previous=json.loads(lead['last_dossier'])
            if q['observed_at']<lead['due_at'] or not isinstance(d['window'],dict) or d['window']['end']<=previous['window']['end']:
                v['missing'].append('watch_requires_fresh_dossier_after_due')
        if v['missing']:
            with self.transaction():
                self.db.execute('UPDATE leads SET last_dossier=? WHERE id=?',(encode(d),id))
                self.transition(id,'EVIDENCE_BLOCKED','missing: '+', '.join(v['missing']),now)
            return 'EVIDENCE_BLOCKED'
        a_id=digest({'lead':id,'attempt':lead['watch_count'],'policy':judge.BINDING})
        with self.transaction():
            fresh=self.lead(id)
            if fresh['state']!=lead['state'] or fresh['watch_count']!=lead['watch_count']:return fresh['state']
            self.db.execute('INSERT INTO assessments VALUES(?,?,?,NULL,?,?,NULL,?,?,?,NULL)',(a_id,id,now,'IN_FLIGHT',encode(d),'NO_ENTRY','request_not_completed',lead['watch_count']))
            self.db.execute('UPDATE leads SET state=?,last_dossier=? WHERE id=?',('JUDGE',encode(d),id))
        status='COMPLETE';response=None;result='NO_ENTRY';reason='';request_id=None
        try:
            received=model.ask(a_id,dossier.state(d,v))
            request_id=received['request_id'];response=judge.validate(received['response'])
            result,reason=judge.gate(response,lead['watch_count'])
        except BudgetBlocked:status='BUDGET_BLOCKED';reason='shared_or_local_budget_no_call'
        except TimeoutError:status='TIMEOUT';reason='timeout_no_automatic_retry'
        except urllib.error.URLError as exc:
            status='TIMEOUT' if isinstance(exc.reason,TimeoutError) else 'PROVIDER_ERROR';reason='transport_failed_no_retry'
        except PermissionError:status='UNAUTHORIZED';reason='paid_authorization_unavailable_no_call'
        except (ValueError,KeyError,TypeError):status='INVALID_RESPONSE';reason='response_or_request_invalid_no_retry'
        except Exception:status='PROVIDER_ERROR';reason='provider_unavailable_no_retry'
        finished=int(clock())
        if finished<now or finished-now>self.cfg['order_expiry_seconds']:
            status='EXPIRED';result='NO_ENTRY';reason='assessment_finished_too_late'
        with self.transaction():
            row=self.db.execute('SELECT status FROM assessments WHERE id=?',(a_id,)).fetchone()
            if row['status']!='IN_FLIGHT':return 'PASS'
            self.db.execute('UPDATE assessments SET finished=?,status=?,response=?,result=?,reason=?,request_id=? WHERE id=?',
                (finished,status,encode(response) if response else None,result,reason,request_id,a_id))
            self.decision(lead['mint'],'JUDGE',{'lead':id,'assessment':a_id,'status':status,'verdict':result,'reason':reason,'response':response},finished)
            if status=='COMPLETE' and result=='WATCH':
                self.db.execute("UPDATE leads SET state='WATCH',watch_count=1,due_at=? WHERE id=?",(finished+1800,id))
            elif status=='COMPLETE' and result=='BUY':
                payload={'account_id':self.identity,'lead':id,'mint':lead['mint'],'pool':lead['pool'],'assessment':a_id,
                    'dossier_hash':digest(d),'policy_hash':digest(self.cfg),'judge_binding':judge.BINDING,
                    'ticket_usd':str(ticket),'evidence_label':d['evidence_label']}
                order_id='N-'+a_id[:24]
                self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,NULL,NULL)",(order_id,id,finished,min(now+self.cfg['order_expiry_seconds'],finished+self.cfg['order_expiry_seconds']),'PENDING',encode(payload)))
                self.transition(id,'STAGED','APPROVE #'+order_id,finished,{'order_id':order_id})
            else:
                self.transition(id,'PASS',reason,finished)
                if status=='COMPLETE' and result=='PASS':
                    hypothetical=simulate(self.costs,'buy',q,ticket,now)
                    self.db.execute('INSERT INTO outcomes VALUES(?,?,?,?)',(a_id,finished+86400,'PENDING',encode({'entry':hypothetical,'decision_quote':q,'decision_at':finished,'ticket_usd':str(ticket),'mint':lead['mint'],'pool':lead['pool']})))
        return result

    def order(self,id):
        r=self.db.execute('SELECT * FROM orders WHERE id=?',(id,)).fetchone()
        if not r:raise ValueError('Unknown NITROGEN order')
        return dict(r,payload=json.loads(r['payload']))

    def pending(self):return [self.order(r[0]) for r in self.db.execute("SELECT id FROM orders WHERE status='PENDING' ORDER BY created")]

    def reject_order(self,id,now):
        with self.transaction():
            order=self.order(id)
            if order['status']!='PENDING':return order['status']
            self.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?",(id,))
            self.transition(order['lead'],'REJECTED','operator_rejected',now)
        return 'REJECTED'

    def approve(self,id,e,now):
        with self.transaction():
            order=self.order(id);p=order['payload']
            if order['status']!='PENDING':return order['status']
            if now>=order['expires']:
                self.db.execute("UPDATE orders SET status='EXPIRED' WHERE id=?",(id,));self.transition(order['lead'],'EXPIRED','pending_order_expired',now)
                return 'EXPIRED'
            if self.get('entries_paused') is True:raise ValueError('New entries paused')
            if p['account_id']!=self.identity or p['policy_hash']!=digest(self.cfg) or p['judge_binding']!=judge.BINDING:raise ValueError('Order policy/account binding invalid')
            a=self.db.execute('SELECT * FROM assessments WHERE id=?',(p['assessment'],)).fetchone()
            if not a or a['lead']!=order['lead'] or a['status']!='COMPLETE' or a['result']!='BUY' or digest(json.loads(a['dossier']))!=p['dossier_hash'] or judge.gate(json.loads(a['response']),a['watch_count'])[0]!='BUY':raise ValueError('Bound BUY assessment required')
            if not self.demo:
                audit=self.db.execute('SELECT payload FROM api_requests WHERE id=?',(a['request_id'],)).fetchone()
                audit=json.loads(audit[0]) if audit else {}
                if audit.get('state')!='RECEIVED' or audit.get('account_id')!=self.identity or audit.get('assessment')!=a['id'] or audit.get('response')!=json.loads(a['response']):raise ValueError('Recorded NITROGEN request required')
            lead=self.lead(order['lead']);q=e['quote'];ticket=number(p['ticket_usd'],True)
            self.valid_quote(q,now,p['mint'],p['pool'])
            if e.get('demo') is not self.demo:raise ValueError('Synthetic evidence cannot enter production')
            fresh=dossier.build(self,lead,e,ticket,now);checks=dossier.validate(fresh,self.cfg,now)
            if checks['missing']:raise ValueError('Approval evidence missing: '+', '.join(checks['missing']))
            if not dossier.whale_checks(e,self.cfg,now):raise ValueError('Current whale confirmation required')
            if not all_pass(hard_checks(self.cfg,q,e['facts'],e['lp'],now)):raise ValueError('Current hard risk failed')
            old=json.loads(a['dossier']);old_price=number(old['observations']['price_now']['value'],True)
            if abs(number(q['price'])/old_price-1)>number(self.cfg['approval_max_price_move_pct']):raise ValueError('Price moved more than approval tolerance; no stale setup fill')
            equity,unknown=self.equity(now)
            if unknown or self.daily(now)['halted']:raise ValueError('Unpriced position or daily loss halt')
            positions=self.positions()
            if len(positions)>=self.cfg['max_open'] or any(x['mint']==p['mint'] for x in positions):raise ValueError('Position count/mint limit')
            buys=[r[0] for r in self.db.execute("SELECT at FROM fills WHERE side='BUY'") if day(r[0])==day(now)]
            if len(buys)>=self.cfg['max_entries_per_day']:raise ValueError('Daily entry cap')
            fill=simulate(self.costs,'buy',q,ticket,now);basis=ticket+number(fill['explicit_fee_usd']);rent=number(fill['rent_capital_reserved_usd'])
            deployed=sum(number(x['basis']) for x in positions)
            if basis+rent>self.available() or basis+rent>equity*number(self.cfg['max_position_pct']) or deployed+basis+rent>equity*number(self.cfg['max_exposure_pct']):raise ValueError('Cash, size or aggregate exposure limit')
            position={'mint':p['mint'],'pool':p['pool'],'symbol':q.get('symbol',''),'order_id':id,'assessment':a['id'],'opened_at':now,
                'quantity':fill['quantity'],'original':fill['quantity'],'basis':str(basis),'entry_price':fill['fill_price'],'entry_liquidity':q['liquidity'],
                'peak':fill['fill_price'],'steps':0,'realized_pnl':'0','rent_reserved':str(rent),'mark':None}
            mark=simulate(self.costs,'sell',q,number(position['quantity']),now)
            position['mark']={'at':now,'net_value':str(max(D(0),number(mark['quantity'])*number(mark['fill_price'])-number(mark['explicit_fee_usd']))),'quote':q}
            self.db.execute('INSERT INTO positions VALUES(?,?)',(p['mint'],encode(position)))
            self.set('cash',str(number(self.get('cash'))-basis-rent));self.set('rent_reserved',str(number(self.get('rent_reserved'))+rent))
            self.db.execute('INSERT INTO fills(at,mint,side,payload) VALUES(?,?,?,?)',(now,p['mint'],'BUY',encode(dict(fill,order_id=id,basis=str(basis),pool=p['pool']))))
            self.db.execute("UPDATE orders SET status='FILLED',approved_at=?,filled_at=? WHERE id=?",(now,now,id))
            self.transition(order['lead'],'FILLED','operator_approved_paper_fill',now,{'order_id':id,'approval_evidence':fresh})
        return 'FILLED'

    def watch(self,q,now):
        with self.transaction():
            row=self.db.execute('SELECT payload FROM positions WHERE mint=?',(q['mint'],)).fetchone()
            if not row:return 'NO_POSITION'
            p=json.loads(row[0]);self.valid_quote(q,now,p['mint'],p['pool'])
            if now<p['opened_at']:raise ValueError('Exit quote predates entry')
            full=simulate(self.costs,'sell',q,number(p['quantity']),now)
            net=number(full['quantity'])*number(full['fill_price'])-number(full['explicit_fee_usd'])
            if net<=0:raise ValueError('Nonpositive exit value; unresolved holdings retained')
            p['mark']={'at':now,'net_value':str(net),'quote':q}
            price=number(q['price']);p['peak']=str(max(number(p['peak']),price));e=self.cfg['exit']
            gain=(price/number(p['entry_price'])-1)*100;qty=number(p['quantity']);reason=None
            if number(q['liquidity'])<number(p['entry_liquidity'])*(1-number(e['liq_drop_pct'])/100):reason='liquidity_drop'
            elif gain<=-number(e['stop_pct']):reason='stop_loss'
            elif p['steps']<len(e['ladder']) and gain>=number(e['ladder'][p['steps']][0]):
                qty=min(qty,number(p['original'])*number(e['ladder'][p['steps']][1]));p['steps']+=1;reason='partial_profit_'+str(p['steps'])
            elif p['steps']>0 and price<=number(p['peak'])*(1-number(e['trail_pct'])/100):reason='trailing_exit'
            elif now-p['opened_at']>=number(e['time_stop_hours'])*3600 and gain<number(e['time_stop_min_pct']):reason='time_exit'
            if reason:
                fill=simulate(self.costs,'sell',q,qty,now);proceeds=qty*number(fill['fill_price'])-number(fill['explicit_fee_usd'])
                if proceeds<=0:raise ValueError('Nonpositive sale; holdings retained')
                allocated=number(p['basis'])*qty/number(p['quantity']);pnl=proceeds-allocated
                p['quantity']=str(number(p['quantity'])-qty);p['basis']=str(number(p['basis'])-allocated);p['realized_pnl']=str(D(p['realized_pnl'])+pnl)
                self.set('cash',str(number(self.get('cash'))+proceeds))
                self.db.execute('INSERT INTO fills(at,mint,side,payload) VALUES(?,?,?,?)',(now,p['mint'],'SELL',encode(dict(fill,order_id=p['order_id'],basis=str(allocated),realized_pnl=str(pnl),reason=reason))))
                self.decision(p['mint'],'EXIT',{'reason':reason,'order_id':p['order_id'],'quantity':str(qty)},now)
                if number(p['quantity'])==0:
                    self.db.execute('DELETE FROM positions WHERE mint=?',(p['mint'],));self.db.execute('INSERT INTO closed_trades VALUES(?,?,?)',(p['order_id'],now,encode(p)))
                else:
                    remainder=simulate(self.costs,'sell',q,number(p['quantity']),now)
                    p['mark']['net_value']=str(max(D(0),number(p['quantity'])*number(remainder['fill_price'])-number(remainder['explicit_fee_usd'])))
                    self.db.execute('UPDATE positions SET payload=? WHERE mint=?',(encode(p),p['mint']))
            else:self.db.execute('UPDATE positions SET payload=? WHERE mint=?',(encode(p),p['mint']))
            self.daily(now)
        return reason or 'HOLD'

    def snapshot(self,now):
        from .view import snapshot
        return snapshot(self.db,now)

    def publish(self):
        with process_lock(self.folder/'publish.lock'):
            self.db.execute('BEGIN')
            try:value=self.snapshot(int(self.clock()))
            finally:self.db.rollback()
            atomic(self.folder/'paper_state.json',encode(value))
            # Presentation-only history: failures cannot change trading or approvals.
            try:
                from .cockpit import record
                record(self,value,int(self.clock()))
            except (OSError,ValueError,sqlite3.Error):
                pass
