"""Offline real Ed25519 signing with synthetic accounts; no mainnet effects."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

try:
    from solders.keypair import Keypair
except ImportError:
    raise unittest.SkipTest('Optional pinned Solana executor SDK is not installed')
from sniper import solana_cpmm as cp
from sniper.execution import AutomaticExecutor
from sniper.execution_solana import SolanaJournal,SolanaExecutionAdapter,RestrictedSolanaSigner
from sniper.adapters.solana_execution import MAINNET_GENESIS,USDC
from sniper.solana_wire import b58encode,decode_unsigned,compile_unsigned
from sniper.execution import encode

NOW=1793190000
KEY=Keypair.from_seed(bytes([7])*32)  # Public, disposable fixture; never a funded wallet.
WALLET=str(KEY.pubkey());MINT=str(Keypair.from_seed(bytes([8])*32).pubkey())
BH=b58encode(bytes([9])*32)


def account(data,owner=cp.PROGRAM,executable=False,lamports=2039280):
    return {'data':[base64.b64encode(data).decode(),'base64'],'owner':owner,'executable':executable,'lamports':lamports}


def token(mint,owner,amount):
    b=bytearray(165);b[:32]=cp.b58decode(mint);b[32:64]=cp.b58decode(owner)
    b[64:72]=struct.pack('<Q',amount);b[108]=1
    return account(bytes(b),cp.TOKEN)


def fixture():
    m0,m1=sorted([USDC,MINT],key=cp.b58decode)
    config,bump=cp.pda([b'amm_config',b'\0\1'],cp.PROGRAM)
    pool,_=cp.pda([b'pool',cp.b58decode(config),cp.b58decode(m0),cp.b58decode(m1)],cp.PROGRAM)
    authority,auth_bump=cp.pda([b'vault_and_lp_mint_auth_seed'],cp.PROGRAM)
    vaults=[cp.pda([b'pool_vault',cp.b58decode(pool),cp.b58decode(m)],cp.PROGRAM)[0] for m in [m0,m1]]
    observation=cp.pda([b'observation',cp.b58decode(pool)],cp.PROGRAM)[0]
    state=bytearray(637);state[:8]=hashlib.sha256(b'account:PoolState').digest()[:8]
    for i,k in enumerate([config,MINT,*vaults,BH,m0,m1,cp.TOKEN,cp.TOKEN,observation]):state[8+i*32:40+i*32]=cp.b58decode(k)
    state[328]=auth_bump;state[330:333]=bytes([6,6,6]);state[373:381]=struct.pack('<Q',NOW-100)
    conf=bytearray(236);conf[:8]=hashlib.sha256(b'account:AmmConfig').digest()[:8];conf[8]=bump;conf[10:12]=b'\1\0';conf[12:20]=struct.pack('<Q',2500)
    obs=bytearray(4075);obs[:8]=hashlib.sha256(b'account:ObservationState').digest()[:8];obs[8]=1;obs[11:43]=cp.b58decode(pool)
    accounts={pool:account(state),config:account(conf),observation:account(obs),WALLET:account(b'',cp.SYSTEM,lamports=100000000)}
    for m,v in zip([m0,m1],vaults):
        mint=bytearray(82);mint[44:46]=bytes([6,1]);accounts[m]=account(mint,cp.TOKEN)
        accounts[v]=token(m,authority,1000000000000)
        accounts[cp.ata(WALLET,m)]=token(m,WALLET,1000000000 if m==USDC else 0)
    for program in [cp.PROGRAM,cp.TOKEN,cp.ATA]:accounts[program]=account(('fixture '+program).encode(),cp.IMMUTABLE,True)
    pins={p:cp.deployment_observation(p,accounts) for p in [cp.PROGRAM,cp.TOKEN,cp.ATA]}
    policy={'account_id':'solana-offline-only','chain_id':'solana:mainnet','genesis_hash':MAINNET_GENESIS,
        'wallet':WALLET,'base_token':USDC,'base_decimals':6,'execution_target':'automatic','capital_raw':1000000000,
        'max_order_raw':25000000,'max_total_exposure_raw':100000000,'daily_loss_raw':50000000,
        'max_gas_wei_day':1000000,'max_tx_gas_wei':50000,'deployment_authorized':False,'acceptance_mode':True,
        'program_pins':pins}
    return pool,accounts,policy


class FakeRpc:
    api_key='OFFLINE_TEST_ONLY'
    def __init__(self,pool,accounts):
        self.pool,self.accounts=pool,accounts;self.now=NOW;self.calls=[];self.sent=[]
        self.status=None;self.tx=None;self.slot=100;self.height=500;self.sim_error=None
        self.blocks={100:{'blockhash':BH,'signatures':[]}}
    def verify_chain(self):self.calls.append(('verify_chain',None))
    def transport(self,method,url,payload,headers,timeout):
        from urllib.parse import urlsplit,parse_qs
        assert method=='GET' and '/build?' in url
        p=parse_qs(urlsplit(url).query);source,target=p['inputMint'][0],p['outputMint'][0];amount=int(p['amount'][0])
        state=cp.inspect_pool(self.pool,source,target,WALLET,self.accounts,self.now)
        out=cp.quote_output(state,source,amount)
        return {'inputMint':source,'outputMint':target,'swapMode':'ExactIn','inAmount':str(amount),'outAmount':str(out),
            'slippageBps':100,'otherAmountThreshold':str(out*99//100),'priceImpactPct':'0.001',
            'routePlan':[{'percent':100,'bps':10000,'swapInfo':{'inputMint':source,'outputMint':target,'inAmount':str(amount),
                'outAmount':str(out),'ammKey':self.pool,'label':'Raydium CP'}}], 'swapInstruction':{'malicious':'ignored, never signed'}}
    def rpc(self,method,params=None):
        self.calls.append((method,deepcopy(params)))
        if method=='getMultipleAccounts':return {'context':{'slot':self.slot},'value':[self.accounts.get(k) for k in params[0]]}
        if method=='getBlockTime':return self.now
        if method=='getMinimumBalanceForRentExemption':return 2039280
        if method=='getLatestBlockhash':return {'context':{'slot':self.slot},'value':{'blockhash':BH,'lastValidBlockHeight':1000}}
        if method=='getFeeForMessage':return {'context':{'slot':self.slot},'value':5000}
        if method=='simulateTransaction':return {'context':{'slot':self.slot},'value':{'err':self.sim_error,'unitsConsumed':120000}}
        if method=='isBlockhashValid':return {'context':{'slot':self.slot},'value':self.height<=1000}
        if method=='getSignatureStatuses':return {'context':{'slot':self.slot},'value':[self.status]}
        if method=='getBlockHeight':return self.height
        if method=='getSlot':return self.slot
        if method=='getBlocks':return sorted(self.blocks)
        if method=='getBlock':return self.blocks[params[0]]
        if method=='getTransaction':return self.tx
        raise AssertionError('Unexpected RPC '+method)
    def send_exact(self,signed,authorize):
        assert authorize(signed)
        self.sent.append(signed['raw_transaction'])
        return signed['tx_hash']
    def settle(self,signed,*,success=True):
        p=signed['prepared'];i=p['intent'];decoded=decode_unsigned(p['unsigned'],{});keys=decoded['keys']
        pre=[self.accounts.get(k,{}).get('lamports',0) for k in keys];post=list(pre);post[0]-=5000
        before=[];after=[]
        for name,sign in [('token_in',-1),('token_out',1)]:
            mint=i[name];index=keys.index(cp.ata(WALLET,mint));qty=cp.token_account(self.accounts[keys[index]],mint,WALLET)
            row={'accountIndex':index,'owner':WALLET,'mint':mint,'programId':cp.TOKEN,'uiTokenAmount':{'amount':str(qty),'decimals':6}}
            before.append(row);new=deepcopy(row)
            delta=int(i['amount_in_raw']) if sign==-1 else int(i['quoted_output_raw'])
            new['uiTokenAmount']['amount']=str(qty+sign*delta if success else qty);after.append(new)
        err=None if success else {'InstructionError':[2,'Custom']}
        self.status={'slot':100,'err':err,'confirmationStatus':'finalized','confirmations':None}
        self.blocks[100]['signatures']=[signed['tx_hash']]
        self.tx={'slot':100,'blockTime':self.now,'transaction':[base64.b64encode(bytes.fromhex(signed['raw_transaction'][2:])).decode(),'base64'],
            'meta':{'fee':5000,'err':err,'preBalances':pre,'postBalances':post,'preTokenBalances':before,'postTokenBalances':after,
                    'loadedAddresses':{'writable':[],'readonly':[]}}}


class SolanaExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.pool,self.accounts,self.policy=fixture()
        self.rpc=FakeRpc(self.pool,self.accounts)
        self.j=SolanaJournal(Path(self.tmp.name)/'execution.sqlite3',self.policy,create=True)
        self.adapter=SolanaExecutionAdapter(self.j.policy,self.rpc,authorize_broadcast=self.j.broadcast_authorized,clock=lambda:self.rpc.now)
        self.adapter.is_test=True
        self.adapter.signer=RestrictedSolanaSigner(self.adapter,lambda:KEY,self.j.signing_authorized)
        q=self.adapter.quote_swap(USDC,MINT,self.pool,25000000)
        self.intent={**q,**{k:self.j.policy[k] for k in ['account_id','chain_id','wallet','policy_hash']},'id':'buy:fixture','kind':'swap','side':'buy',
            'entry_evidence':{'safety_passed':True,'required_provider_response':True,'safety_at':NOW,'model_at':NOW,'model_validated':True,
             'model_verdict':'BUY','confidence':'0.9','model_request_id':'offline-model-fixture','mint':MINT,'pool':self.pool,'liquidity_usd':'100000'}}
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def sign(self):
        self.j.enqueue(self.intent,NOW)
        self.assertEqual(AutomaticExecutor(self.j,self.adapter).tick(NOW)['status'],'SIGNED')
        return self.j.order(self.intent['id'])['signed']
    def test_real_signature_persisted_before_send_and_canonical_fill_exactly_once(self):
        signed=self.sign();self.assertEqual(self.rpc.sent,[]);self.adapter.signed_message(signed)
        self.assertEqual(self.j.snapshot()['fill_count'],0)
        self.j.close();self.j=SolanaJournal(Path(self.tmp.name)/'execution.sqlite3',self.policy)
        self.adapter.authorize_broadcast=self.j.broadcast_authorized
        self.adapter.signer.authorize=self.j.signing_authorized
        self.assertEqual(AutomaticExecutor(self.j,self.adapter).tick(NOW)['status'],'SUBMITTED')
        self.assertEqual(len(self.rpc.sent),1)
        self.rpc.settle(signed)
        self.assertEqual(AutomaticExecutor(self.j,self.adapter).tick(NOW)['status'],'CONFIRMED')
        self.assertEqual(self.j.snapshot()['fill_count'],1);self.assertEqual(self.j.snapshot()['native_fees_lamports'],'5000')
        self.assertEqual(len(self.j.positions()),1)
        AutomaticExecutor(self.j,self.adapter).tick(NOW)
        self.assertEqual(self.j.snapshot()['fill_count'],1)
    def test_duplicate_missing_risk_and_model_rejections(self):
        for field in ['required_provider_response','model_validated']:
            bad=deepcopy(self.intent);bad['entry_evidence'][field]=False
            with self.assertRaises(ValueError):self.j.enqueue(bad,NOW)
        self.j.enqueue(self.intent,NOW);self.j.enqueue(self.intent,NOW)
        self.assertEqual(len(self.j.orders()),1)
        bad=deepcopy(self.intent);bad['amount_in_raw']='1'
        with self.assertRaises(ValueError):self.j.enqueue(bad,NOW)
    def test_signer_never_unlocks_without_grant_and_rejects_wrong_wallet(self):
        p=self.adapter.prepare(self.intent);calls=[]
        signer=RestrictedSolanaSigner(self.adapter,lambda:calls.append(1),lambda _:False)
        with self.assertRaises(ValueError):signer.sign(p)
        self.assertEqual(calls,[])
        signer=RestrictedSolanaSigner(self.adapter,lambda:Keypair.from_seed(bytes([1])*32),lambda _:True)
        with self.assertRaises(ValueError):signer.sign(p)
    def test_message_tampering_added_transfer_privileges_and_amount_fail(self):
        p=self.adapter.prepare(self.intent)
        for k,v in [('amount',1),('minimum',1),('wallet',MINT)]:
            bad=deepcopy(p);bad['expected'][k]=v
            with self.assertRaises(ValueError):self.adapter.validate_prepared(bad)
        decoded=decode_unsigned(p['unsigned'],{});instructions=deepcopy(decoded['instructions'])
        instructions.insert(0,cp.ix(cp.SYSTEM,[(WALLET,True,True),(MINT,False,True)],b'\2\0\0\0'+struct.pack('<Q',1)))
        wire,_=compile_unsigned(instructions,WALLET,BH,{})
        bad=deepcopy(p);bad['unsigned']=wire
        with self.assertRaises(ValueError):self.adapter.validate_prepared(bad)
    def test_account_owners_vaults_extensions_and_program_pins_fail(self):
        originals=deepcopy(self.accounts)
        for address,field,value in [(self.pool,'owner',cp.SYSTEM),(MINT,'owner','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'),(cp.PROGRAM,'data',[base64.b64encode(b'upgrade').decode(),'base64'])]:
            self.accounts[address][field]=value
            with self.assertRaises(ValueError):self.adapter.prepare(self.intent)
            self.accounts.clear();self.accounts.update(deepcopy(originals))
    def test_unfunded_missing_ata_and_failed_simulation_do_not_sign(self):
        for key in [WALLET,cp.ata(WALLET,USDC)]:
            original=self.accounts.pop(key)
            with self.assertRaises(ValueError):self.adapter.prepare(self.intent)
            self.accounts[key]=original
        self.rpc.sim_error='AccountNotFound'
        with self.assertRaises(ValueError):self.adapter.prepare(self.intent)
        self.assertEqual(self.rpc.sent,[])
    def test_receipt_mismatch_or_missing_canonical_signature_never_fills(self):
        signed=self.sign();self.rpc.settle(signed)
        self.rpc.blocks[100]['signatures']=[]
        with self.assertRaises(ValueError):self.adapter.reconcile(self.j.order(self.intent['id']))
        self.assertEqual(self.j.snapshot()['fill_count'],0)
    def test_reverted_transaction_records_fee_without_position(self):
        signed=self.sign();self.rpc.settle(signed,success=False)
        self.assertEqual(AutomaticExecutor(self.j,self.adapter).tick(NOW)['status'],'REVERTED')
        self.assertEqual(self.j.snapshot()['fill_count'],0);self.assertEqual(self.j.positions(),[])
        self.assertEqual(self.j.snapshot()['native_fees_lamports'],'5000')
    def test_expiry_requires_full_canonical_scan_and_preserves_ambiguous_identity(self):
        self.sign();self.rpc.height=1001;self.rpc.slot=101
        self.rpc.blocks[101]={'blockhash':b58encode(bytes([10])*32),'signatures':[]}
        result=AutomaticExecutor(self.j,self.adapter).tick(NOW)
        self.assertEqual(result['status'],'EXPIRED');self.assertEqual(self.rpc.sent,[])
        self.assertEqual(self.j.snapshot()['fill_count'],0)
    def test_expired_but_signature_in_chain_is_not_dropped(self):
        signed=self.sign();self.rpc.height=1001;self.rpc.blocks[100]['signatures']=[signed['tx_hash']]
        self.assertEqual(AutomaticExecutor(self.j,self.adapter).tick(NOW)['status'],'UNKNOWN')
        self.assertEqual(self.j.snapshot()['reserved_base_raw'],'25000000')


if __name__=='__main__':unittest.main()
