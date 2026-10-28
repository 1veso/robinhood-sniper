"""Automatic Solana strategy/execution orchestration; disabled until deployment review.

Shares the existing Rugcheck, Market, Judge, exits and Telegram outbox. Never
reads paper fills into an execution account. No manual per-order signing.
"""
import argparse
from contextlib import closing
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .execution import AutomaticExecutor,OPEN,RESERVING,encode,digest
from .execution_solana import SolanaJournal,SolanaExecutionAdapter,SolanaRpc,RestrictedSolanaSigner,validate_policy
from .solana_cpmm import require
from .adapters.solana_execution import USDC
from .execution_service import notify_execution


def validate_deployment(policy,root):
    from rug_desk.model_gate import strategy_binding
    p=validate_policy(policy);root=Path(root)
    require(p['deployment_authorized'] is True and not p['acceptance_mode'],'Initial Solana deployment is not authorized')
    for key in ('user_approval_reference','independent_review_reference','wallet_simulation_reference','strategy_review_reference','signer_path'):
        require(isinstance(p.get(key),str) and p[key].strip(),'Missing deployment review: '+key)
    current=strategy_binding(root/'rug_desk');bound=p.get('strategy_binding') or {}
    require(all(bound.get(k)==v for k,v in current.items()),'Strategy identity/configuration changed')
    require(p['capital_raw']>0 and p['max_order_raw']>0 and p['max_tx_gas_wei']>0 and p['daily_loss_raw']>0,'Positive approved capital and limits required')
    require(isinstance(p.get('program_pins'),dict) and len(p['program_pins'])==3,'Reviewed deployment pins required')
    public=(root/'runtime/solana-trading-address.txt').read_text(encoding='utf-8-sig').strip()
    require(public==p['wallet'],'Saved public wallet differs from deployment')
    return p


