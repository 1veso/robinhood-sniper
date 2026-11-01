import copy
import struct
import unittest
import importlib.util
from sniper import pumpswap_evidence as p
from sniper import solana_cpmm as cp
from sniper.adapters.solana_execution import USDC
from sniper.solana_wire import b58encode


@unittest.skipUnless(importlib.util.find_spec('solders'),'Optional PDA dependency unavailable')
class PumpEvidenceTests(unittest.TestCase):
    def setUp(self):
        from test_solana_executor import account,token
        self.account=account;self.token=token
        self.quote=getattr(self,"quote",cp.WSOL)
        self.mint=b58encode(bytes([17])*32);self.creator=cp.pda([b'pool-authority',cp.b58decode(self.mint)],p.PUMP)[0]
        self.pool,bump=cp.pda([b'pool',bytes(2),cp.b58decode(self.creator),cp.b58decode(self.mint),cp.b58decode(self.quote)],p.PROGRAM)
        self.vaults=[cp.pda([cp.b58decode(self.pool),cp.b58decode(cp.TOKEN),cp.b58decode(m)],cp.ATA)[0] for m in (self.mint,self.quote)]
        raw=bytearray(301);raw[:8]=p.discriminator('Pool');raw[8]=bump
        for key,offset in ((self.creator,11),(self.mint,43),(self.quote,75),(cp.pda([b'pool_lp_mint',cp.b58decode(self.pool)],p.PROGRAM)[0],107),(self.vaults[0],139),(self.vaults[1],171),(self.mint,211)):
            raw[offset:offset+32]=cp.b58decode(key)
        raw[245:261]=(-100000000).to_bytes(16,'little',signed=True)
        glob=bytearray(949);glob[:8]=p.discriminator('GlobalConfig')
        fee=bytearray(69+40+4+24);fee[:8]=p.discriminator('FeeConfig');fee[8]=cp.pda([b'fee_config',cp.b58decode(p.PROGRAM)],p.FEE_PROGRAM)[1]
        struct.pack_into('<QQQ',fee,41,20,5,30);struct.pack_into('<I',fee,65,1);struct.pack_into('<QQQ',fee,85,20,5,30)
        self.accounts={self.pool:account(raw,p.PROGRAM),p.global_key():account(glob,p.PROGRAM),p.fee_key():account(fee,p.FEE_PROGRAM)}
        for i,m in enumerate((self.mint,self.quote)):
            mint=bytearray(82);mint[44]=6 if i==0 or self.quote==USDC else 9;mint[45]=1;struct.pack_into('<Q',mint,36,10**15)
            self.accounts[m]=account(mint,cp.TOKEN);v=token(m,self.pool,10**12)
            if i==1 and self.quote==cp.WSOL:
                import base64
                r=bytearray(cp.blob(v));struct.pack_into('<I',r,109,1);struct.pack_into('<Q',r,113,2039280)
                v=account(r,cp.TOKEN,lamports=10**12+2039280)
            self.accounts[self.vaults[i]]=v
    def state(self):return p.inspect_pool(self.pool,self.quote,self.mint,p.PROGRAM,self.accounts,1)
    def test_negative_virtual_reserves_fee_math_and_real_liquidity(self):
        s=self.state();self.assertEqual(s['reserve1'],999900000000);self.assertEqual(s['fees_bps'],[20,5,30])
        amount=100000000;net=amount*10000//10055;net-=max(0,net+sum((net*r+9999)//10000 for r in [20,5,30])-amount);net-=1
        expected=s['reserve0']*net//(s['reserve1']+net)
        self.assertEqual(p.quote_output(s,cp.WSOL,amount),expected)
        s['real_quote_reserve']=1
        with self.assertRaises(ValueError):p.quote_output(s,self.mint,amount)
    def test_pda_vault_and_unknown_extensions_rejected(self):
        with self.assertRaises(ValueError):p.inspect_pool(self.mint,cp.WSOL,self.mint,p.PROGRAM,{**self.accounts,self.mint:self.accounts[self.pool]},1)
        self.accounts[self.vaults[0]]=self.token(self.mint,self.mint,10**12)
        with self.assertRaises(ValueError):self.state()
    def test_token2022_metadata_allowed_transfer_hooks_rejected(self):
        raw=bytearray(166);raw[44:46]=bytes([6,1]);raw[165]=1
        raw+=struct.pack('<HH',18,64)+bytes(64)
        a=self.account(raw,p.TOKEN_2022);self.assertEqual(p.mint_info(a)[0],6)
        raw[166:168]=struct.pack('<H',14)
        with self.assertRaises(ValueError):p.mint_info(self.account(raw,p.TOKEN_2022))
    def test_disabled_swaps_and_unknown_pool_tail_rejected(self):
        raw=bytearray(cp.blob(self.accounts[p.global_key()]));raw[56]=8
        self.accounts[p.global_key()]=self.account(raw,p.PROGRAM)
        with self.assertRaises(ValueError):self.state()
        raw=bytearray(cp.blob(self.accounts[self.pool]));raw[-1]=1
        with self.assertRaises(ValueError):p.pool_state(self.account(raw,p.PROGRAM),1)

    def test_producer_and_independent_reviewer_recompute_pump_quote(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from decimal import Decimal
        from sniper.crew_evidence import Producer,PIN_SCOPE
        from sniper.crew_review import _exit,require_program_review
        from sniper.adapters.solana_execution import MAINNET_GENESIS
        from test_solana_executor import account
        programs=(p.PROGRAM,p.FEE_PROGRAM,cp.TOKEN,p.TOKEN_2022,cp.ATA)
        for key in programs:self.accounts[key]=account(b'fixture program',cp.IMMUTABLE,True)
        class Rpc:
            def rpc(inner,method,params):
                if method=='getGenesisHash':return MAINNET_GENESIS
                if method=='getMultipleAccounts':return {'context':{'slot':123},'value':[self.accounts.get(k) for k in params[0]]}
                if method=='getBlockTime':return 100
                if method=='getBlock':return {'blockTime':100,'blockhash':'fixture'}
                raise AssertionError(method)
        class Market:
            def quote(inner,mint,now):
                assert mint==cp.WSOL
                class Quote:price_usd=Decimal(100);observed_at=100
                return Quote()
        identity={'account_id':'fixture','config_sha256':'fixture'}
        review={'scope':PIN_SCOPE,**identity,'review_reference':'fixture:test','reviewed_at':99,'expires_at':200,
            'program_pins':{key:cp.deployment_observation(key,self.accounts) for key in programs}}
        with TemporaryDirectory() as root:
            producer=Producer(root,rpc=Rpc(),usd_market=Market(),clock=lambda:100,program_review=review)
            producer.deadline=110;producer.rpc_calls=0
            venue,route=producer._pool(identity,self.mint,self.pool,Decimal(25),Decimal('.1'))
        self.assertEqual(route['status'],'VALID');self.assertEqual(route['quote_mint'],cp.WSOL)
        obs={k:{'value':route[v],'observed_at':100} for k,v in [('exit_capacity_usd','exit_capacity_usd'),('exit_price_impact_pct','exit_price_impact_pct'),('exit_quote_ticket_usd','ticket_usd')]}
        payload={**identity,'venue':venue,'exit':route,'dossier_fields':{'observations':obs}}
        _exit(payload,{'mint':self.mint,'ticket_usd':25});require_program_review(payload,now=100)
        route['sell_output_usdc_raw']=str(int(route['sell_output_usdc_raw'])+1)
        with self.assertRaises(ValueError):_exit(payload,{'mint':self.mint,'ticket_usd':25})

    def test_usdc_quote_uses_stable_fee_tiers(self):
        self.quote=USDC;self.setUp()
        fee=bytearray(cp.blob(self.accounts[p.fee_key()]))
        # Append one distinct USDC tier after the SOL schedule.
        fee=fee[:109]+struct.pack('<I',1)+bytes(16)+struct.pack('<QQQ',1,2,3)+bytes(24)
        self.accounts[p.fee_key()]=self.account(fee,p.FEE_PROGRAM)
        s=self.state();self.assertEqual(s['decimals1'],6);self.assertEqual(s['fees_bps'],[1,2,3])
        self.assertGreater(p.quote_output(s,USDC,25000000),0)

    def test_upgradeable_program_reads_are_split_and_header_changes_rejected(self):
        from tempfile import TemporaryDirectory
        from decimal import Decimal
        from sniper.crew_evidence import Producer
        from sniper.adapters.solana_execution import MAINNET_GENESIS
        programs=(p.PROGRAM,p.FEE_PROGRAM,cp.TOKEN,p.TOKEN_2022,cp.ATA)
        code_keys=[]
        for i,key in enumerate(programs):
            address=b58encode(bytes([70+i])*32);code_keys.append(address)
            self.accounts[key]=self.account(b'\2\0\0\0'+cp.b58decode(address),cp.UPGRADEABLE,True)
            data=bytearray(2_000_100 if i==0 else 46);data[:4]=b'\3\0\0\0';data[4:12]=(10).to_bytes(8,'little');data[-1]=1
            self.accounts[address]=self.account(data,cp.UPGRADEABLE)
        calls=[];changed=False
        class Rpc:
            def rpc(inner,method,params):
                if method=='getGenesisHash':return MAINNET_GENESIS
                if method=='getMultipleAccounts':
                    keys,opts=params;calls.append((keys,opts))
                    if 'dataSlice' in opts:
                        vals=[]
                        for key in keys:
                            full=cp.blob(self.accounts[key]);offset=opts['dataSlice']['offset'];length=opts['dataSlice']['length']
                            raw=bytearray(full[offset:offset+length])
                            if changed and length==45:raw[4]=11
                            vals.append({**self.account(raw,cp.UPGRADEABLE),'space':len(full)})
                    else:vals=[self.accounts.get(key) for key in keys]
                    return {'context':{'slot':123},'value':vals}
                if method=='getBlockTime':return 100
                if method=='getBlock':return {'blockTime':100,'blockhash':'fixture'}
                raise AssertionError(method)
        with TemporaryDirectory() as root:
            producer=Producer(root,rpc=Rpc(),clock=lambda:100)
            producer.deadline=125;producer.rpc_calls=0
            venue,route=producer._pool({'account_id':'fixture','config_sha256':'fixture'},self.mint,self.pool,Decimal(25),Decimal('.1'))
            self.assertEqual(venue['reason'],'current_independent_program_review_missing')
            self.assertEqual(len(venue['context']['deployment_snapshot_slots']),5)
            self.assertTrue(any(opts.get('dataSlice',{}).get('offset')==2_000_000 for keys,opts in calls))
            self.assertTrue(all(len(keys)==1 for keys,opts in calls if any(k in code_keys for k in keys) and 'dataSlice' not in opts))
            changed=True;producer.rpc_calls=0
            with self.assertRaisesRegex(ValueError,'deployment changed'):
                producer._pool({'account_id':'fixture','config_sha256':'fixture'},self.mint,self.pool,Decimal(25),Decimal('.1'))
