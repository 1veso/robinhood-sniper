import copy
import importlib.util
import unittest
from unittest.mock import patch

from sniper.execution_evm import (EvmExecutionAdapter, RestrictedEvmSigner,
    ExecutionRpcClient, ExecutionRejected, SubmissionUnknown, code_sha256,
    swap_calldata, approval_calldata, decode_signed, simulated_output)
from sniper.adapters.evm import FACTORY, WETH, USDG, TRANSFER_TOPIC, SWAP_TOPIC
from sniper.adapters.v3_route import QUOTER


WALLET='0x'+'11'*20
TOKEN='0x'+'22'*20
POOL='0x'+'33'*20
ROUTER='0x'+'44'*20
GENESIS='0x'+'55'*32
BLOCK='0x'+'66'*32
NOW=1000
CODE='0x60006000'

def word(n):return f'{n:064x}'
def addr(a):return a[2:].zfill(64)

def policy():
    return {'account_id':'live-test','policy_hash':'policy-test','chain_id':4663,
        'wallet':WALLET,'router':ROUTER,'router_abi':'uniswap_v3_swaprouter01',
        'base_token':USDG,'genesis_hash':GENESIS,'max_order_raw':'25000000',
        'max_tx_gas_wei':'1000000000000000','max_gas_price_wei':'1000000000',
        'max_gas_limit':500000,'max_slippage_bps':100,'max_quote_age_seconds':30,
        'confirmations':5,'require_finalized':True,'deployment_authorized':False,
        **{k:code_sha256(CODE) for k in ('router_code_sha256','factory_code_sha256',
            'quoter_code_sha256','base_token_code_sha256')}}


class FakeRpc:
    def __init__(self):
        self.allowance=25000000
        self.nonce=7
        self.pending=7
        self.now=NOW
        self.writes=[]
        self.reorg=False
        self.receipt=None
        self.tx=None
        self.before={USDG:1000000000,TOKEN:0}
        self.after=dict(self.before)
        self.native_before=10**18
        self.native_after=10**18
        self.old_balance=False
    def call(self,method,params=None):
        params=params or []
        if method=='eth_chainId':return hex(4663)
        if method=='eth_blockNumber':return hex(110)
        if method=='eth_getBlockByNumber':
            return {'number':hex(110 if params[0]=='finalized' else 100),
                    'hash':GENESIS if params[0]=='0x0' else ('0x'+'99'*32 if self.reorg else BLOCK),
                    'timestamp':hex(self.now)}
        if method=='eth_getCode':return '0x' if params[0]==WALLET else CODE
        if method=='eth_getStorageAt':return '0x'+'0'*64
        if method=='eth_getTransactionCount':return hex(self.pending if params[1]=='pending' else self.nonce)
        if method=='eth_gasPrice':return hex(10**8)
        if method=='eth_estimateGas':return hex(100000)
        if method=='eth_getBalance':return hex(self.native_before if params[1]=='0x63' else self.native_after)
        if method=='eth_getTransactionReceipt':return self.receipt
        if method=='eth_getTransactionByHash':return self.tx
        if method=='eth_call':
            target,data=params[0]['to'],params[0]['data'];sel=data[:10]
            if sel=='0xc45a0155':return '0x'+addr(FACTORY)
            if sel=='0x4aa4a4fc':return '0x'+addr(WETH)
            if sel=='0x0dfe1681':return '0x'+addr(USDG)
            if sel=='0xd21220a7':return '0x'+addr(TOKEN)
            if sel=='0xddca3f43':return '0x'+word(3000)
            if sel=='0x1698ee82':return '0x'+addr(POOL)
            if sel=='0xc6a5026a':return '0x'+word(50000000)+word(1)+word(1)+word(100000)
            if sel=='0x70a08231':return '0x'+word((self.before if params[1]=='0x63' else self.after)[target])
            if sel=='0xdd62ed3e':return '0x'+word(self.allowance)
            if sel=='0x414bf389':return '0x'+word(50000000)
            if sel=='0x5ae401dc':return '0x'+''.join(word(n) for n in (32,1,32,32,50000000))
            if sel=='0x095ea7b3':return '0x'+word(1)
        raise AssertionError((method,params))
    def send_raw(self,raw):self.writes.append(raw);return self.tx['hash']


