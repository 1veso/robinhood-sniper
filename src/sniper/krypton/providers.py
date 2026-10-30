"""Bounded public reads. No signing, swaps, fresh paid reports or model calls."""
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from sniper.adapters.solana import request_json, validate_address
from rug_desk.market import parse
from .config import number, digest
from .checks import TOKEN_PROGRAM, holder_facts, parsed


def settings(root):
    path=Path(root)/'krypton_bot/.env'
    values={}
    if path.exists():
        from dotenv import dotenv_values
        values=dotenv_values(path)
    for name in ('MODE','SOLANA_RPC_URL','XAI_API_KEY'):
        if name in os.environ: values[name]=os.environ[name]
    if values.get('MODE','paper')!='paper': raise ValueError('Live mode is not implemented')
    return values


def lp_evidence(report,mint,pool,now):
    out={'mint':mint,'pool':pool,'source':'rugcheck','observed_at':now,'state':'UNKNOWN',
        'independently_verified':False,'source_state_timestamp':None,
        'scope':'Rugcheck provider classification; retrieval time is not chain-state time; not independent LP verification',
        'reason':'Missing or invalid pool-specific provider confirmation','report_hash':digest(report)}
    if not isinstance(report,dict) or report.get('mint')!=mint: return out
    risks=report.get('risks'); rugged=report.get('rugged')
    if type(rugged) is not bool or not isinstance(risks,list): return out
    if any(not isinstance(r,dict) or r.get('level') not in ('info','warn','danger') for r in risks): return out
    out['danger']=rugged or any(r['level']=='danger' for r in risks)
    if out['danger']:
        out.update(state='FAIL',reason='Provider reports dangerous/rugged token'); return out
    markets=report.get('markets')
    if not isinstance(markets,list): return out
    matches=[m for m in markets if isinstance(m,dict) and m.get('pubkey',m.get('address'))==pool]
    if len(matches)!=1: return out
    try:
        pct=number(matches[0]['lp']['lpLockedPct'])
        if pct>100: return out
    except (KeyError,TypeError,ValueError): return out
    out.update(reported_locked_pct=str(pct),state='PASS' if pct==100 else 'FAIL',
        reason='Provider reports 100% locked/burned for pinned pool' if pct==100 else 'Provider does not report fully locked/burned LP')
    return out


