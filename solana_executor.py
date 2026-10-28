"""Solana automatic executor operator entrypoint. Default command is read-only status."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
from rug_desk.book import atomic,process_lock
from rug_desk.model_gate import status as model_status
from sniper.execution import encode
from sniper.execution_solana import SolanaJournal,SolanaExecutionAdapter,SolanaRpc
from sniper.execution_solana_service import cycle,validate_deployment
from sniper.solana_execution_probe import probe
from sniper import solana_cpmm as cp
from sniper.adapters.solana_execution import USDC
from prepare_live import local_reader


def reader(root):
    original=local_reader(root/'runtime')
    return SolanaRpc(rpc_url=original.rpc_url,api_key=original.api_key,transport=original.transport)


def wallet_validation(root,policy):
    """Actual accounts, no overrides, signatures, funding or submission."""
    p=validate_deployment(policy,root);adapter=SolanaExecutionAdapter(p,reader(root))
    pair=p.get('validation_pair') or {}
    cp.require(all(k in pair for k in ('mint','pool','buy_input_raw','sell_input_raw')),'Explicit validation setup pair required')
    cp.require(0<int(pair['buy_input_raw'])<=p['max_order_raw'],'Validation buy amount exceeds policy')
    results=[]
    for side,source,target,amount in [('buy',USDC,pair['mint'],pair['buy_input_raw']),('sell',pair['mint'],USDC,pair['sell_input_raw'])]:
        q=adapter.quote_swap(source,target,pair['pool'],int(amount))
        i={**q,**{k:p[k] for k in ('account_id','chain_id','wallet','policy_hash')},'id':'wallet-validation:'+side,'side':side,'kind':'swap'}
        prepared=adapter.prepare(i)
        results.append({'side':side,'message_sha256':prepared['message_sha256'],'fee_lamports':prepared['max_fee_wei'],
            'rent_lamports':prepared['rent_lamports'],'slot':prepared['context_slot'],'succeeded':True})
    _,accounts,slot,_=adapter.pool_snapshot(pair['pool'],USDC,pair['mint'])
    cp.require(cp.token_account(accounts[cp.ata(p['wallet'],USDC)],USDC,p['wallet'])>=p['capital_raw'],'Wallet USDC below approved allocation')
    cp.require(accounts[p['wallet']]['lamports']>=p['max_gas_wei_day']+p['max_rent_lamports']*p['max_open_positions'],'Wallet native balance below fee/rent reserve')
    return {'validated':True,'policy_hash':p['policy_hash'],'wallet_sha256':hashlib.sha256(p['wallet'].encode()).hexdigest(),
            'tested_at':int(time.time()),'probes':results,'signing_calls':0,'broadcast_calls':0,
            'scope':'Wallet-specific setup-pair simulations only; each later trade must be screened and simulated again'}


def initialize(root,policy_path):
    from sniper.signer_vault import load_signer
    root=Path(root);folder=root/'runtime/executor/solana-live'
    cp.require(not (folder/'ledger.sqlite3').exists() and not (folder/'policy.json').exists(),'Execution account already initialized; never overwrite it')
    p=validate_deployment(json.loads(policy_path.read_text(encoding='utf-8-sig')),root)
    cp.require(model_status(root/'rug_desk')['enabled'],'Funded, authorized model gate must pass before initial deployment')
    checks=wallet_validation(root,p)
    # Ownership is checked locally after explicit deployment authority, never by a test trade.
    key=load_signer(Path(p['signer_path']),p);cp.require(str(key.pubkey())==p['wallet'],'Signer does not match public wallet')
    folder.mkdir(parents=True,exist_ok=True)
    with closing(SolanaJournal(folder/'ledger.sqlite3',p,create=True)) as journal:
        atomic(folder/'policy.json',encode(journal.policy))
    atomic(folder/'wallet-validation.json',encode(checks))
    return {'initialized':True,'account_id':p['account_id'],'signing_calls':0,'broadcast_calls':0,
            'scope':'Separate live account prepared; run workers only under the approved deployment'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--app-root',type=Path,default=ROOT)
    sub=parser.add_subparsers(dest='command',required=True);sub.add_parser('status');sub.add_parser('probe')
    for command in ('once','run','collect'):
        p=sub.add_parser(command);p.add_argument('--allow-live-submission',action='store_true')
        if command=='run':p.add_argument('--interval',type=int,default=10)
    init=sub.add_parser('initialize-live');init.add_argument('--policy',type=Path,required=True);init.add_argument('--initial-deployment-authorized',action='store_true')
    check=sub.add_parser('validate-wallet');check.add_argument('--policy',type=Path,required=True)
    halt=sub.add_parser('halt');halt.add_argument('--kind',choices=['entries','all'],required=True)
    args=parser.parse_args();root=args.app_root.resolve();folder=root/'runtime/executor/solana-live'
    if args.command=='status':
        result={'implementation':'restricted_classic_spl_raydium_cp','live_execution_ready':False,'model_gate':model_status(root/'rug_desk'),
                'deployed':(folder/'policy.json').exists(),'scope':'No key loaded or provider called'}
        if (folder/'policy.json').exists():
            policy=json.loads((folder/'policy.json').read_text(encoding='utf-8-sig'))
            with closing(SolanaJournal(folder/'ledger.sqlite3',policy)) as j:result['snapshot']=j.snapshot()
    elif args.command=='probe':
        wallet=(root/'runtime/solana-trading-address.txt').read_text(encoding='utf-8-sig').strip()
        result=probe(reader(root),wallet);atomic(root/'runtime/live-preparation/solana-cp-execution-probe.json',encode(result))
    elif args.command=='validate-wallet':
        result=wallet_validation(root,json.loads(args.policy.read_text(encoding='utf-8-sig')))
        atomic(root/'runtime/live-preparation/solana-policy-wallet-validation.json',encode(result))
    elif args.command=='initialize-live':
        if not args.initial_deployment_authorized:parser.error('Explicit initial deployment authorization is required')
        with process_lock(root/'runtime/executor/solana-initialize.lock'):result=initialize(root,args.policy)
    elif args.command=='halt':
        p=json.loads((folder/'policy.json').read_text(encoding='utf-8-sig'))
        with closing(SolanaJournal(folder/'ledger.sqlite3',p)) as j:j.set_halt(args.kind,'User-controlled Solana halt')
        result={'halt':args.kind,'reconciliation_continues':True}
    else:
        if not args.allow_live_submission:parser.error('This command requires an explicitly approved live deployment')
        if args.command=='run' and not 5<=args.interval<=120:parser.error('Interval must be 5..120 seconds')
        while True:
            try:result=cycle(root,collect=args.command=='collect')
            except Exception as exc:
                result={'health':'ERROR','error_type':type(exc).__name__,'observed_at':int(time.time()),
                        'live_execution_ready':False,'scope':'State retained; no uncertain operation is counted as a fill'}
                atomic(folder/('collector-status.json' if args.command=='collect' else 'status.json'),encode(result))
            if args.command!='run':break
            time.sleep(args.interval)
    print(encode(result));return 2 if result.get('health')=='ERROR' else 0

if __name__=='__main__':raise SystemExit(main())