class EvmExecutorTests(unittest.TestCase):
    def setUp(self):
        self.rpc=FakeRpc();self.policy=policy()
        self.backend=EvmExecutionAdapter(self.policy,self.rpc,clock=lambda:NOW)
        self.intent={'id':'intent-1','account_id':'live-test','policy_hash':'policy-test','side':'buy',
            **self.backend.quote_swap(USDG,TOKEN,POOL,'25000000')}

    def test_quote_and_preparation_bind_single_pool_wallet_and_expiry(self):
        prepared=self.backend.prepare(self.intent)
        self.assertEqual(prepared['tx']['to'],ROUTER)
        self.assertEqual(prepared['tx']['data'],swap_calldata(self.intent,WALLET))
        self.assertEqual(prepared['nonce'],7)
        self.assertEqual(prepared['max_fee_wei'],'12500000000000')
        self.assertEqual(prepared['expires_at'],1030)
        self.assertEqual(prepared['tx']['value'],0)
        self.assertEqual(self.rpc.writes,[])

    def test_finality_latency_requires_policy_and_rejects_long_delay(self):
        with self.assertRaises(ExecutionRejected): self.backend.check_finality_latency()
        self.backend.policy['max_finality_lag_seconds']=60
        original=self.rpc.call
        def observed(method, params=None):
            if method=='eth_getBlockByNumber' and params[0] in ('latest','finalized','0x64','0x5a'):
                final=params[0] in ('finalized','0x5a')
                return {'number':hex(90 if final else 100),'hash':BLOCK,
                        'timestamp':hex(NOW-self.lag if final else NOW)}
            return original(method,params)
        self.rpc.call=observed
        self.lag=978
        with self.assertRaises(ExecutionRejected): self.backend.check_finality_latency()
        self.lag=20
        self.assertEqual(self.backend.check_finality_latency()['lag_seconds'],20)
        self.lag=-1
        with self.assertRaises(ExecutionRejected): self.backend.check_finality_latency()

    def test_finality_entry_limit_does_not_disable_exit_quote(self):
        self.backend.policy['max_finality_lag_seconds']=None
        with self.assertRaises(ExecutionRejected): self.backend.check_finality_latency()
        result=self.backend.quote_swap(TOKEN,USDG,POOL,'25000000')
        self.assertEqual(result['token_in'],TOKEN)

    def test_router02_exact_single_call_deadline_wrapper(self):
        profile=policy();profile['router_abi']='uniswap_v3_swaprouter02'
        backend=EvmExecutionAdapter(profile,self.rpc,clock=lambda:NOW)
        prepared=backend.prepare(self.intent)
        data=prepared['tx']['data']
        self.assertEqual(data[:10],'0x5ae401dc')
        words=[data[i:i+64] for i in range(10,330,64)]
        self.assertEqual([int(w,16) for w in words],[1030,64,1,32,228])
        self.assertEqual(data[330:338],'04e45aaf')
        self.assertEqual(len(bytes.fromhex(data[2:])),420)
        altered=copy.deepcopy(prepared);altered['tx']['data']+='00'*32
        with self.assertRaises(ExecutionRejected):backend.validate_prepared(altered)

    def test_router02_simulation_requires_single_canonical_bytes_result(self):
        encoded='0x'+''.join(word(n) for n in (32,1,32,32,50000000))
        self.assertEqual(simulated_output(encoded,'uniswap_v3_swaprouter02'),50000000)
        for values in ((32,2,32,32,50000000),(64,1,32,32,50000000),
                       (32,1,64,32,50000000),(32,1,32,64,50000000),
                       (50000000,),(32,1,32,32,50000000,0)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    simulated_output('0x'+''.join(word(n) for n in values),'uniswap_v3_swaprouter02')

    @unittest.skipUnless(importlib.util.find_spec('eth_abi'),'optional executor ABI dependency not installed')
    def test_router02_encoding_matches_independent_abi_library(self):
        from eth_abi import encode
        from eth_utils import keccak
        inner=keccak(text='exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))')[:4]
        inner+=encode(['(address,address,uint24,address,uint256,uint256,uint160)'],
            [(USDG,TOKEN,3000,WALLET,25000000,49500000,0)])
        outer=keccak(text='multicall(uint256,bytes[])')[:4]+encode(['uint256','bytes[]'],[1030,[inner]])
        self.assertEqual(swap_calldata(self.intent,WALLET,'uniswap_v3_swaprouter02'),'0x'+outer.hex())
        response=encode(['bytes[]'],[[encode(['uint256'],[50000000])]])
        self.assertEqual(simulated_output('0x'+response.hex(),'uniswap_v3_swaprouter02'),50000000)

    def test_approval_resets_existing_allowance_then_exact_amount(self):
        self.rpc.allowance=2**256-1
        requirement=self.backend.prepare(self.intent)
        self.assertTrue(requirement['requires_approval'])
        reset=requirement['approval_intent']
        self.assertEqual(reset['kind'],'approval_reset')
        prepared=self.backend.prepare(reset)
        self.assertEqual(prepared['tx']['data'],approval_calldata(ROUTER,0))
        self.rpc.allowance=0
        approval=self.backend.prepare(self.intent)['approval_intent']
        self.assertEqual(approval['approval_amount_raw'],'25000000')
        self.assertEqual(self.backend.prepare(approval)['tx']['data'],approval_calldata(ROUTER,25000000))
        approval['approval_amount_raw']=str(2**256-1)
        with self.assertRaises(ExecutionRejected):self.backend.prepare(approval)

    def test_stale_quote_nonce_cap_wrong_deployment_block(self):
        for mutate in (
            lambda i:i.update(quote_at=900),lambda i:i.update(deadline=2000),
            lambda i:i.update(amount_in_raw='999999999'),lambda i:i.update(min_output_raw='1'),
            lambda i:i.update(pool='0x'+'77'*20),lambda i:i.update(expected_nonce=8)):
            i=copy.deepcopy(self.intent);mutate(i)
            with self.subTest(i=i):
                with self.assertRaises(ExecutionRejected):self.backend.prepare(i)
        self.rpc.pending=8
        with self.assertRaises(ExecutionRejected):self.backend.prepare(self.intent)
        self.rpc.pending=7
        bad=copy.deepcopy(self.policy);bad['router_code_sha256']='0x'+'88'*32
        with self.assertRaises(ExecutionRejected):EvmExecutionAdapter(bad,self.rpc,clock=lambda:NOW).prepare(self.intent)

    def test_claimed_quote_cannot_weaken_independent_output_floor(self):
        intent=copy.deepcopy(self.intent)
        intent.update(quoted_output_raw='1',min_output_raw='1')
        with self.assertRaises(ExecutionRejected):self.backend.prepare(intent)

    def test_base_proxy_implementation_must_be_independently_pinned(self):
        from sniper.execution_evm import IMPLEMENTATION_SLOT
        implementation='0x'+'ab'*20
        original=self.rpc.call
        def proxy(method,params=None):
            if method=='eth_getStorageAt' and params[0]==USDG and params[1]==IMPLEMENTATION_SLOT:
                return '0x'+addr(implementation)
            return original(method,params)
        self.rpc.call=proxy
        with self.assertRaises(ExecutionRejected):self.backend.prepare(self.intent)
        self.policy['base_token_proxy']={'kind':'eip1967','implementation':implementation,
            'implementation_code_sha256':code_sha256(CODE)}
        backend=EvmExecutionAdapter(self.policy,self.rpc,clock=lambda:NOW)
        self.assertIn('tx',backend.prepare(self.intent))
        backend.policy['base_token_proxy']['implementation']='0x'+'cd'*20
        with self.assertRaises(ExecutionRejected):backend.prepare(self.intent)

    def test_expired_recovery_relaxes_clock_but_not_policy_or_calldata(self):
        prepared=self.backend.prepare(self.intent)
        self.backend.clock=lambda:2000
        self.assertTrue(self.backend.validate_prepared(prepared,allow_expired=True))
        with self.assertRaises(ExecutionRejected):self.backend.validate_prepared(prepared)
        signed={'prepared':prepared,'raw_transaction':'0x','tx_hash':'0x'+'aa'*32,'nonce':7}
        signed['prepared']['intent']['account_id']='different-account'
        with self.assertRaises(ExecutionRejected):self.backend._validate_signed(signed,allow_expired=True)

    def test_prepared_tampering_fails_before_key_read(self):
        prepared=self.backend.prepare(self.intent)
        for key,value in [('to',TOKEN),('value',1),('data','0x'),('nonce',8),('gas',50000000)]:
            altered=copy.deepcopy(prepared);altered['tx'][key]=value
            with self.subTest(key=key):
                with self.assertRaises(ExecutionRejected):self.backend.validate_prepared(altered)
        reads=[]
        signer=RestrictedEvmSigner(self.backend,lambda:reads.append(1),lambda _:True)
        with self.assertRaises(ExecutionRejected):signer.sign(prepared)
        self.assertEqual(reads,[])
        self.backend.policy['deployment_authorized']=True
        signer=RestrictedEvmSigner(self.backend,lambda:reads.append(1),lambda _:False)
        with self.assertRaises(ExecutionRejected):signer.sign(prepared)
        self.assertEqual(reads,[])

    def test_nonfinalized_policy_and_changed_submission_chain_are_rejected(self):
        unsafe=copy.deepcopy(self.policy);unsafe['require_finalized']=False
        with self.assertRaises(ExecutionRejected):EvmExecutionAdapter(unsafe,self.rpc)
        prepared=self.backend.prepare(self.intent)
        self.backend.policy['deployment_authorized']=True
        signed={'prepared':prepared,'raw_transaction':'0x1234','tx_hash':'0x'+'aa'*32,'nonce':7}
        original=self.rpc.call
        self.rpc.call=lambda method,params=None:'0x1' if method=='eth_chainId' else original(method,params)
        with patch.object(self.backend,'_validate_signed'):
            with self.assertRaises(ExecutionRejected):self.backend.broadcast(signed)
        self.assertEqual(self.rpc.writes,[])

    def test_broadcast_requires_final_durable_authority_after_rpc_checks(self):
        prepared=self.backend.prepare(self.intent)
        self.backend.policy['deployment_authorized']=True
        signed={'prepared':prepared,'raw_transaction':'0x1234','tx_hash':'0x'+'aa'*32,'nonce':7}
        with patch.object(self.backend,'_validate_signed'):
            with self.assertRaises(ExecutionRejected):self.backend.broadcast(signed)
            calls=[]
            self.backend.authorize_broadcast=lambda value:calls.append(value['tx_hash']) or False
            with self.assertRaises(ExecutionRejected):self.backend.broadcast(signed)
        self.assertEqual(calls,[signed['tx_hash']])
        self.assertEqual(self.rpc.writes,[])

    def test_disabled_transport_cannot_broadcast(self):
        called=[]
        client=ExecutionRpcClient('http://127.0.0.1:8545',transport=lambda x:called.append(x))
        with self.assertRaises(ExecutionRejected):client.send_raw('0x1234')
        self.assertEqual(called,[])
        client.broadcast_enabled=True
        with self.assertRaises(SubmissionUnknown):client.send_raw('0x1234')
        self.assertEqual(len(called),1)

    def test_unknown_receipt_never_means_failed_or_expired(self):
        signed={'prepared':self.backend.prepare(self.intent),'tx_hash':'0x'+'aa'*32}
        with patch.object(self.backend,'_validate_signed'):
            self.assertEqual(self.backend.reconcile({'signed':signed})['status'],'pending')
            self.rpc.nonce=20;self.rpc.pending=20
            self.assertEqual(self.backend.reconcile({'signed':signed})['status'],'pending')

    def receipt_fixture(self,kind='swap',status=1):
        prepared=self.backend.prepare(self.intent)
        if kind!='swap':
            self.rpc.allowance=0
            self.intent=self.backend.prepare(self.intent)['approval_intent']
            prepared=self.backend.prepare(self.intent)
            self.rpc.allowance=25000000
        h='0x'+'aa'*32
        signed={'prepared':prepared,'tx_hash':h}
        fee=100000*10**8
        self.rpc.native_after-=fee
        self.rpc.tx={'from':WALLET,'to':prepared['tx']['to'],'input':prepared['tx']['data'],
            'value':'0x0','nonce':'0x7','hash':h,'blockHash':BLOCK,'blockNumber':'0x64','chainId':hex(4663)}
        self.rpc.receipt={'transactionHash':h,'blockHash':BLOCK,'blockNumber':'0x64','status':hex(status),
            'gasUsed':hex(100000),'effectiveGasPrice':hex(10**8),'logs':[]}
        if kind=='swap' and status:
            self.rpc.after[USDG]-=25000000;self.rpc.after[TOKEN]+=50000000
            common={'transactionHash':h,'blockHash':BLOCK,'blockNumber':'0x64','removed':False}
            self.rpc.receipt['logs']=[
                {**common,'logIndex':'0x0','address':USDG,'topics':[TRANSFER_TOPIC,'0x'+addr(WALLET),'0x'+addr(POOL)],'data':'0x'+word(25000000)},
                {**common,'logIndex':'0x1','address':TOKEN,'topics':[TRANSFER_TOPIC,'0x'+addr(POOL),'0x'+addr(WALLET)],'data':'0x'+word(50000000)},
                {**common,'logIndex':'0x2','address':POOL,'topics':[SWAP_TOPIC,'0x'+addr(ROUTER),'0x'+addr(WALLET)],
                 'data':'0x'+''.join(word(n) for n in (25000000,2**256-50000000,2**96,1000000000,0))}]
        return {'signed':signed}

    def test_finalized_balances_logs_and_actual_fee_required(self):
        order=self.receipt_fixture()
        with patch.object(self.backend,'_validate_signed'):
            result=self.backend.reconcile(order)
            self.assertEqual(result['status'],'confirmed')
            self.assertEqual(result['fill']['input_raw'],'25000000')
            self.assertEqual(result['fill']['output_raw'],'50000000')
            self.assertEqual(result['fill']['fee_wei'],'10000000000000')
            self.assertFalse(result['fill']['pnl_complete'])
            self.rpc.after[TOKEN]+=1
            self.assertEqual(self.backend.reconcile(order)['status'],'unknown')
            self.rpc.after[TOKEN]-=1;self.rpc.native_after-=1
            self.assertEqual(self.backend.reconcile(order)['status'],'unknown')

    def test_malformed_or_unrelated_pool_event_cannot_confirm_balanced_transfers(self):
        order=self.receipt_fixture()
        with patch.object(self.backend,'_validate_signed'):
            original=self.rpc.receipt['logs'][2]['data']
            self.rpc.receipt['logs'][2]['data']='0x'
            self.assertEqual(self.backend.reconcile(order)['status'],'unknown')
            self.rpc.receipt['logs'][2]['data']=original
            self.rpc.receipt['logs'][2]['topics'][2]='0x'+addr(TOKEN)
            self.assertEqual(self.backend.reconcile(order)['status'],'unknown')

    def test_revert_is_expense_only_and_reorg_never_fill(self):
        order=self.receipt_fixture(status=0)
        with patch.object(self.backend,'_validate_signed'):
            result=self.backend.reconcile(order)
            self.assertEqual(result['status'],'reverted')
            self.assertEqual(result['fill']['input_raw'],'0')
            self.rpc.reorg=True
            self.assertEqual(self.backend.reconcile(order)['status'],'reorged')

    def test_approval_confirmation_is_not_swap(self):
        order=self.receipt_fixture(kind='approval')
        with patch.object(self.backend,'_validate_signed'):
            result=self.backend.reconcile(order)
            self.assertEqual(result['status'],'confirmed')
            self.assertEqual(result['fill']['kind'],'approval')
            self.assertEqual(result['fill']['input_raw'],'0')

    @unittest.skipUnless(importlib.util.find_spec('eth_account'),'optional executor crypto dependency not installed')
    def test_real_crypto_signing_with_isolated_known_test_key(self):
        from eth_account import Account
        # Publicly known deterministic TEST key. Never deployed or funded.
        key=bytes.fromhex('01'*32)
        self.policy['wallet']=Account.from_key(key).address.lower()
        self.policy['deployment_authorized']=True
        backend=EvmExecutionAdapter(self.policy,self.rpc,clock=lambda:NOW)
        prepared=self.backend.prepare(self.intent)
        prepared['tx']['data']=swap_calldata(self.intent,backend.wallet)
        signer=RestrictedEvmSigner(backend,lambda:key,lambda _:True)
        with patch.object(backend,'validate_prepared',return_value=True):
            signed=signer.sign(prepared)
        decoded=decode_signed(signed['raw_transaction'])
        self.assertEqual(decoded['wallet'],backend.wallet)
        self.assertEqual(decoded['transaction'],prepared['tx'])
        self.assertEqual(decoded['tx_hash'],signed['tx_hash'])
        altered=copy.deepcopy(signed);altered['prepared']['tx']['value']=1
        with self.assertRaises(ExecutionRejected):backend._validate_signed(altered,allow_expired=True)

