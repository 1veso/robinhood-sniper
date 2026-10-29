"""Bounded factual observations for the existing Solana paper admission path.

This module has no signer, trade, model or notification method. Provider holder
counts are provider observations. Distinct wallets never prove independent
controllers. Pool math is a quote, not wallet-specific simulation or a fill.
"""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .adapters.solana import (DexScreenerClient, RugcheckClient, request_budget,
                              validate_address)
from .adapters.solana_execution import SolanaExecutionReader, USDC, MAINNET_GENESIS
from . import solana_cpmm as cp

REVISION = 'crew-factual-observations-v1'
PIN_SCOPE = 'CREW_READ_ONLY_PROGRAM_REVIEW'
MAX_AGE = 30


def plain(value):
    if isinstance(value, Decimal):
        if not value.is_finite(): raise ValueError('Nonfinite evidence')
        return str(value)
    if isinstance(value, dict): return {k:plain(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [plain(v) for v in value]
    return value


def encode(value):
    return json.dumps(plain(value),sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value): return hashlib.sha256(encode(value).encode()).hexdigest()


def number(value, *, positive=False):
    if isinstance(value,bool) or value is None: raise ValueError('Missing numeric evidence')
    out=Decimal(str(value))
    if not out.is_finite() or out < 0 or (positive and out == 0): raise ValueError('Invalid numeric evidence')
    return out


def unknown(reason, status='UNKNOWN'):
    return {'status':status,'reason':reason}


class EvidenceStore:
    """Additive immutable journal, separate from all financial books."""
    def __init__(self, root):
        self.root=Path(root);self.folder=self.root/'runtime/crew'
        self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'evidence.sqlite3'
        self.db=sqlite3.connect(self.path,timeout=1);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bindings(account_id TEXT PRIMARY KEY,config_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,
                mint TEXT NOT NULL,pool TEXT NOT NULL,observed_at INTEGER NOT NULL,
                received_at INTEGER NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS evidence_scope ON evidence(account_id,mint,pool,observed_at,received_at);''')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('source_id',?)",(str(uuid.uuid4()),))
        self.source_id=self.db.execute("SELECT value FROM metadata WHERE key='source_id'").fetchone()[0]

    def close(self): self.db.close()

    def put(self, payload):
        if payload.get('source_id')!=self.source_id or payload.get('producer_revision')!=REVISION:
            raise ValueError('Evidence source binding differs')
        account,config=payload.get('account_id'),payload.get('config_sha256')
        if not isinstance(account,str) or not account or not isinstance(config,str) or not config:
            raise ValueError('Account and configuration binding required')
        for key in ('mint','pool'): validate_address(payload[key])
        observed,received=payload['observed_at'],payload['received_at']
        if type(observed)is not int or type(received)is not int or not 0<=observed<=received:
            raise ValueError('Invalid evidence times')
        identifier=digest(payload);body=encode(payload)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?)',(account,config))
            if self.db.execute('SELECT config_sha256 FROM bindings WHERE account_id=?',(account,)).fetchone()[0]!=config:
                raise ValueError('Source account configuration changed')
            self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)',
                (identifier,account,payload['mint'],payload['pool'],observed,received,body))
            if self.db.execute('SELECT payload FROM evidence WHERE id=?',(identifier,)).fetchone()[0]!=body:
                raise ValueError('Immutable evidence differs')
        return {'id':identifier,'source_ref':'rug_desk:crew:'+identifier,'payload':json.loads(body)}

    def available(self, account, mint, pool, snapshot_at, *, limit=256):
        if not 1<=limit<=512: raise ValueError('Bounded evidence limit required')
        rows=self.db.execute('''SELECT id,payload FROM evidence WHERE account_id=? AND mint=? AND pool=?
            AND observed_at<=? AND received_at<=? ORDER BY observed_at DESC,received_at DESC,id LIMIT ?''',
            (account,mint,pool,snapshot_at,snapshot_at,limit)).fetchall()
        result=[]
        for row in rows:
            payload=json.loads(row['payload'])
            if digest(payload)!=row['id'] or payload['source_id']!=self.source_id:
                raise ValueError('Evidence integrity check failed')
            result.append(payload)
        return result

    def records_for(self, dossier):
        """Only exact hashed records referenced by this dossier, without lookahead."""
        out=[]
        for ref in dossier.get('source_refs',{}):
            if not ref.startswith('rug_desk:crew:'): continue
            identifier=ref.removeprefix('rug_desk:crew:')
            row=self.db.execute('SELECT payload FROM evidence WHERE id=?',(identifier,)).fetchone()
            if row is None: raise ValueError('Referenced producer evidence missing')
            payload=json.loads(row[0])
            if (digest(payload)!=identifier or payload['source_id']!=self.source_id or payload['account_id']!=dossier['account_id']
                or payload['mint']!=dossier['mint'] or payload['pool']!=dossier['pool']
                or payload['received_at']>dossier['snapshot_at']):
                raise ValueError('Referenced producer evidence binding differs')
            out.append(payload)
        return out


class ReadOnlyRpc(SolanaExecutionReader):
    ALLOWED_RPC=SolanaExecutionReader.ALLOWED_RPC|{'getMultipleAccounts','getBlockTime','getBlock','getTokenLargestAccounts'}


class Producer:
    def __init__(self, root, *, store=None, market=None, rugcheck_client=None,
                 rpc=None, usd_market=None, clock=time.time, budget_seconds=10,
                 max_rpc_calls=20, program_review=None):
        if not 0<budget_seconds<=25 or type(max_rpc_calls)is not int or not 1<=max_rpc_calls<=20:
            raise ValueError('Bounded producer budget required')
        self.root=Path(root);self.store=store;self.clock=clock
        self.market,self.rugcheck=market,rugcheck_client
        self.rpc,self.usd_market=rpc,usd_market
        self.budget_seconds,self.max_rpc_calls=budget_seconds,max_rpc_calls
        self.program_review=program_review

    def _check(self):
        if self.clock()>=self.deadline: raise TimeoutError('Evidence collection budget expired')

    def _rpc(self,method,params=None):
        self._check()
        if self.rpc_calls>=self.max_rpc_calls: raise TimeoutError('Evidence RPC request bound reached')
        self.rpc_calls+=1
        result=self.rpc.rpc(method,params or [])
        self._check()
        return result

    def _batch(self,keys,minimum=0):
        keys=list(dict.fromkeys(keys))
        if len(keys)>24: raise ValueError('Account snapshot bound exceeded')
        value=self._rpc('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':minimum}])
        slot=value.get('context',{}).get('slot')
        if type(slot)is not int or slot<minimum or not isinstance(value.get('value'),list) or len(value['value'])!=len(keys):
            raise ValueError('Incomplete finalized account snapshot')
        return dict(zip(keys,value['value'])),slot

    def _reader(self):
        if self.rpc is None:
            path=self.root/'runtime/solana-rpc-url.txt'
            self.rpc=ReadOnlyRpc(rpc_url=path.read_text(encoding='utf-8-sig').strip() if path.is_file() else 'https://api.mainnet-beta.solana.com')

    def _holders(self,mint):
        """At most twenty accounts; only a full supply reconciliation is complete.

        This counts distinct positive classic-SPL account owners. It never
        excludes infrastructure or equates owner count with controller count.
        """
        if self._rpc('getGenesisHash')!=MAINNET_GENESIS:raise ValueError('Solana mainnet identity required')
        largest=self._rpc('getTokenLargestAccounts',[mint,{'commitment':'finalized'}])
        first=largest.get('context',{}).get('slot');rows=largest.get('value')
        if type(first)is not int or not isinstance(rows,list) or not 0<len(rows)<=20:
            raise ValueError('Bounded largest-account response required')
        addresses=[validate_address(x['address']) for x in rows]
        if len(set(addresses))!=len(addresses) or mint in addresses:raise ValueError('Duplicate holder account')
        accounts,slot=self._batch([mint,*addresses],first)
        raw=cp.blob(accounts[mint],cp.TOKEN,82)
        decimals=raw[44];cp.mint_account(accounts[mint],decimals)
        supply=cp.u64(raw,36)
        if supply<=0:raise ValueError('Positive minted supply required')
        total=0;owners=set();covered=[]
        for address in addresses:
            token=cp.blob(accounts[address],cp.TOKEN,165)
            if cp.key(token,0)!=mint or token[108] not in (1,2):raise ValueError('Holder mint/account state differs')
            amount=cp.u64(token,64);total+=amount
            owner=cp.key(token,32)
            covered.append({'address':address,'owner':owner,'balance_raw':str(amount)})
            if amount:owners.add(owner)
        stamp=self._rpc('getBlockTime',[slot])
        if type(stamp)is not int or not 0<=int(self.clock())-stamp<=MAX_AGE:raise ValueError('Holder snapshot stale')
        block=self._rpc('getBlock',[slot,{'commitment':'finalized','transactionDetails':'none','rewards':False}])
        if not isinstance(block,dict) or not isinstance(block.get('blockhash'),str) or block.get('blockTime')!=stamp:
            raise ValueError('Canonical holder slot unavailable')
        context={'slot':slot,'block_hash':block['blockhash'],'block_time':stamp,'commitment':'finalized',
                 'accounts_sha256':digest(accounts),'minted_supply_raw':str(supply),'covered_balance_raw':str(total),
                 'accounts_examined':len(addresses),'covered_accounts':covered,'positive_owners':sorted(owners),
                 'holder_definition':'distinct owners of positive classic-SPL token balances; infrastructure included'}
        if total!=supply:
            return {'status':'UNKNOWN','reason':'largest_accounts_do_not_cover_complete_supply','context':context}
        recheck=self._rpc('getBlock',[slot,{'commitment':'finalized','transactionDetails':'none','rewards':False}])
        if recheck.get('blockhash')!=block['blockhash']:raise ValueError('Canonical holder slot changed')
        return {'status':'VALID','value':len(owners),'observed_at':stamp,'received_at':int(self.clock()),
                'source':'solana_finalized_supply_reconciled_accounts','independently_verified':True,
                'source_observation_time_known':True,'context':context,'controller_independence':'UNKNOWN'}

    def _pool(self,identity,mint,pool,ticket,reference_price):
        if self._rpc('getGenesisHash')!=MAINNET_GENESIS: raise ValueError('Solana mainnet identity required')
        initial,first=self._batch([pool,cp.PROGRAM,cp.TOKEN,cp.ATA])
        state=cp.pool_state(initial[pool],int(self.clock()))
        if {state['mint0'],state['mint1']}!={mint,USDC}: raise ValueError('Only direct classic-SPL CP/USDC supported')
        extra=[]
        for program in (cp.PROGRAM,cp.TOKEN,cp.ATA):
            raw=cp.blob(initial[program],executable=True)
            if initial[program]['owner']==cp.UPGRADEABLE:
                if len(raw)!=36 or raw[:4]!=b'\2\0\0\0': raise ValueError('Unsupported program loader')
                extra.append(cp.key(raw,4))
        keys=[pool,cp.PROGRAM,cp.TOKEN,cp.ATA,*extra,state['config'],state['observation'],
              state['vault0'],state['vault1'],mint,USDC]
        accounts,slot=self._batch(keys,first)
        stamp=self._rpc('getBlockTime',[slot])
        if type(stamp)is not int or not 0<=int(self.clock())-stamp<=MAX_AGE: raise ValueError('Stale finalized state')
        block=self._rpc('getBlock',[slot,{'commitment':'finalized','transactionDetails':'none','rewards':False}])
        if not isinstance(block,dict) or not isinstance(block.get('blockhash'),str) or block.get('blockTime')!=stamp:
            raise ValueError('Canonical slot identity unavailable')
        # A public program address is only a placeholder for pure ATA derivation;
        # no user wallet is read and no wallet-specific execution is asserted.
        state=cp.inspect_pool(pool,USDC,mint,cp.PROGRAM,accounts,int(self.clock()))
        pins={p:cp.deployment_observation(p,accounts) for p in (cp.PROGRAM,cp.TOKEN,cp.ATA)}
        context={'slot':slot,'block_hash':block['blockhash'],'block_time':stamp,
                 'commitment':'finalized','accounts_sha256':digest(accounts),'program_observations':pins}
        review=self.program_review
        if review is None:
            path=self.root/'runtime/crew/program-review.json'
            if path.is_file(): review=json.loads(path.read_text(encoding='utf-8-sig'))
        reviewed=(isinstance(review,dict) and review.get('scope')==PIN_SCOPE
            and review.get('account_id')==identity['account_id'] and review.get('config_sha256')==identity['config_sha256']
            and isinstance(review.get('review_reference'),str) and bool(review['review_reference'].strip())
            and type(review.get('reviewed_at'))is int and type(review.get('expires_at'))is int
            and review['reviewed_at']<=int(self.clock())<review['expires_at'] and review.get('program_pins')==pins)
        if not reviewed:
            return {'status':'UNKNOWN','reason':'current_independent_program_review_missing','context':context},unknown('current_independent_program_review_missing')
        self._check()
        usd=(self.usd_market or DexScreenerClient(timeout=5)).quote(USDC,now=int(self.clock()))
        self._check()
        usd_price=number(usd.price_usd,positive=True)
        if not 0<=int(self.clock())-usd.observed_at<=MAX_AGE: raise ValueError('USDC/USD observation stale')
        raw=int((ticket/usd_price*10**6).to_integral_value(rounding=ROUND_DOWN))
        if not 0<raw<2**64: raise ValueError('Ticket outside raw-unit bounds')
        acquired=cp.quote_output(state,USDC,raw)
        direction=0 if state['mint0']==mint else 1
        paper_bound=int((ticket/number(reference_price,positive=True)*10**state[f'decimals{direction}']).to_integral_value(rounding=ROUND_UP))
        covered=max(acquired,paper_bound)
        if not 0<covered<2**64:raise ValueError('Paper holding coverage outside raw-unit bounds')
        output=cp.quote_output(state,mint,covered)
        a,b=state[f'reserve{direction}'],state[f'reserve{1-direction}']
        impact=max(Decimal(0),1-Decimal(output)*a/(Decimal(covered)*b))*100
        # Bounded integer search reuses the same quote math and its 3% bound.
        low,high=covered,min(a,2**64-1)
        for _ in range(48):
            if low>=high: break
            mid=(low+high+1)//2
            try: cp.quote_output(state,mint,mid)
            except ValueError: high=mid-1
            else: low=mid
        capacity=Decimal(low)*b/a/10**6*usd_price
        if capacity<ticket: raise ValueError('Size-bounded exit capacity below intended ticket')
        recheck=self._rpc('getBlock',[slot,{'commitment':'finalized','transactionDetails':'none','rewards':False}])
        if recheck.get('blockhash')!=block['blockhash']: raise ValueError('Canonical slot changed')
        at=int(self.clock())
        venue={'status':'VALID','phase':'AMM','observed_at':at,'program':cp.PROGRAM,'context':context,
               'review_sha256':digest(review),'scope':'Verified direct classic-SPL Raydium CP pool structure and reviewed program pins'}
        route={'status':'VALID','observed_at':at,'ticket_usd':str(ticket),'input_usdc_raw':str(raw),
            'buy_quantity_raw':str(acquired),'paper_quantity_upper_bound_raw':str(paper_bound),'reference_price_usd':str(reference_price),
            'sell_quantity_raw':str(covered),'sell_output_usdc_raw':str(output),'exit_capacity_usd':str(capacity),
            'exit_price_impact_pct':str(impact),'base_usd':str(usd_price),'base_usd_observed_at':usd.observed_at,
            'state':plain(state),
            'context':context,'review_sha256':digest(review),'scope':'Deterministic exact-size quote on finalized pinned pool reserves; fees included',
            'state_after_buy_applied':False,'wallet_simulated':False,'network_costs_included':False,'real_money_enabled':False}
        return venue,route

    def capture(self, identity, mint, pool, *, quote=None, raw_report=None, now=None):
        validate_address(mint);validate_address(pool)
        if not isinstance(identity,dict) or not identity.get('account_id') or not identity.get('config_sha256'):
            raise ValueError('Existing account identity required')
        self.deadline=self.clock()+self.budget_seconds;self.rpc_calls=0
        started=int(self.clock()) if now is None else now
        if type(started)is not int or not 0<=started<=int(self.clock()): raise ValueError('Invalid capture time')
        own=self.store is None;store=self.store or EvidenceStore(self.root)
        out={'schema_version':1,'producer_revision':REVISION,'source_id':store.source_id,
            'account_id':identity['account_id'],'config_sha256':identity['config_sha256'],
            'mint':mint,'pool':pool,'observed_at':started,'received_at':started,
            'observations':{},'holder_coverage':unknown('not_collected'),'venue':unknown('not_collected'),'exit':unknown('not_collected'),
            'independence':unknown('No independently verified controller evidence producer is configured'),
            'dossier_fields':{'observations':{},'venue_phase':None,'exit_route':None,'independence_review':None},
            'source_refs':{},'errors':[],
            'limitations':['Holder totals are provider reports, not complete independently verified ownership.',
                'No inference of controller independence from distinct wallets, owners, fee payers or routers.',
                'Exact one-hour windows require recorded endpoints; no rounding, interpolation or historical backfill.',
                'State-based exit quotes are not wallet simulations, fills or guaranteed future liquidity.']}
        def error(part,exc):
            out['errors'].append({'part':part,'error_type':type(exc).__name__})
        try:
            with request_budget(self.deadline,clock=self.clock):
                try:
                    self._check()
                    if quote is None:
                        from rug_desk.market import Market
                        quote=(self.market or Market()).quote(mint,pool)
                    self._check()
                    from rug_desk.market import validate_quote
                    validate_quote(quote,int(self.clock()))
                    if quote['mint']!=mint or quote['pool']!=pool: raise ValueError('Pinned quote identity differs')
                    at=quote['observed_at'];value=number(quote['price'],positive=True)
                    out['observations']['price_now']={'status':'VALID','value':str(value),'observed_at':at,'received_at':int(self.clock()),'source':'dexscreener:pinned_pool','payload_sha256':digest(quote)}
                    out['dossier_fields']['observations']['price_now']={'value':float(value),'observed_at':at}
                except Exception as exc: error('price',exc)
                try:
                    self._check()
                    report=raw_report if raw_report is not None else (self.rugcheck or RugcheckClient(timeout=5)).raw_report(mint)
                    self._check();at=int(self.clock())
                    count=report.get('totalHolders')
                    if report.get('mint')!=mint or type(count)is not int or not 0<=count<=2**53:
                        raise ValueError('Explicit mint and finite whole holder total required')
                    # Receipt time proves when this report was seen, not when
                    # its underlying holder state was last observed.
                    out['observations']['reported_total_holders']={'status':'VALID','value':count,'observed_at':None,'received_at':at,
                        'source':'rugcheck:reported_totalHolders','payload_sha256':digest(report),'independently_verified':False,
                        'source_observation_time_known':False,'scope':'Provider-reported total at retrieval; not a complete timed holder window'}
                except Exception as exc: error('holders',exc)
                try:
                    self._check();self._reader()
                    out['holder_coverage']=self._holders(mint)
                    holder=out['holder_coverage']
                    if holder['status']=='VALID':
                        out['observations']['holders_now']=holder
                        out['dossier_fields']['observations']['holders_now']={'value':holder['value'],'observed_at':holder['observed_at']}
                except Exception as exc:
                    error('holder_coverage',exc);out['holder_coverage']=unknown('complete_canonical_holder_coverage_unavailable')
                try:
                    self._check()
                    self._reader()
                    from rug_desk import config
                    out['venue'],out['exit']=self._pool(identity,mint,pool,Decimal(config.TICKET_USD),out['observations']['price_now']['value'])
                    if out['venue']['status']=='VALID':out['dossier_fields']['venue_phase']='AMM'
                    route=out['exit']
                    if route['status']=='VALID':
                        at=route['observed_at']
                        out['dossier_fields']['exit_route']={'verified':True,'observed_at':at,'mint':mint,'pool':pool}
                        for name,field in [('exit_capacity_usd','exit_capacity_usd'),('exit_price_impact_pct','exit_price_impact_pct'),('exit_quote_ticket_usd','ticket_usd')]:
                            out['dossier_fields']['observations'][name]={'value':float(number(route[field])),'observed_at':at}
                except Exception as exc:
                    error('pool',exc);out['venue']=unknown('unsupported_or_unavailable_pinned_pool');out['exit']=unknown('unsupported_or_unavailable_size_bounded_exit')
        finally:
            out['observed_at']=max([started]+[x['observed_at'] for x in out['dossier_fields']['observations'].values()])
            out['received_at']=max(out['observed_at'],int(self.clock()))
            out['rpc_calls']=self.rpc_calls;out['duration_seconds']=max(0,self.clock()-(self.deadline-self.budget_seconds))
            result=store.put(out)
            if own:store.close()
        return result


def enrich(dossier, store, *, now=None):
    """Consume only prospective immutable records already received by snapshot."""
    d=deepcopy(dossier);snapshot=d['snapshot_at']
    if now is not None and snapshot>now: raise ValueError('Future dossier snapshot')
    records=store.available(d['account_id'],d['mint'],d['pool'],snapshot)
    def ref(record):
        identifier=digest(record);name='rug_desk:crew:'+identifier
        d['source_refs'][name]={'provider':'crew_factual_journal','reference':name,
            'observed_at':record['observed_at'],'payload_sha256':identifier}
        return name
    def observation(record,name,target=None):
        item=record['dossier_fields']['observations'][name]
        d['observations'][target or name]={**item,'source_ref':ref(record)}
    fresh=[r for r in records if 0<=snapshot-r['observed_at']<=MAX_AGE]
    if fresh:
        current=fresh[0]
        for name in ('price_now','holders_now'):
            if name in current['dossier_fields']['observations']:observation(current,name)
        fields=current['dossier_fields'];obs=fields['observations']
        if all(k in obs for k in ('price_now','holders_now')) and obs['price_now']['observed_at']==obs['holders_now']['observed_at']:
            end=obs['price_now']['observed_at'];start=end-3600
            old=next((r for r in records if all(r['dossier_fields']['observations'].get(k,{}).get('observed_at')==start for k in ('price_now','holders_now'))),None)
            if old:
                observation(old,'price_now','price_then');observation(old,'holders_now','holders_then')
                d['window']={'start':start,'end':end}
        if current['venue']['status']=='VALID' and fields['venue_phase']=='AMM':
            d['venue_phase']='AMM';d['venue_phase_source_ref']=ref(current)
        if current['exit']['status']=='VALID' and fields['exit_route'] is not None:
            if number(current['exit']['ticket_usd'])==number(d['ticket_usd']):
                d['exit_route']={**fields['exit_route'],'source_ref':ref(current)}
                for name in ('exit_capacity_usd','exit_price_impact_pct','exit_quote_ticket_usd'):observation(current,name)
        d['limitations']=list(dict.fromkeys(d['limitations']+current['limitations']))
    return d