class SolanaGuardedBackend:
    is_test=False
    def __init__(self,adapter,journal,root,*,market=None,rugcheck=None,judge=None,clock=time.time):
        from rug_desk.market import Market
        from rug_desk.rugcheck import Rugcheck
        from rug_desk.grok import Judge
        self.adapter,self.journal,self.root=adapter,journal,Path(root)
        self.market,self.rugcheck,self.judge=market or Market(costs=True),rugcheck or Rugcheck(),judge or Judge(self.root/'rug_desk')
        self.clock=clock

    def model_evidence(self,intent):
        from rug_desk.grok import validate
        from rug_desk.model_gate import binding
        require(self.judge.recurring_enabled(),'Approved funded model gate unavailable')
        rid=intent.get('entry_evidence',{}).get('model_request_id','')
        require(re.fullmatch(r'rug-desk:[a-f0-9]{32}',rid) is not None,'Missing auditable model identity')
        path=self.root/'rug_desk/runtime/x-responses'/(rid.replace(':','-')+'.json')
        require(path.stat().st_size<=2_000_000,'Model audit too large')
        audit=json.loads(path.read_text(encoding='utf-8'))
        require(audit.get('request_id')==rid and audit.get('purpose')=='token_judgment'
                and audit.get('execution_account_id')==self.journal.policy['account_id']
                and audit.get('validated') is True and audit.get('mint')==intent['token_out']
                and audit.get('binding')==binding(self.root/'rug_desk'),'Model provenance mismatch')
        require(type(audit.get('at'))is int and 0<=int(self.clock())-audit['at']<=self.journal.policy['max_evidence_age_seconds'],'Stale model evidence')
        inputs=audit['request']['input']
        supplied=json.loads(next(i['content'] for i in inputs if i['role']=='user'))
        require(supplied.get('mint')==intent['token_out'] and supplied.get('pool',{}).get('pool')==intent['pool'],'Judgment used another pool')
        answer=validate(audit['response'])
        require(answer['verdict']=='BUY' and D(answer['confidence'])>=D('.70'),'Model did not approve this candidate')
        return {**answer,'judged_at':audit['at'],'request_id':rid}

    def entry_evidence(self,intent):
        from rug_desk.market import check_limits
        from rug_desk.rugcheck import ORDER
        answer=self.model_evidence(intent)
        facts=self.rugcheck.report(intent['token_out'])
        require(facts.get('failure') is None and facts.get('mint')==intent['token_out'] and facts.get('pool')==intent['pool']
                and all(facts.get('checks',{}).get(k) is True for k in ORDER)
                and type(facts.get('observed_at'))is int and 0<=int(self.clock())-facts['observed_at']<=30,'Current mandatory risk/pool evidence failed')
        quote=self.market.quote(intent['token_out'],intent['pool'])
        require(check_limits(quote,int(self.clock())) is None,'Current pool limits failed')
        return {'mint':intent['token_out'],'pool':intent['pool'],'safety_passed':True,'required_provider_response':True,
                'safety_at':int(self.clock()),'liquidity_usd':quote['liquidity'],'model_validated':True,
                'model_at':answer['judged_at'],'model_request_id':answer['request_id'],'model_verdict':'BUY',
                'confidence':answer['confidence'],'risk_digest':digest(facts)}

    def risk_check(self,intent=None):
        """Conservative current USD risk marks; not invented historical USD P&L."""
        j=self.journal;p=j.policy;now=int(self.clock());self.sync_model_usage();native=self.market.native_price()
        require(0<=now-native['sol_observed_at']<=30,'Native risk mark stale')
        fx=D(native['sol_usd']);require(fx.is_finite() and fx>0,'Native risk mark invalid')
        base=D(self.market.client.quote(USDC,now=now).price_usd)
        require(base.is_finite() and base>0,'USDC/USD risk mark missing')
        if intent is not None:require(D(intent['amount_in_raw'])/10**6*base<=25,'Order exceeds the approved USD ticket')
        day=j.day_start(now)
        usage=list(j.db.execute('SELECT billed_usd FROM execution_model_costs WHERE at>=?',(day,)))
        require(all(r[0] is not None for r in usage),'Actual API spending is unresolved')
        fills=[dict(r) for r in j.db.execute('SELECT * FROM execution_fills WHERE observed_at>=?',(day,))]
        pnl=sum(D(f['realized_base_raw'])/10**6*base for f in fills)-sum(D(f['fee_wei'])/10**9*fx for f in fills)-sum(D(r[0]) for r in usage)
        fees=sum(int(o['gas_reservation_wei']) for o in j.orders() if o['status'] in RESERVING)
        reserve=(D(fees)+D(p['max_tx_gas_wei'])*3)/10**9*fx
        require(pnl-reserve>-min(D(50),D(p['daily_loss_raw'])/10**6*base),'Daily loss including network stress reserve reached')
        return {'sol_usd':str(fx),'base_usd':str(base),'marked_at':native['sol_observed_at'],'risk_pnl_usd':str(pnl),'scope':'Current FX risk estimate; native fees remain separately accounted'}

    def sync_model_usage(self):
        path=self.root/'rug_desk/runtime/ai-budget.sqlite3'
        if not path.exists():return
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_request_accounts'").fetchone():return
            rows=list(db.execute('SELECT r.request_id,r.created_at,u.billed_usd,r.reserved_usd FROM advisory_reservations r JOIN advisory_request_accounts a USING(request_id) LEFT JOIN advisory_usage u USING(request_id) WHERE a.account_id=?',(self.journal.policy['account_id'],)))
        with self.journal.transaction():
            for rid,at,billed,reserved in rows:
                for value in (billed,reserved):
                    if value is not None:require(D(value).is_finite() and D(value)>=0,'Invalid model bill evidence')
                prior=self.journal.db.execute('SELECT billed_usd FROM execution_model_costs WHERE request_id=?',(rid,)).fetchone()
                if prior and prior[0] is not None:require(prior[0]==billed,'Conflicting historical model charge')
                self.journal.db.execute('INSERT INTO execution_model_costs VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET billed_usd=excluded.billed_usd',(rid,at,billed,reserved))

    def authorize_sign(self,prepared):
        if not self.journal.signing_authorized(prepared):return False
        i=prepared['intent']
        if i['side']=='buy':self.entry_evidence(i);self.risk_check(i)
        return self.journal.signing_authorized(prepared)

    def authorize_broadcast(self,signed):
        if not self.journal.broadcast_authorized(signed):return False
        i=signed['prepared']['intent']
        if i['side']=='buy':self.entry_evidence(i);self.risk_check(i)
        return self.journal.broadcast_authorized(signed)

    def refresh_intent(self,intent):
        fresh=dict(intent)
        if intent['side']=='buy':fresh['entry_evidence']=self.entry_evidence(intent);self.risk_check(intent)
        fresh.update(self.adapter.quote_swap(intent['token_in'],intent['token_out'],intent['pool'],int(intent['amount_in_raw'])))
        return fresh
    def prepare(self,i):
        if i['side']=='buy':self.entry_evidence(i);self.risk_check(i)
        return self.adapter.prepare(i)
    def sign(self,p):return self.adapter.sign(p)
    def broadcast(self,s):return self.adapter.broadcast(s)
    def reconcile(self,o):return self.adapter.reconcile(o)


