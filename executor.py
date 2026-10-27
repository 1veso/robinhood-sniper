"""Automatic executor runner. The installed task uses SHADOW, with no signing authority."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
from supervise import atomic_json,instance_lock
from sniper.execution_service import shadow_cycle,live_cycle,collect_live,validate_deployment
from sniper.execution import ExecutionJournal,validate_policy


def initialize_live(root, policy_path):
    """Explicit initial deployment only; never called by shadow/profitability checks."""
    from sniper.execution_evm import EvmExecutionAdapter,ExecutionRpcClient
    from sniper.adapters.evm import quantity
    from sniper.adapters.evm_store import EvmStore
    from sniper.confirmation_book import ConfirmationBook
    from sniper.execution_service import OpportunityInbox
    policy=json.loads(policy_path.read_text(encoding='utf-8-sig'))
    folder=root/'runtime/executor/live'
    if folder.exists():raise ValueError('Live account already exists; never reset or overwrite it')
    # A source UUID is part of policy identity. Generate once during explicit initialization.
    policy['observation_source_id']=str(uuid.uuid4())
    policy=validate_policy(policy)
    validate_deployment(policy)
    rpc=ExecutionRpcClient((root/'runtime/quote-rpc-url.txt').read_text(encoding='utf-8-sig').strip(),timeout=8,max_retries=1)
    adapter=EvmExecutionAdapter(policy,rpc)
    anchor=adapter._anchor();tag=hex(anchor['block_number']) if 'block_number' in anchor else hex(anchor['number'])
    if adapter._balance(policy['base_token'],tag)<int(policy['capital_raw']):raise ValueError('Wallet base balance is below approved allocation')
    if adapter._uint(policy['base_token'],'0x313ce567',tag)!=policy['base_decimals']:raise ValueError('Base precision differs from approved policy')
    if quantity(rpc.call('eth_getBalance',[policy['wallet'],tag]))<int(policy['max_gas_wei_day']):raise ValueError('Native gas balance is below approved daily reservation')
    if rpc.call('eth_getCode',[policy['wallet'],tag])!='0x':raise ValueError('Only dedicated EOA execution wallets are supported')
    if quantity(rpc.call('eth_getTransactionCount',[policy['wallet'],'latest']))!=quantity(rpc.call('eth_getTransactionCount',[policy['wallet'],'pending'])):
        raise ValueError('Resolve pending wallet transactions before deployment')
    folder.mkdir(parents=True)
    with closing(EvmStore(folder/'events.sqlite3')) as store:
        store.db.execute("UPDATE evm_metadata SET value=? WHERE key='identity'",(policy['observation_source_id'],))
        ConfirmationBook(store)
    with closing(OpportunityInbox(folder/'opportunities.sqlite3')):pass
    with closing(ExecutionJournal(folder/'ledger.sqlite3',policy,create=True)) as journal:
        atomic_json(folder/'policy.json',journal.policy)
    return {'mode':'LIVE_DEPLOYMENT_INITIALIZED','signed':False,'broadcast':False,
            'account_id':policy['account_id'],'scope':'Separate live ledger initialized. Start the approved live workers explicitly.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root',type=Path,default=ROOT)
    sub=parser.add_subparsers(dest='command',required=True)
    status=sub.add_parser('status');status.add_argument('--mode',choices=('shadow','live'),default='shadow')
    halt=sub.add_parser('halt');halt.add_argument('--kind',choices=('entries','all'),required=True)
    for name in ('once','run','collect'):
        p=sub.add_parser(name);p.add_argument('--mode',choices=('shadow','live'),default='shadow')
        p.add_argument('--allow-live-submission',action='store_true')
        if name=='run':p.add_argument('--interval',type=int,default=10)
    acceptance=sub.add_parser('acceptance');acceptance.add_argument('--output',type=Path)
    init=sub.add_parser('initialize-live');init.add_argument('--policy',type=Path,required=True)
    init.add_argument('--initial-deployment-authorized',action='store_true')
    args=parser.parse_args();root=args.app_root.resolve();folder=root/'runtime/executor'
    if args.command=='status':
        path=folder/('live-status.json' if args.mode=='live' else 'status.json')
        result=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'mode':'NOT_STARTED','real_money_enabled':False}
        print(json.dumps(result,ensure_ascii=True));return 0
    if args.command=='halt':
        policy=json.loads((folder/'live/policy.json').read_text(encoding='utf-8-sig'))
        with closing(ExecutionJournal(folder/'live/ledger.sqlite3',policy)) as journal:
            journal.set_halt(args.kind,'User-controlled emergency halt')
        print(json.dumps({'halt':args.kind,'reconciliation_continues':True,'new_signing_disabled':args.kind=='all'}));return 0
    if args.command=='acceptance':
        from sniper.execution_acceptance import run_acceptance
        output=args.output or folder/'acceptance'/str(uuid.uuid4())
        result=run_acceptance(output);print(json.dumps(result));return 0
    if args.command=='initialize-live':
        if not args.initial_deployment_authorized:parser.error('Initial deployment authorization flag is required')
        folder.mkdir(parents=True,exist_ok=True)
        with instance_lock(folder/'initialize.lock'):result=initialize_live(root,args.policy)
        print(json.dumps(result));return 0
    if args.mode=='live' and not args.allow_live_submission:
        parser.error('Live workers require an explicit authorized deployment and --allow-live-submission')
    if args.command=='collect' and args.mode!='live':parser.error('Shadow uses local inputs only; no second market collector is started')
    if args.command=='run' and not 5<=args.interval<=120:parser.error('Polling interval must be 5..120 seconds')
    folder.mkdir(parents=True,exist_ok=True)
    lock=folder/('collector.lock' if args.command=='collect' else args.mode+'.lock')
    try:
        with instance_lock(lock):
            while True:
                try:
                    if args.mode=='shadow':result=shadow_cycle(root)
                    elif args.command=='collect':
                        policy=validate_deployment(json.loads((folder/'live/policy.json').read_text(encoding='utf-8-sig')))
                        result={'mode':'LIVE_COLLECTOR','real_money_enabled':False,**collect_live(root,policy)}
                    else:result=live_cycle(root)
                except Exception as exc:
                    result={'mode':args.mode.upper(),'health':'ERROR','error_type':type(exc).__name__,
                            'finished_at':int(time.time()),'real_money_enabled':None if args.mode=='live' else False,
                            'authority_status':'unverified_after_error' if args.mode=='live' else 'disabled',
                            'scope':'State retained. No failed or uncertain operation is counted as a fill.'}
                output=folder/('collector-status.json' if args.command=='collect' else 'live-status.json' if args.mode=='live' else 'status.json')
                atomic_json(output,result)
                if args.command!='run':
                    print(json.dumps(result,ensure_ascii=True));return 2 if result.get('health')=='ERROR' else 0
                time.sleep(args.interval)
    except OSError as exc:
        print(json.dumps({'mode':args.mode.upper(),'health':'ERROR','error_type':type(exc).__name__,
                          'authority_status':'unverified_after_error' if args.mode=='live' else 'disabled',
                          'real_money_enabled':None if args.mode=='live' else False,
                          'scope':'Worker lock or storage unavailable; inspect the journal before restarting.'}));return 2


if __name__=='__main__':raise SystemExit(main())
