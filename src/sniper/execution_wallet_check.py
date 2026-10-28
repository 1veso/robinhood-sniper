"""Read-only EVM wallet simulation. Never approves tokens or signs transactions."""
import hashlib
import time
from pathlib import Path
from .execution import validate_policy
from .execution_service import validate_deployment
from .execution_evm import EvmExecutionAdapter,ExecutionRpcClient
from .adapters.evm import address,quantity


def validate_wallet(root,policy,*,rpc=None,clock=time.time):
    root=Path(root);p=validate_policy(policy);validate_deployment(p)
    public=address((root/'runtime/evm-trading-address.txt').read_text(encoding='utf-8-sig').strip())
    if public!=p['wallet']:raise ValueError('Public wallet differs from reviewed policy')
    pair=p.get('validation_pair') or {}
    if not all(k in pair for k in ('mint','pool','buy_input_raw','sell_input_raw')):
        raise ValueError('Explicit setup pair and both exact amounts required')
    if not 0<int(pair['buy_input_raw'])<=p['max_order_raw']:
        raise ValueError('Setup amount exceeds policy limit')
    if rpc is None:
        rpc=ExecutionRpcClient((root/'runtime/quote-rpc-url.txt').read_text(encoding='utf-8-sig').strip(),timeout=8,max_retries=1)
    adapter=EvmExecutionAdapter(p,rpc,clock=clock)
    finality=adapter.check_finality_latency();anchor=adapter._anchor();tag=hex(anchor['block_number'])
    if rpc.call('eth_getCode',[p['wallet'],tag])!='0x':raise ValueError('Dedicated EOA required')
    if quantity(rpc.call('eth_getTransactionCount',[p['wallet'],'latest']))!=quantity(rpc.call('eth_getTransactionCount',[p['wallet'],'pending'])):
        raise ValueError('Pending wallet transactions require reconciliation')
    if adapter._balance(p['base_token'],tag)<p['capital_raw']:raise ValueError('Wallet settlement balance below allocation')
    if quantity(rpc.call('eth_getBalance',[p['wallet'],tag]))<p['max_gas_wei_day']:raise ValueError('Wallet native balance below reserve')
    if adapter._uint(p['base_token'],'0x313ce567',tag)!=p['base_decimals']:raise ValueError('Settlement precision mismatch')
    results=[]
    for side,source,target,amount in [('buy',p['base_token'],pair['mint'],pair['buy_input_raw']),('sell',pair['mint'],p['base_token'],pair['sell_input_raw'])]:
        try:
            quote=adapter.quote_swap(source,target,pair['pool'],amount)
            intent={**quote,**{k:p[k] for k in ('account_id','policy_hash')},'id':'wallet-validation:'+side,'kind':'swap','side':side}
            prepared=adapter.prepare(intent)
            if prepared.get('requires_approval'):
                results.append({'side':side,'succeeded':False,'reason':'Exact on-chain allowance prerequisite is absent; approval is not submitted'})
            else:
                results.append({'side':side,'succeeded':True,'block':prepared['anchor'],
                    'max_fee_wei':prepared['max_fee_wei'],'simulation_result':prepared['simulation_result']})
        except Exception as exc:
            results.append({'side':side,'succeeded':False,'error_type':type(exc).__name__,
                            'reason':'Actual wallet preparation failed; no override or fabricated balance used'})
    return {'validated':len(results)==2 and all(r['succeeded'] for r in results),
        'policy_hash':p['policy_hash'],'wallet_sha256':hashlib.sha256(p['wallet'].encode()).hexdigest(),
        'tested_at':int(clock()),'finality':finality,'probes':results,'signing_calls':0,'broadcast_calls':0,
        'scope':'Actual setup-pair wallet simulations; later tokens still require current safety and simulation'}