class Providers:
    def __init__(self,root,book,clock=time.time,transport=request_json):
        self.root,self.book,self.clock,self.transport=Path(root),book,clock,transport
        values=settings(root)
        saved=self.root/'runtime/solana-rpc-url.txt'
        self.rpc_url=values.get('SOLANA_RPC_URL') or (saved.read_text().strip() if saved.exists() else 'https://api.mainnet.solana.com')
        self.native_cache=None

    def request(self,provider,url,body=None):
        now=int(self.clock()); prior=self.book.provider(provider)
        if prior and prior.get('retry_after',0)>now: raise ValueError(provider+' backoff active')
        # Persist BEFORE the request. Interrupted calls also respect this bound.
        self.book.provider(provider,{'attempted_at':now,'retry_after':now+(60 if provider=='profiles' else 0),'state':'RUNNING'})
        try:
            headers={'Accept':'application/json','User-Agent':'sniper-paper/0.1 Krypton'}
            if body is not None:headers['Content-Type']='application/json'
            result=self.transport('POST' if body is not None else 'GET',url,body=body,headers=headers,timeout=8)
        except Exception as exc:
            code=exc.code if isinstance(exc,HTTPError) else None
            self.book.provider(provider,{'attempted_at':now,'finished_at':int(self.clock()),'state':'ERROR',
                'error':('HTTP '+str(code)) if code else type(exc).__name__,
                'retry_after':now+(300 if code==403 else 60)})
            raise ValueError(provider+': '+('HTTP '+str(code) if code else type(exc).__name__)) from None
        self.book.provider(provider,{'attempted_at':now,'finished_at':int(self.clock()),'state':'OK',
            'retry_after':now+(60 if provider=='profiles' else 0)})
        return result

    def rpc(self,method,params):
        response=self.request('rpc',self.rpc_url,{'jsonrpc':'2.0','id':1,'method':method,'params':params})
        if not isinstance(response,dict) or response.get('error') or 'result' not in response:
            self.book.provider('rpc',{'state':'ERROR','finished_at':int(self.clock()),'retry_after':int(self.clock())+60,'error':'RPC response error'})
            raise ValueError('RPC response error')
        return response['result']

    def profiles(self):
        values=self.request('profiles','https://api.dexscreener.com/token-profiles/latest/v1')
        if not isinstance(values,list): raise ValueError('Invalid profiles response')
        result=[]
        for item in values[:100]:
            if isinstance(item,dict) and item.get('chainId')=='solana':
                try: mint=validate_address(item.get('tokenAddress'))
                except ValueError: continue
                if mint not in result: result.append(mint)
        return result

    def pairs(self,mint):
        return self.request('dexscreener','https://api.dexscreener.com/token-pairs/v1/solana/'+validate_address(mint))

    def quote(self,mint,pool=None):
        pairs=self.pairs(mint); now=int(self.clock())
        if not isinstance(pairs,list): raise ValueError('Invalid pair response')
        if pool is None:
            choices=[]
            for item in pairs:
                try:
                    q=parse(mint,item['pairAddress'],[item],now)
                    choices.append((number(q['liquidity']),q['pool']))
                except (ValueError,KeyError,TypeError): continue
            if not choices: raise ValueError('No valid pool observation')
            pool=max(choices)[1]
        out=parse(mint,pool,pairs,now)
        return out

    def costs(self,quote):
        now=int(self.clock())
        if not self.native_cache or now-self.native_cache['sol_observed_at']>20:
            wsol='So11111111111111111111111111111111111111112'; usdc='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
            choices=[]
            for p in self.pairs(wsol):
                try:
                    if p['chainId']=='solana' and p['baseToken']['address']==wsol and p['quoteToken']['address']==usdc:
                        choices.append((number(p['liquidity']['usd'],True),number(p['priceUsd'],True)))
                except (KeyError,ValueError,TypeError): continue
            if not choices: raise ValueError('Native USD cost mark unavailable')
            self.native_cache={'sol_usd':str(max(choices)[1]),'sol_observed_at':int(self.clock())}
        return dict(quote,**self.native_cache)

    def holders(self,mint,cfg):
        opts={'commitment':'finalized'}
        supply=self.rpc('getTokenSupply',[mint,opts]); top=self.rpc('getTokenLargestAccounts',[mint,opts])
        slot=max(supply['context']['slot'],top['context']['slot'])
        if type(slot) is not int or slot<=0: raise ValueError('Missing finalized context')
        addresses=[validate_address(a['address']) for a in top['value']]
        if len(addresses)>20 or len(set(addresses))!=len(addresses): raise ValueError('Invalid largest-accounts sample')
        snap=self.rpc('getMultipleAccounts',[[mint]+addresses,dict(opts,encoding='jsonParsed',minContextSlot=slot)])
        if snap['context']['slot']<slot or len(snap['value'])!=len(addresses)+1: raise ValueError('RPC snapshot mismatch')
        mint_account=snap['value'][0]
        facts=holder_facts(mint,mint_account,list(zip(addresses,snap['value'][1:])),complete=False)
        info=parsed(mint_account,'mint')
        if info['supply']!=supply['value']['amount'] or info['decimals']!=supply['value']['decimals']:
            raise ValueError('Supply changed during capture')
        facts.update(mint=mint,observed_at=int(self.clock()),source='solana_finalized_rpc',slot=snap['context']['slot'])
        # Partial owner shares can reject, but cannot prove either upper bound.
        if (facts['mint_disabled'] is not True or facts['freeze_disabled'] is not True
            or number(facts['max_single_holder'] or 0)>number(cfg['max_single_holder'])
            or number(facts['top10_share'] or 0)>number(cfg['max_top10_share'])): return facts
        full=self.rpc('getProgramAccounts',[TOKEN_PROGRAM,dict(opts,encoding='jsonParsed',withContext=True,
            minContextSlot=snap['context']['slot'],filters=[{'dataSize':165},{'memcmp':{'offset':0,'bytes':mint}}])])
        rows=full['value']; fullslot=full['context']['slot']
        if type(fullslot) is not int or fullslot<snap['context']['slot'] or not isinstance(rows,list) or len(rows)>cfg['max_holder_accounts']:
            raise ValueError('Bounded complete holder enumeration unavailable')
        after=self.rpc('getAccountInfo',[mint,dict(opts,encoding='jsonParsed',minContextSlot=fullslot)])
        if after['context']['slot']<fullslot or parsed(after['value'],'mint')!=info: raise ValueError('Mint changed during enumeration')
        complete=holder_facts(mint,mint_account,[(a['pubkey'],a['account']) for a in rows],complete=True)
        complete.update(mint=mint,source='solana_finalized_rpc',observed_at=int(self.clock()),slot=fullslot,
            mint_before_slot=snap['context']['slot'],mint_after_slot=after['context']['slot'])
        return complete

    def lp(self,mint,pool):
        report=self.request('rugcheck','https://api.rugcheck.xyz/v1/tokens/'+validate_address(mint)+'/report')
        return lp_evidence(report,mint,pool,int(self.clock()))
