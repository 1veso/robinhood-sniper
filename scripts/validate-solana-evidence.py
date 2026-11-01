"""Bounded read-only live diagnostics; no model, trade, wallet or ledger writes."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT))
from sniper.crew_evidence import ReadOnlyRpc, digest
from sniper.holder_evidence import collect as holders
from sniper.buyer_evidence import collect as buyers
from sniper import pumpswap_evidence as pump
from sniper.adapters.solana_execution import MAINNET_GENESIS
from sniper.adapters.solana import validate_address, DexScreenerClient
from decimal import Decimal


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mint',required=True);parser.add_argument('--pool',required=True)
    parser.add_argument('--holder-method',choices=['getProgramAccounts','getProgramAccountsV2'],default='getProgramAccounts')
    parser.add_argument('--holder-rpc-file',type=Path,help='Local file with an alternate HTTPS RPC URL; never prints the URL')
    parser.add_argument('--skip-holders',action='store_true',help='Keep an already diagnosed provider outage from being retried')
    args=parser.parse_args();validate_address(args.mint);validate_address(args.pool)
    if args.holder_rpc_file and not args.holder_rpc_file.is_file():parser.error('Holder RPC file does not exist')
    try:
        import certifi
        os.environ.setdefault('SSL_CERT_FILE',certifi.where())
    except ImportError:pass
    rpc=ReadOnlyRpc(rpc_url=(ROOT/'runtime/solana-rpc-url.txt').read_text(encoding='utf-8-sig').strip())
    calls=[];deadline=time.monotonic()+90
    def read(method,params,reader=None):
        if len(calls)>=20 or time.monotonic()>=deadline:raise TimeoutError('Diagnostic request bound')
        calls.append(method);return (reader or rpc).rpc(method,params)
    if read('getGenesisHash',[])!=MAINNET_GENESIS:raise ValueError('Mainnet required')
    result={'schema':1,'started_at':int(time.time()),'mint':args.mint,'pool':args.pool,
        'purpose':'READ_ONLY_PAPER_EVIDENCE_DIAGNOSTIC','real_money_enabled':False,'results':{}}
    def run(name,fn):
        try:result['results'][name]=fn()
        except Exception as exc:
            # Do not log exceptions containing credential-bearing request URLs.
            item={'status':'UNAVAILABLE','error_type':type(exc).__name__,'method':calls[-1] if calls else None}
            if isinstance(exc,HTTPError):
                item['http_status']=exc.code
                try:
                    error=json.loads(exc.read()).get('error',{})
                    item['rpc_code']=error.get('code')
                    message=str(error.get('message','')).lower()
                    if 'free tier' in message:item['reason']='provider_plan_does_not_include_method'
                    elif 'compute units' in message:item['reason']='provider_compute_capacity_exceeded'
                except (ValueError,AttributeError):pass
            elif isinstance(exc,ValueError):item['reason']='evidence_validation_failed'
            result['results'][name]=item
    if args.skip_holders:result['results']['holders']={'status':'NOT_RETRIED','reason':'known_provider_access_failure'}
    else:
        endpoint=args.holder_rpc_file or ROOT/'runtime/crew/holder-rpc-url.txt'
        holder_reader=ReadOnlyRpc(rpc_url=endpoint.read_text(encoding='utf-8-sig').strip()) if endpoint.is_file() else rpc
        run('holders',lambda:holders(lambda method,params:read(method,params,holder_reader),args.mint,time.time,method=args.holder_method))
    def pool_quote():
        first=read('getAccountInfo',[args.pool,{'encoding':'base64','commitment':'finalized'}])
        state=pump.pool_state(first['value'],int(time.time()))
        if state['mint0']!=args.mint:raise ValueError('Mint differs')
        keys=[args.pool,state['config'],state['fee_config'],state['vault0'],state['vault1'],state['mint0'],state['mint1']]
        snapshot=read('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'finalized','minContextSlot':first['context']['slot']}])
        slot=snapshot['context']['slot'];accounts=dict(zip(keys,snapshot['value']))
        state=pump.inspect_pool(args.pool,state['mint1'],args.mint,pump.PROGRAM,accounts,int(time.time()))
        block=read('getBlock',[slot,{'commitment':'finalized','transactionDetails':'none','rewards':False}])
        if type(block.get('blockTime'))is not int or not 0<=int(time.time())-block['blockTime']<=30:raise ValueError('Stale snapshot')
        valuation=DexScreenerClient(timeout=5).quote(state['mint1'],now=int(time.time()))
        if not 0<=int(time.time())-valuation.observed_at<=30:raise ValueError('Stale USD valuation')
        from rug_desk.config import TICKET_USD
        units=int(Decimal(str(TICKET_USD))/Decimal(str(valuation.price_usd))*10**state['decimals1'])
        bought=pump.quote_output(state,state['mint1'],units)
        sold=pump.quote_output(state,args.mint,bought)
        return {'status':'VALID_READ_ONLY_QUOTE','slot':slot,'block_hash':block['blockhash'],'block_time':block['blockTime'],
            'snapshot_sha256':digest(snapshot),'quote_mint':state['mint1'],'quote_usd':str(valuation.price_usd),
            'quote_usd_observed_at':valuation.observed_at,'ticket_usd':str(TICKET_USD),'input_quote_raw':str(units),
            'buy_quantity_raw':str(bought),'sell_output_quote_raw':str(sold),'state':state,
            'program_review':'NOT_ASSERTED','state_after_buy_applied':False,'paper_fill_recorded':False,
            'scope':'Pure state quote, not admission proof or wallet simulation'}
    run('pool_quote',pool_quote)
    run('buyer_control',lambda:buyers(read,args.mint,args.pool,time.time,limit=1))
    result['finished_at']=int(time.time());result['rpc_methods']=calls
    folder=ROOT/'runtime/crew/live-diagnostics';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(str(result['finished_at'])+'.json');path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    for name,item in result['results'].items():
        print(name,json.dumps({k:item[k] for k in ('status','collection_status','reason','http_status','rpc_code','slot','observed_buy_receipts','verified_independent_buyers') if k in item}))
    print('Diagnostic saved:',path.relative_to(ROOT))
    return 0


if __name__=='__main__':raise SystemExit(main())
