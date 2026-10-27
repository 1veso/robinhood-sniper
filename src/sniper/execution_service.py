"""Automatic executor orchestration; shadow observation cannot acquire authority."""
from contextlib import closing
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .models import Candidate
from .execution import OPEN, ExecutionJournal, AutomaticExecutor, encode
from .adapters.evm import address, hash32, quantity, WETH

D = Decimal


class OpportunityInbox:
    """Inputs, not fills. Source identity and import cursor survive process exits."""
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS opportunities(id TEXT PRIMARY KEY,observed_at INTEGER NOT NULL,
            candidate TEXT NOT NULL,pool TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OBSERVED',reason TEXT);
            CREATE TABLE IF NOT EXISTS runs(at INTEGER PRIMARY KEY,mode TEXT NOT NULL,payload TEXT NOT NULL);''')
        self.db.commit()

    def close(self): self.db.close()

    def add(self, candidate, pool, source):
        candidate = Candidate.from_dict(candidate) if isinstance(candidate, dict) else candidate
        pool = address(pool)
        identity = source + ':' + candidate.event_id
        self.db.execute('INSERT OR IGNORE INTO opportunities(id,observed_at,candidate,pool,source) VALUES(?,?,?,?,?)',
                        (identity, candidate.observed_at, encode(candidate.as_dict()), pool, source))
        return identity

    def import_paper_inputs(self, root, limit=200):
        """Read candidate inputs for every verdict; never read or mirror paper fills."""
        root = Path(root); folder = root/'runtime/exploration'
        manifest = json.loads((folder/'trial.json').read_text(encoding='utf-8-sig'))
        identity = manifest['account_id']
        with closing(sqlite3.connect((folder/'ledger.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)) as source:
            source.execute('PRAGMA query_only=ON'); source.execute('BEGIN')
            actual = source.execute("SELECT value FROM metadata WHERE key='account_id'").fetchone()
            if not actual or actual[0] != identity: raise ValueError('Paper input account identity mismatch')
            old = self.db.execute("SELECT value FROM metadata WHERE key='paper_account'").fetchone()
            if old and old[0] != identity: raise ValueError('Paper input source was replaced')
            row = self.db.execute("SELECT value FROM metadata WHERE key='paper_cursor'").fetchone()
            cursor = int(row[0]) if row else 0
            batch = source.execute('SELECT rowid,payload FROM decisions WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor,limit)).fetchall()
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('paper_account',?)", (identity,))
                for rowid, raw in batch:
                    payload = json.loads(raw)
                    candidate, quote = payload.get('candidate'), payload.get('quote')
                    if candidate and quote:
                        c = Candidate.from_dict(candidate)
                        if c.metadata.get('source_id') != manifest['source_id']: raise ValueError('Candidate source binding mismatch')
                        self.add(c, quote['pool_id'], 'paper-input:'+identity)
                    cursor = rowid
                self.db.execute("INSERT INTO metadata VALUES('paper_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cursor),))
            remaining = source.execute('SELECT COUNT(*) FROM decisions WHERE rowid>?', (cursor,)).fetchone()[0]
        return {'source_account':identity,'checkpoint':cursor,'rows_read':len(batch),'remaining':remaining,
                'scope':'Candidate inputs only; no paper fills or performance imported'}

    def recent(self, now, limit=20):
        return [dict(r) for r in self.db.execute("SELECT * FROM opportunities WHERE observed_at BETWEEN ? AND ? AND status='OBSERVED' ORDER BY observed_at DESC,id LIMIT ?", (now-900,now,limit))]

    def note(self, identity, status, reason):
        with self.db: self.db.execute('UPDATE opportunities SET status=?,reason=? WHERE id=?',(status,reason,identity))

    def summary(self):
        return {'observed_candidates':self.db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0],
                'states':dict(self.db.execute('SELECT status,COUNT(*) FROM opportunities GROUP BY status'))}


def exit_decision(position, quote, now):
    """Original Robinhood exits, using this executor's reconciled holdings only."""
    qty = int(position['remaining_qty_raw']); original = int(position['original_qty_raw'])
    if not qty or not 0 <= now-int(quote['quote_at']) <= 30: return None
    price = D(quote['quoted_output_raw'])/qty
    entry = D(position['entry_cost_raw'])/original
    peak = max(D(position['peak_price']),price)
    gain = price/entry-1
    reason = None; amount = qty
    if gain <= D('-.35'): reason = 'stop_loss'
    elif position['take_profit_done'] and price <= peak*D('.70'): reason = 'trailing_stop'
    elif not position['take_profit_done'] and gain >= D('1'):
        reason = 'take_profit'; amount = min(qty, original//2)
    elif now-position['opened_at'] >= 21600 and gain < D('.20'): reason = 'time_stop'
    return {'reason':reason,'amount_in_raw':str(amount),'peak_price':str(peak)} if reason and amount else {'peak_price':str(peak)}


class GuardedBackend:
    """Recheck source proof and fresh safety before every unsigned entry/approval."""
    is_test = False
    def __init__(self, adapter, source_path, policy, market, *, clock=time.time, screen=None, journal=None):
        from .adapters.goplus import GoPlusClient
        self.adapter,self.source_path,self.policy,self.market = adapter,Path(source_path),policy,market
        self.clock,self.screen = clock,screen or GoPlusClient().screen
        self.journal=journal
        self._screen_cache={}

    def entry_evidence(self, token, pool):
        from .confirmation_book import ConfirmationBook
        from .exploration import pool_adjusted_screen, observed_pool_screen, observe_creator_balance
        from .rug_screen import enhance_rug_screen
        now = int(self.clock())
        with closing(sqlite3.connect(self.source_path.resolve().as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory = sqlite3.Row;source.execute('PRAGMA query_only=ON');source.execute('BEGIN')
            identity=source.execute("SELECT value FROM evm_metadata WHERE key='identity'").fetchone()
            if not identity or identity[0]!=self.policy['observation_source_id']: raise ValueError('Live observation source mismatch')
            class ReadStore: pass
            store=ReadStore();store.db=source
            book=object.__new__(ConfirmationBook);book.store=store
            proofs=book.recent(token,pool,now=now,age=900,minimum_spend=D('10'))
        if len(proofs)<2: raise ValueError('Current two-transaction recipient quorum missing')
        proofs=proofs[:2]
        ConfirmationBook.confirm(proofs,self.adapter.rpc)
        cached=self._screen_cache.get((token,pool))
        if cached and 0<=now-cached.observed_at<=10:screen=cached
        else:
            screen=enhance_rug_screen(observe_creator_balance(observed_pool_screen(pool_adjusted_screen(self.screen(token),pool),proofs[0]),self.adapter.rpc))
            self._screen_cache[token,pool]=screen
        if any(v is False for v in screen.checks.values()) or screen.checks.get('rug_screen_available') is not True:
            raise ValueError('Current required safety response failed or missing')
        # The initial live policy must explicitly name the checks reviewed for real funds.
        if any(screen.checks.get(k) is not True for k in self.policy['required_live_safety_checks']):
            raise ValueError('Required live safety evidence is incomplete')
        mark=self.market.quote(token,now=int(self.clock()),pool_id=pool)
        if mark.liquidity_usd < D('30000'): raise ValueError('Liquidity below strategy floor')
        return {'safety_passed':True,'required_provider_response':True,'safety_at':screen.observed_at,
            'liquidity_usd':str(mark.liquidity_usd),'checks':screen.checks,
            'unknown_safety_checks':[k for k,v in screen.checks.items() if v is None],
            'buyers':[{'wallet':p['wallet'],'tx_hash':p['tx_hash'],'pool':pool,'token':token,'observed_at':p['observed_at']} for p in proofs]}

    def refresh_intent(self, intent):
        fresh=dict(intent)
        if intent['side']=='buy': fresh['entry_evidence']=self.entry_evidence(intent['token_out'],intent['pool'])
        quote=self.adapter.quote_swap(intent['token_in'],intent['token_out'],intent['pool'],int(intent['amount_in_raw']))
        fresh.update(quote)
        return fresh

    def prepare(self, intent):
        if intent['side']=='buy':
            # This also runs for exact-approval prerequisites. An old permission cannot bypass safety.
            self.entry_evidence(intent['token_out'],intent['pool'])
        return self.adapter.prepare(intent)
    def sign(self, prepared):
        intent=prepared['intent']
        if intent['side']=='buy':
            self.entry_evidence(intent['token_out'],intent['pool'])
            self.check_equity_risk(intent)
        return self.adapter.sign(prepared)
    def broadcast(self, signed): return self.adapter.broadcast(signed)
    def reconcile(self, order): return self.adapter.reconcile(order)

    def check_equity_risk(self, intent):
        """Cost-aware current risk marks, never claimed as historical USD P&L."""
        if self.journal is None:raise ValueError('Live equity journal is required')
        j=self.journal;p=self.policy;now=int(self.clock());unit=D(10)**int(p['base_decimals'])
        base=self.market.quote(p['base_token'],now=now).price_usd
        eth=self.market.quote(WETH,now=now).price_usd
        if min(base,eth)<=0:raise ValueError('Current risk valuation unavailable')
        snapshot=j.snapshot();equity=D(snapshot['cash_raw'])/unit*base
        for position in j.positions():
            quote=self.adapter.quote_swap(position['token'],p['base_token'],position['pool'],int(position['remaining_qty_raw']))
            equity+=max(D(0),D(quote['min_output_raw'])/unit*base-D(p['max_tx_gas_wei'])/10**18*eth*(3 if position['take_profit_done'] else 6))
        # Native expenses are exact wei; current FX is a risk stress valuation, not backfilled realized P&L.
        equity-=D(snapshot['native_fees_wei'])/10**18*eth
        with j.transaction():
            j.db.execute('CREATE TABLE IF NOT EXISTS execution_risk_days(day INTEGER PRIMARY KEY,opening_usd TEXT NOT NULL,halted INTEGER NOT NULL DEFAULT 0)')
            day=now//86400
            j.db.execute('INSERT OR IGNORE INTO execution_risk_days(day,opening_usd) VALUES(?,?)',(day,str(equity)))
            record=j.db.execute('SELECT opening_usd,halted FROM execution_risk_days WHERE day=?',(day,)).fetchone()
            halted=bool(record['halted']) or equity<=0 or equity<=D(record['opening_usd'])*D('.98')
            if halted:j.db.execute('UPDATE execution_risk_days SET halted=1 WHERE day=?',(day,))
            j._set('risk_mark',encode({'at':now,'equity_stress_usd':str(equity),'base_usd':str(base),'native_usd':str(eth),
                'daily_halt':halted,'scope':'Current FX risk marks including actual native fees and bounded exit costs; not historical realized USD P&L'}))
        if halted:raise ValueError('Daily 2 percent equity-loss entry halt')
        amount=D(intent['amount_in_raw'])/unit*base
        pending=sum(int(o['reservation_raw']) for o in j.orders() if o['status'] in OPEN and not o['parent_id'] and o['id']!=intent.get('id') and o['id']!=intent.get('parent_intent_id'))
        basis=sum(int(pos['cost_basis_raw']) for pos in j.positions())
        fee_ceiling=D(p['max_tx_gas_wei'])/10**18*eth
        # Entry reset/approval/swap plus partial and final exit reset/approval/swaps fit inside the loss budget.
        pending_count=sum(o['status'] in OPEN and not o['parent_id'] and o['intent']['side']=='buy' and o['id'] not in (intent.get('id'),intent.get('parent_intent_id')) for o in j.orders())
        future_fees=fee_ceiling*(9+sum(3 if pos['take_profit_done'] else 6 for pos in j.positions())+9*pending_count)
        if amount+9*fee_ceiling>equity*D('.005') or (D(pending+basis)/unit*base+amount+future_fees)>equity*D('.02'):
            raise ValueError('Current equity position/account exposure limit')

    def entry_size(self, identity):
        self.check_equity_risk({'id':identity,'amount_in_raw':'1'})
        mark=json.loads(self.journal._get('risk_mark'));p=self.policy
        # Reserve entry plus partial/final exit reset/approval/swap fees before fixing the amount.
        buffered=D(mark['equity_stress_usd'])*D('.005')-9*D(p['max_tx_gas_wei'])/10**18*D(mark['native_usd'])
        size=int((max(D(0),buffered)/D(mark['base_usd'])*(D(10)**int(p['base_decimals']))).to_integral_value(rounding=ROUND_DOWN))
        return min(int(p['max_order_raw']),size)


def plan_exits(journal, backend, now):
    planned=[];errors=[]
    for position in journal.positions():
        try:
            if any(o['status'] in OPEN and o['intent'].get('position_id')==position['position_id'] for o in journal.orders()): continue
            quote=backend.adapter.quote_swap(position['token'],journal.policy['base_token'],position['pool'],int(position['remaining_qty_raw']))
            result=exit_decision(position,quote,int(backend.clock()))
            if result is None: continue
            journal.update_position_peak(position['position_id'],result['peak_price'],now)
            if not result.get('reason'): continue
            amount=int(result['amount_in_raw'])
            if amount != int(position['remaining_qty_raw']):
                quote=backend.adapter.quote_swap(position['token'],journal.policy['base_token'],position['pool'],amount)
            intent={**quote,**{k:journal.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},
                'id':'exit:'+position['position_id']+':'+str(now), 'side':'sell','kind':'swap',
                'position_id':position['position_id'],'reason':result['reason']}
            journal.enqueue(intent,int(backend.clock()));planned.append(intent['id'])
        except Exception as exc:
            errors.append({'position_id':position['position_id'],'error_type':type(exc).__name__})
    return planned,errors

def collect_live(root, policy, *, seconds=35):
    """Independent collector; never consumes paper account capacity or deadline."""
    from .adapters.evm import RpcClient, DEFAULT_RPC
    from .adapters.evm_store import EvmStore
    from .adapters.solana import DexScreenerClient
    from .confirmation_book import ConfirmationBook
    from .exploratory_source import ExploratorySource, signal_id
    from .models import SafetyEvidence, WalletConfirmation
    root=Path(root);folder=root/'runtime/executor/live';folder.mkdir(parents=True,exist_ok=True)
    endpoint=root/'runtime/observation-rpc-url.txt'
    rpc=RpcClient(endpoint.read_text(encoding='utf-8-sig').strip() if endpoint.exists() else DEFAULT_RPC,timeout=8,max_retries=1)
    started=time.monotonic();now=int(time.time())
    # Initialization is an explicit deployment operation; never repair a lost journal silently.
    path=folder/'events.sqlite3'
    if not path.exists(): raise ValueError('Approved live observation journal is missing')
    with closing(EvmStore(path)) as store,closing(OpportunityInbox(folder/'opportunities.sqlite3')) as inbox:
        if store.identity!=policy['observation_source_id']:raise ValueError('Live collector source identity mismatch')
        book=ConfirmationBook(store);source=ExploratorySource(rpc,store,DexScreenerClient(chain_id='robinhood'))
        with rpc.budget(seconds):
            collected=source.collect(sample_size=16,watch_pools=book.watched_pools(now,900))
            if store.reorgs(4663): raise ValueError('Live observation reorganization requires recovery')
            for swap in source.pending():
                if time.monotonic()-started>seconds-5:break
                proof=source.prove(swap)
                if not proof or D(proof['spent_usd'])<10:
                    source.finish(swap,{'reason':'not_supported_acquisition'});continue
                book.record(signal_id(swap),proof)
                cohort=book.recent(proof['token_address'],proof['pool'],now=int(time.time()),age=900,minimum_spend=D('10'))
                if len(cohort)<2:continue
                asset='eip155:4663/erc20:'+proof['token_address']
                candidate=Candidate(signal_id(swap),asset,proof['token_address'][:10],proof['observed_at'],
                    [WalletConfirmation(p['wallet'],'unreviewed:'+p['wallet'],p['tx_hash'],p['observed_at'],D(p['spent_usd']),True,False) for p in cohort[:2]],
                    SafetyEvidence(asset,int(time.time()),{},'unreviewed:executor-collector'),
                    metadata={'source_id':store.identity})
                with inbox.db: inbox.add(candidate,proof['pool'],'live-source:'+store.identity)
                source.finish(swap,{'reason':'opportunity_recorded_not_ordered'})
        return {'collection':collected,**inbox.summary(),'finished_at':int(time.time())}


def shadow_cycle(root, now=None):
    root=Path(root);now=int(time.time()) if now is None else now
    with closing(OpportunityInbox(root/'runtime/executor/shadow/opportunities.sqlite3')) as inbox:
        imported=inbox.import_paper_inputs(root)
        result={'mode':'SHADOW','execution_target':'automatic','live_execution_ready':False,
            'real_money_enabled':False,'signer_loaded':False,'signing_calls':0,'broadcast_calls':0,
            'started_at':now,'finished_at':int(time.time()),'import':imported,**inbox.summary(),
            'blockers':['Initial live deployment has not been authorized',
                'Dedicated wallet, capital limits and restricted signer have not been provisioned',
                'Wallet-specific simulation and independent live instruction review remain required'],
            'scope':'Observed candidate inputs only. No simulated or real fills; excluded from all strategy performance.'}
        with inbox.db: inbox.db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?)',(now,'SHADOW',encode(result)))
        return result


def validate_deployment(policy):
    """No profitability flag or paper metadata can grant initial live authority."""
    if policy.get('deployment_authorized') is not True or policy.get('acceptance_mode') is True:
        raise ValueError('Explicit initial live deployment is required')
    required=('user_approval_reference','independent_review_reference','observation_source_id','keystore_path')
    if any(not isinstance(policy.get(k),str) or not policy[k].strip() for k in required):
        raise ValueError('Deployment review, observation identity and local signer must be bound')
    if policy.get('chain_id')!=4663 or policy.get('execution_target')!='automatic':raise ValueError('Unsupported execution deployment')
    if policy.get('minimum_buyers')!=2 or D(str(policy.get('min_liquidity_usd'))) < 30000:
        raise ValueError('Deployment must preserve the approved entry revision')
    checks=policy.get('required_live_safety_checks')
    if not isinstance(checks,list) or not checks or 'rug_screen_available' not in checks:
        raise ValueError('Live safety requirements must be explicitly reviewed')
    capital=int(policy['capital_raw'])
    if int(policy['max_order_raw'])>capital//200 or int(policy['max_total_exposure_raw'])>capital//50:
        raise ValueError('Deployment exceeds existing full-position/account loss budgets')
    if int(policy['daily_loss_raw'])>capital//50 or policy['max_open_positions']>4 or policy['max_entries_per_day']>6:
        raise ValueError('Deployment exceeds existing daily/position risk limits')
    if policy.get('max_quote_age_seconds',30)>30 or policy.get('buyer_window_seconds',900)>900:
        raise ValueError('Deployment weakens evidence freshness')
    if policy.get('require_finalized') is not True:raise ValueError('Finalized receipt reconciliation is required')
    return policy


def live_backend(root, journal):
    import os
    from .execution_evm import ExecutionRpcClient,EvmExecutionAdapter,RestrictedEvmSigner
    from .adapters.solana import DexScreenerClient
    p=validate_deployment(journal.policy);root=Path(root)
    endpoint=(root/'runtime/quote-rpc-url.txt').read_text(encoding='utf-8-sig').strip()
    rpc=ExecutionRpcClient(endpoint,timeout=8,max_retries=1,broadcast_enabled=True)
    adapter=EvmExecutionAdapter(p,rpc,authorize_broadcast=journal.broadcast_authorized)
    # Password comes from the dedicated runner environment, never argv, reports or prompts.
    def password():
        value=os.environ.get('SNIPER_EXECUTOR_KEYSTORE_PASSWORD')
        if not value:raise ValueError('Restricted keystore password is unavailable')
        return value
    adapter.signer=RestrictedEvmSigner.encrypted_keystore(adapter,Path(p['keystore_path']),password,journal.signing_authorized)
    return GuardedBackend(adapter,root/'runtime/executor/live/events.sqlite3',p,DexScreenerClient(chain_id='robinhood'),journal=journal)


def notify_execution(journal, folder, runtime):
    """Dedicated durable delivery; finalized receipts are the only fill notices."""
    from .telegram import AlertStore,TelegramClient
    if journal.policy['acceptance_mode']:return {'configured':False,'reason':'Acceptance cannot send live trade notices'}
    runtime,folder=Path(runtime),Path(folder)
    token,recipient=runtime/'telegram-bot-token.txt',runtime/'telegram-recipient.json'
    if not token.exists() or not recipient.exists():return {'configured':False}
    with closing(AlertStore(folder/'notifications.sqlite3')) as outbox:
        old=journal._get('notification_journal')
        if old and old!=outbox.identity:raise ValueError('Execution notification journal identity changed')
        with journal.transaction():journal._set('notification_journal',outbox.identity)
        for notice in journal.notices():
            data=notice['payload']
            message=('SNIPER | AUTOMATIC LIVE EXECUTOR\nAccount: '+journal.policy['account_id']+
                '\nEvent: '+notice['kind']+'\nOrder: '+str(notice.get('order_id') or data.get('order_id',''))+
                '\nTransaction: '+str(data.get('tx_hash','pending'))+
                '\nActual input/output raw: '+str(data.get('input_raw','—'))+' / '+str(data.get('output_raw','—'))+
                '\nActual native fee wei: '+str(data.get('fee_wei','—'))+
                '\nOnly finalized canonical receipts change holdings. Base and native fees remain separate; no unverified net USD P&L.')
            outbox.enqueue(str(notice['id']),{'message':message,'account_id':journal.policy['account_id'],'notice_id':notice['id']},notice['at'])
        counts=outbox.counts()
        if counts.get('pending'):
            destination=json.loads(recipient.read_text(encoding='utf-8-sig'))
            client=TelegramClient(token.read_text(encoding='utf-8-sig').strip());me=client.call('getMe')
            if me.get('id')!=destination['bot_id']:raise ValueError('Execution Telegram bot identity mismatch')
            outbox.bind(destination['bot_id'],destination['chat_id'])
            outbox.deliver(client,destination['chat_id'],lambda p:p['account_id']==journal.policy['account_id'])
        for row in outbox.db.execute("SELECT token FROM outbox WHERE state='sent'"):
            journal.acknowledge_notice(row[0],int(time.time()))
        return {'configured':True,'outbox':outbox.counts()}


def live_cycle(root):
    """One bounded automatic tick. Collector runs independently of this process."""
    root=Path(root);folder=root/'runtime/executor/live'
    policy=validate_deployment(json.loads((folder/'policy.json').read_text(encoding='utf-8-sig')))
    with closing(ExecutionJournal(folder/'ledger.sqlite3',policy)) as journal:
        backend=live_backend(root,journal);worker=AutomaticExecutor(journal,backend);now=int(time.time())
        # Reconciliation and exits always run before entry work and do not use paper holdings.
        progress=worker.tick(now) if any(o['status'] in ('SIGNED','SUBMITTED','UNKNOWN','SIGNING') for o in journal.orders()) else {'status':'IDLE'}
        errors=[]
        exits,exit_errors=plan_exits(journal,backend,int(time.time()))
        errors.extend(exit_errors)
        # Latch each UTC day's equity baseline even if no new candidate arrives.
        risk_allowed=True;risk_error=None
        try:backend.check_equity_risk({'id':'cycle-risk-mark','amount_in_raw':'1'})
        except Exception as exc:risk_allowed=False;risk_error=type(exc).__name__
        progress_after_exits=worker.tick(int(time.time()))
        with closing(OpportunityInbox(folder/'opportunities.sqlite3')) as inbox:
            if risk_allowed and not journal.snapshot()['entry_halt'] and not journal.snapshot()['full_halt'] and not errors:
                for row in inbox.recent(int(time.time()),limit=3):
                    if journal.order(row['id']):continue
                    try:
                        candidate=Candidate.from_dict(json.loads(row['candidate']));token=address(candidate.asset_id.split(':')[-1])
                        if row['source']!='live-source:'+policy['observation_source_id']:raise ValueError('Wrong live signal source')
                        size=backend.entry_size(row['id'])
                        if size<=0:raise ValueError('No safe unreserved order size')
                        quote=backend.adapter.quote_swap(policy['base_token'],token,row['pool'],size)
                        intent={**quote,**{k:journal.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},
                            'id':row['id'],'kind':'swap','side':'buy','reason':'two_wallet_30k',
                            'entry_evidence':backend.entry_evidence(token,row['pool'])}
                        backend.check_equity_risk(intent)
                        journal.enqueue(intent,int(time.time()));inbox.note(row['id'],'QUEUED','Independent live account limits and evidence checked')
                        break
                    except Exception as exc:
                        # Retry only while the source remains fresh; errors are categorized without secrets.
                        errors.append('entry:'+type(exc).__name__)
        delivery=notify_execution(journal,folder,root/'runtime')
        return {'mode':'LIVE','real_money_enabled':True,'execution_target':'automatic','started_at':now,
            'finished_at':int(time.time()),'step':progress,'exit_step':progress_after_exits,
            'planned_exits':exits,'errors':errors,'risk_entry_allowed':risk_allowed,'risk_gate':risk_error,
            'snapshot':journal.snapshot(),'notifications':delivery}
