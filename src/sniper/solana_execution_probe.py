"""Free setup evidence for the restricted CP-Swap path. Never signs or submits."""
import hashlib
import time
from urllib.parse import urlencode
from . import solana_cpmm as cp
from .execution_solana import SolanaExecutionAdapter,SolanaRpc
from .adapters.solana_execution import USDC,MAINNET_GENESIS
from .solana_preflight import validate_quote


def observe_programs(reader):
    programs=[cp.PROGRAM,cp.TOKEN,cp.ATA]
    result=reader.rpc('getMultipleAccounts',[programs,{'encoding':'base64','commitment':'finalized'}])
    accounts=dict(zip(programs,result['value']));extra=[]
    for key in programs:
        raw=cp.blob(accounts[key],executable=True)
        if accounts[key]['owner']==cp.UPGRADEABLE:
            cp.require(len(raw)==36 and raw[:4]==b'\2\0\0\0','Unknown program loader state')
            extra.append(cp.key(raw,4))
    if extra:
        result=reader.rpc('getMultipleAccounts',[programs+extra,{'encoding':'base64','commitment':'finalized','minContextSlot':result['context']['slot']}])
        accounts=dict(zip(programs+extra,result['value']))
    return {key:cp.deployment_observation(key,accounts) for key in programs}


def probe(reader,wallet,*,target=cp.WSOL,clock=time.time):
    """Observed pins are draft review material, not self-approved deployments."""
    started=int(clock());reader.verify_chain()
    result={'observed_at':started,'wallet_sha256':hashlib.sha256(wallet.encode()).hexdigest(),'genesis_verified':True,
        'strategy_token_approved':False,'signer_loaded':False,'signing_calls':0,'broadcast_calls':0,'live_execution_ready':False,
        'scope':'Direct classic-SPL Raydium CP setup pair. Observed program pins require independent review.'}
    result['observed_program_pins']=observe_programs(reader)
    policy={'account_id':'read-only-cp-setup-probe','chain_id':'solana:mainnet','genesis_hash':MAINNET_GENESIS,
        'wallet':wallet,'base_token':USDC,'base_decimals':6,'execution_target':'automatic','capital_raw':25000000,
        'max_order_raw':25000000,'max_total_exposure_raw':25000000,'daily_loss_raw':25000000,
        'max_gas_wei_day':1000000,'max_tx_gas_wei':50000,'deployment_authorized':False,'acceptance_mode':False,
        'program_pins':result['observed_program_pins']}
    adapter=SolanaExecutionAdapter(policy,reader,clock=clock)
    params={'inputMint':USDC,'outputMint':target,'amount':'25000000','taker':wallet,'slippageBps':100,
            'onlyDirectRoutes':'true','dexes':'Raydium CP','wrapAndUnwrapSol':'false','transactionVersion':'0'}
    try:
        raw=reader.transport('GET','https://api.jup.ag/swap/v2/build?'+urlencode(params),None,{'x-api-key':reader.api_key},20)
        validate_quote(raw,USDC,target,25000000);cp.require(len(raw['routePlan'])==1,'Not a direct pool')
        pool=raw['routePlan'][0]['swapInfo']['ammKey'];result['pool']=pool
        quote=adapter.quote_swap(USDC,target,pool,25000000)
        result['quote']=quote;result['independent_pool_quote_validated']=True
        state,accounts,slot,pins=adapter.pool_snapshot(pool,USDC,target)
        wallet_account=accounts[wallet]
        result['wallet_checks']={'slot':slot,'native_lamports':wallet_account.get('lamports',0) if wallet_account else 0,
            'ordinary_fee_payer':bool(wallet_account and wallet_account.get('owner')==cp.SYSTEM),
            'input_ata_present':accounts[cp.ata(wallet,USDC)] is not None,
            'output_ata_present':accounts[cp.ata(wallet,target)] is not None}
        i={**quote,**{k:adapter.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},'id':'free-probe','side':'buy','kind':'swap'}
        try:
            prepared=adapter.prepare(i)
            result['wallet_specific_simulation']={'succeeded':True,'message_sha256':prepared['message_sha256'],
                'fee_lamports':prepared['max_fee_wei'],'rent_lamports':prepared['rent_lamports']}
        except Exception as exc:
            result['wallet_specific_simulation']={'succeeded':False,'error_type':type(exc).__name__,
                'reason':'Real wallet balances/accounts, fee, blockhash or simulation did not satisfy the restricted builder'}
    except Exception as exc:
        result['quote_error_type']=type(exc).__name__
        result['quote_http_status']=getattr(exc,'code',None)
        result['independent_pool_quote_validated']=False
    result['finished_at']=int(clock());return result