def plan_exits(journal,backend,now):
    from rug_desk.watch import exit_decision
    planned,errors=[],[]
    for p in journal.positions():
        try:
            if any(o['status'] in RESERVING and o['intent'].get('position_id')==p['position_id'] for o in journal.orders()):continue
            amount=int(p['remaining_qty_raw']);quote=backend.adapter.quote_swap(p['token'],USDC,p['pool'],amount)
            mark=backend.market.quote(p['token'],p['pool'])
            # Entry/peak use the same USDC-raw/token-raw ratio as actual receipts.
            price=D(quote['quoted_output_raw'])/amount;peak=max(price,D(p['peak_price']))
            journal.update_position_peak(p['position_id'],str(peak),now)
            decision=exit_decision({'entry_price':str(D(p['entry_cost_raw'])/D(p['original_qty_raw'])),
                'peak':str(peak),'remaining':str(amount),'original':p['original_qty_raw'],'entry_liquidity':p['entry_liquidity'],
                'steps':p['tp_step'],'opened_at':p['opened_at']},{'price':str(price),'liquidity':mark['liquidity']},now)
            if not decision:continue
            reason,quantity=decision;quantity=int(quantity)
            if quantity<=0:continue
            if quantity!=amount:quote=backend.adapter.quote_swap(p['token'],USDC,p['pool'],quantity)
            i={**quote,**{k:journal.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},
                'id':f"exit:{p['position_id']}:{p['remaining_qty_raw']}:{reason}:{now}",
                'kind':'swap','side':'sell','position_id':p['position_id'],'reason':reason}
            journal.enqueue(i,int(backend.clock()));planned.append(i['id'])
        except Exception as exc:errors.append({'position_id':p['position_id'],'error_type':type(exc).__name__})
    return planned,errors


