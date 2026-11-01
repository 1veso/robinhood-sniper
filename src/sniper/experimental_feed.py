"""Bounded live reads for experimental portfolios; no execution/signing interface."""
from decimal import Decimal as D
from pathlib import Path
import time
from .adapters.solana import request_json,validate_address,request_budget
from .adapters.solana_execution import SolanaExecutionReader,MAINNET_GENESIS,USDC,WSOL
from .krypton.config import number,digest
from . import pumpswap_evidence as pump,solana_cpmm as cp
from .solana_wire import b58decode

class SourceUnavailable(RuntimeError):
 pass

class Reader(SolanaExecutionReader):
 ALLOWED_RPC=frozenset({'getGenesisHash','getAccountInfo','getMultipleAccounts','getBlockTime','getTokenLargestAccounts'})

class Feed:
 def __init__(self,root,clock=time.time,transport=request_json):
  self.root=Path(root);self.clock=clock;self.transport=transport;self.cache={};self.cache_at={};self.native=None
  path=self.root/'runtime/holder-rpc-url.txt'
  if not path.exists():path=self.root/'runtime/solana-rpc-url.txt'
  self.reader=Reader(rpc_url=path.read_text(encoding='utf-8-sig').strip(),transport=transport)
  try:self.reader.verify_chain()
  except Exception as exc:raise SourceUnavailable(type(exc).__name__) from None
 def get(self,url):
  try:return self.transport('GET',url,None,{'Accept':'application/json','User-Agent':'sniper-experimental-paper'},8)
  except Exception as exc:raise SourceUnavailable(type(exc).__name__) from None
 def rpc(self,method,args):
  try:return self.reader.rpc(method,args)
  except Exception as exc:raise SourceUnavailable(type(exc).__name__) from None
 def candidates(self):
  rows=self.get('https://api.dexscreener.com/token-profiles/latest/v1');out=[]
  if not isinstance(rows,list):raise ValueError('Discovery unavailable')
  for r in rows[:100]:
   if r.get('chainId')=='solana':
    try:mint=validate_address(r.get('tokenAddress'))
    except ValueError:continue
    if mint not in out:out.append(mint)
  return out
 def pairs(self,mint):
  if mint not in self.cache or self.clock()-self.cache_at[mint]>15:
   at=int(self.clock());self.cache[mint]=self.get('https://api.dexscreener.com/token-pairs/v1/solana/'+validate_address(mint));self.cache_at[mint]=at
  if not isinstance(self.cache[mint],list):raise ValueError('Pair source unavailable')
  return self.cache[mint]
 def native_price(self):
  if self.native and 0<=int(self.clock())-self.native[1]<=20:return self.native
  choices=[]
  for p in self.pairs(WSOL):
   if p.get('chainId')=='solana' and p.get('baseToken',{}).get('address')==WSOL and p.get('quoteToken',{}).get('address')==USDC:
    try:choices.append((number(p['liquidity']['usd'],True),number(p['priceUsd'],True)))
    except (KeyError,ValueError):pass
  if not choices:raise ValueError('Native USD mark unavailable')
  self.native=(max(choices)[1],self.cache_at[WSOL]);return self.native
 def capture(self,mint,pool=None):
  # Every capture has a finite global deadline, including response-body reads.
  with request_budget(self.clock()+35,clock=self.clock):return self._capture(mint,pool)
 def _capture(self,mint,pool):
  started=int(self.clock());pairs=self.pairs(mint);choices=[]
  for p in pairs:
   if (p.get('chainId')!='solana' or p.get('baseToken',{}).get('address')!=mint or (pool and p.get('pairAddress')!=pool)):continue
   try:
    validate_address(p['pairAddress']);liq=number(p['liquidity']['usd'],True);number(p['priceUsd'],True)
    if not pool and (liq<25000 or type(p.get('pairCreatedAt'))is not int or started-p['pairCreatedAt']/1000<600):continue
    choices.append((liq,p))
   except (ValueError,KeyError,TypeError):continue
  if not choices:raise ValueError('No current liquid eligible pair')
  pair=max(choices,key=lambda x:x[0])[1];pool=pair['pairAddress']
  raw=self.rpc('getAccountInfo',[pool,{'encoding':'base64','commitment':'finalized'}]);first=raw['context']['slot']
  adapter=pump if raw['value'] and raw['value'].get('owner')==pump.PROGRAM else cp
  state=adapter.pool_state(raw['value'],started)
  if mint not in (state['mint0'],state['mint1']):raise ValueError('Pool mint mismatch')
  quote=state['mint1'] if state['mint0']==mint else state['mint0']
  if quote not in (WSOL,USDC):raise ValueError('Unsupported quote asset')
  keys=list(dict.fromkeys([pool,state['mint0'],state['mint1'],state['vault0'],state['vault1'],state['config'],state['fee_config'] if adapter is pump else state['observation']]))
  snap=self.rpc('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':first}])
  slot=snap['context']['slot']
  if type(slot)is not int or slot<first or len(snap['value'])!=len(keys):raise ValueError('Incomplete pool snapshot')
  accounts=dict(zip(keys,snap['value']))
  # The all-ones system address is a read-only placeholder; no user's wallet
  # or token accounts are read or connected. Only pool state is inspected.
  state=adapter.inspect_pool(pool,quote,mint,cp.SYSTEM,accounts,started)
  index=0 if state['mint0']==mint else 1
  mint_raw,_=pump.token_blob(accounts[mint],'mint')
  if int.from_bytes(mint_raw[:4],'little')!=0 or int.from_bytes(mint_raw[46:50],'little')!=0:raise ValueError('Mint or freeze authority enabled')
  supply=cp.u64(mint_raw,36)
  if supply<=0:raise ValueError('Empty mint supply')
  top=self.rpc('getTokenLargestAccounts',[mint,{'commitment':'finalized'}]);topslot=top['context']['slot']
  addresses=[validate_address(r['address']) for r in top['value']]
  if type(topslot)is not int or topslot<slot or len(addresses)>20 or len(set(addresses))!=len(addresses):raise ValueError('Holder sample context invalid')
  held=self.rpc('getMultipleAccounts',[addresses,{'encoding':'base64','commitment':'finalized','minContextSlot':topslot}])
  if type(held['context']['slot'])is not int or held['context']['slot']<topslot or len(held['value'])!=len(addresses):raise ValueError('Holder sample unavailable')
  owners={}
  for address,account in zip(addresses,held['value']):
   if address in (state['vault0'],state['vault1']):continue
   token,program=pump.token_blob(account,'account')
   if cp.key(token,0)!=mint or program!=accounts[mint]['owner']:raise ValueError('Sample mint mismatch')
   owner=cp.key(token,32);owners[owner]=owners.get(owner,0)+cp.u64(token,64)
  values=sorted(owners.values(),reverse=True)
  if sum(values)>supply:raise ValueError('Sample exceeds supply')
  stamp=self.rpc('getBlockTime',[slot]);now=int(self.clock())
  if type(stamp)is not int or not 0<=now-stamp<=120:raise ValueError('Stale finalized state')
  sol,sol_at=self.native_price();baseusd=sol if quote==WSOL else D(1)
  base=number(state[f'reserve{index}'],True)/(D(10)**state[f'decimals{index}'])
  quoted=number(state[f'reserve{1-index}'],True)/(D(10)**state[f'decimals{1-index}'])
  price=quoted/base*baseusd;reference=number(pair['priceUsd'],True)
  if abs(price/reference-1)>D('.20'):raise ValueError('Chain and market price diverge')
  realquote=number(state.get('real_quote_reserve',state[f'reserve{1-index}']),True)/(D(10)**state[f'decimals{1-index}'])
  liq=min(number(pair['liquidity']['usd'],True),2*realquote*baseusd)
  # Require a positive bounded exit for a small probe, without constructing a transaction.
  probe=max(1,int(D(10)/price*(D(10)**state[f'decimals{index}'])))
  adapter.quote_output(state,mint,probe)
  fees=sum(state['fees_bps']) if adapter is pump else D(state['trade_fee_rate']+state['creator_fee_rate'])/100
  tx=pair.get('txns',{});changes=pair.get('priceChange',{});vol=pair.get('volume',{})
  activity=dict(buys_h1=tx.get('h1',{}).get('buys'),sells_h1=tx.get('h1',{}).get('sells'),
   buys_m5=tx.get('m5',{}).get('buys'),sells_m5=tx.get('m5',{}).get('sells'),
   volume_h1=vol.get('h1'),change_h1=changes.get('h1'),change_m5=changes.get('m5'),
   observed_owner_share=str(D(values[0] if values else 0)/supply),observed_top10_share=str(D(sum(values[:10]))/supply),
   sampled_token_accounts=len(addresses),full_holder_coverage=False,verified_pool_vaults_excluded=True,
   activity_source='DexScreener aggregate counts; individual buyers and purchases not established')
  q=dict(mint=mint,pool=pool,symbol=str(pair['baseToken'].get('symbol',''))[:40],price=str(price),liquidity=str(liq),
   observed_at=min(started,self.cache_at[mint]),created_at_ms=pair['pairCreatedAt'],sol_usd=str(sol),sol_observed_at=sol_at,executable=False)
  from rug_desk.book import atomic,encode
  archive={'pool':pool,'mint':mint,'accounts':accounts,'slot':slot,'block_time':stamp,
           'holder_snapshot':held,'largest_accounts':top,'pair':pair,'native_usd':str(sol),'native_at':sol_at}
  archive_hash=digest(archive);destination=self.root/'runtime/experiments/evidence'/(archive_hash+'.json')
  destination.parent.mkdir(parents=True,exist_ok=True)
  if not destination.exists():atomic(destination,encode(archive))
  return dict(source_mode='live',raw_capture_sha256=archive_hash,quote=q,activity=activity,fee_bps=str(fees),
   chain=dict(genesis_verified=True,commitment='finalized',slot=slot,block_time=stamp,mint=mint,pool=pool,
    mint_disabled=True,freeze_disabled=True,pool_structure_verified=True,program=adapter.PROGRAM,
    snapshot_hash=digest(accounts),holder_snapshot_hash=digest(held),quote_currency=quote,usdc_usd_assumption='1' if quote==USDC else None),
   market_payload_hash=digest(pair),received_at=int(self.clock()),independence='UNKNOWN',trader_skill='UNKNOWN',lp_lock='UNKNOWN')