def collect_candidate(journal,backend):
    """Original profile discovery, seen once, gated before any paid judgment."""
    from rug_desk.market import check_limits
    from rug_desk.rugcheck import ORDER
    require(backend.judge.recurring_enabled(),'Paid model gate disabled')
    journal.db.execute('CREATE TABLE IF NOT EXISTS execution_sol_candidates(mint TEXT PRIMARY KEY,at INTEGER NOT NULL,result TEXT NOT NULL)')
    for item in backend.market.profiles()[:5]:
        mint=item if isinstance(item,str) else item.get('tokenAddress')
        from .adapters.solana import validate_address
        mint=validate_address(mint);now=int(backend.clock())
        with journal.transaction():
            if journal.db.execute('SELECT 1 FROM execution_sol_candidates WHERE mint=?',(mint,)).fetchone():continue
            journal.db.execute('INSERT INTO execution_sol_candidates VALUES(?,?,?)',(mint,now,encode({'stage':'CLAIMED','verdict':'SKIP'})))
        result={'verdict':'SKIP','stage':'risk'}
        try:
            valuation=backend.risk_check()
            facts=backend.rugcheck.report(mint)
            require(facts.get('failure') is None and facts.get('mint')==mint and all(facts.get('checks',{}).get(k) is True for k in ORDER),'Token risk check failed')
            pool=facts['pool'];mark=backend.market.quote(mint,pool)
            require(check_limits(mark,now) is None,'Pool limits failed')
            size=min(journal.policy['max_order_raw'],int(D(25)/D(valuation['base_usd'])*10**6))
            quote=backend.adapter.quote_swap(USDC,mint,pool,size)
            i={**quote,**{k:journal.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},
               'id':'solana-profile:'+mint,'kind':'swap','side':'buy','reason':'rug_desk_profile'}
            journal._entry_limits(i,int(backend.clock()))
            result['stage']='model'
            answer=backend.judge.ask(mint,facts,mark,execution_account_id=journal.policy['account_id'])
            require(answer.get('validated') is True and answer.get('verdict')=='BUY' and D(answer['confidence'])>=D('.70'),'Model did not approve entry')
            i['entry_evidence']={'model_request_id':answer['request_id']}
            i=backend.refresh_intent(i)
            journal.enqueue(i,int(backend.clock()));result.update(verdict='BUY',stage='QUEUED',order_id=i['id'],fill=False)
        except Exception as exc:result['error_type']=type(exc).__name__
        backend.sync_model_usage()
        with journal.transaction():journal.db.execute('UPDATE execution_sol_candidates SET result=? WHERE mint=?',(encode(result),mint))
        # One candidate per pass keeps the exit worker independent of slow judgment.
        return result
    return {'verdict':'SKIP','stage':'NO_NEW_CANDIDATE'}


def live_backend(root,journal):
    from prepare_live import local_reader
    from .signer_vault import load_signer
    root=Path(root);p=validate_deployment(journal.policy,root);original=local_reader(root/'runtime')
    reader=SolanaRpc(rpc_url=original.rpc_url,api_key=original.api_key,transport=original.transport)
    adapter=SolanaExecutionAdapter(p,reader)
    backend=SolanaGuardedBackend(adapter,journal,root)
    adapter.authorize_broadcast=backend.authorize_broadcast
    adapter.signer=RestrictedSolanaSigner(adapter,lambda:load_signer(Path(p['signer_path']),p),backend.authorize_sign)
    return backend


def cycle(root,*,collect=False):
    from rug_desk.book import process_lock,atomic
    root=Path(root);folder=root/'runtime/executor/solana-live'
    policy=validate_deployment(json.loads((folder/'policy.json').read_text(encoding='utf-8-sig')),root)
    # Separate discovery process prevents a 90-second paid request blocking exits.
    lock=folder/('collector.lock' if collect else 'worker.lock')
    with process_lock(lock),closing(SolanaJournal(folder/'ledger.sqlite3',policy)) as journal:
        backend=live_backend(root,journal)
        if collect:
            result=collect_candidate(journal,backend)
        else:
            worker=AutomaticExecutor(journal,backend)
            first=worker.tick() if any(o['status'] in ('SIGNED','SUBMITTED','UNKNOWN','SIGNING') for o in journal.orders()) else {'status':'IDLE'}
            planned,errors=plan_exits(journal,backend,int(time.time()));second=worker.tick()
            result={'first':first,'second':second,'planned_exits':planned,'errors':errors,
                    'snapshot':journal.snapshot(),'notifications':notify_execution(journal,folder,root/'runtime')}
        result.update(observed_at=int(time.time()),mode='LIVE',execution_target='automatic')
        atomic(folder/('collector-status.json' if collect else 'status.json'),encode(result))
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--once',action='store_true');parser.add_argument('--collect',action='store_true')
    args=parser.parse_args()
    if not args.once:parser.error('--once is required; no deployment or signer is initialized implicitly')
    try:print(encode(cycle(args.root,collect=args.collect)))
    except Exception as exc:
        print(encode({'enabled':False,'error_type':type(exc).__name__,'reason':'Deployment or execution gate failed; inspect local review evidence'}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
