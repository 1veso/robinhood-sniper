import copy
import unittest
from sniper.holder_evidence import collect, TOKEN_2022, MAX_ACCOUNTS
from sniper.adapters.solana_execution import MAINNET_GENESIS
from test_rug_desk import MINT, POOL, NOW


class HolderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.info={'supply':'100','decimals':6,'isInitialized':True}
        self.account={'owner':TOKEN_2022,'executable':False,'data':{'parsed':{'type':'mint','info':self.info}}}
        self.rows=[{'pubkey':POOL,'account':{'owner':TOKEN_2022,'executable':False,
            'data':{'parsed':{'type':'account','info':{'mint':MINT,'owner':MINT,
                'state':'initialized','tokenAmount':{'amount':'100','decimals':6}}}}}}]

    def rpc(self,method,params):
        self.calls.append((method,params))
        if method=='getGenesisHash':return MAINNET_GENESIS
        if method=='getAccountInfo':return {'context':{'slot':123},'value':copy.deepcopy(self.account)}
        if method=='getProgramAccounts':return {'context':{'slot':123},'value':copy.deepcopy(self.rows)}
        if method=='getBlock':return {'blockTime':NOW,'blockhash':'fixture-canonical'}
        raise AssertionError('Unexpected method: '+method)

    def test_token2022_enumerates_extensions_and_never_claims_controller_identity(self):
        out=collect(self.rpc,MINT,lambda:NOW)
        self.assertEqual(out['status'],'VALID');self.assertEqual(out['value'],1)
        opts=next(params[1] for name,params in self.calls if name=='getProgramAccounts')
        self.assertEqual(opts['filters'],[{'memcmp':{'offset':0,'bytes':MINT}}])
        self.assertEqual(out['controller_independence'],'UNKNOWN')
        self.assertEqual(out['observed_at'],NOW)

    def test_supply_gap_duplicate_and_oversized_snapshots_cannot_pass(self):
        self.rows[0]['account']['data']['parsed']['info']['tokenAmount']['amount']='99'
        self.assertEqual(collect(self.rpc,MINT,lambda:NOW)['status'],'UNKNOWN')
        self.rows*=2
        with self.assertRaises(ValueError):collect(self.rpc,MINT,lambda:NOW)
        self.rows=self.rows[:1]*(MAX_ACCOUNTS+1)
        with self.assertRaises(ValueError):collect(self.rpc,MINT,lambda:NOW)

    def test_mint_change_and_stale_canonical_time_are_rejected(self):
        count=0
        def changed(method,params):
            nonlocal count
            out=self.rpc(method,params)
            if method=='getAccountInfo':
                count+=1
                if count==2:out['value']['data']['parsed']['info']['supply']='101'
            return out
        with self.assertRaises(ValueError):collect(changed,MINT,lambda:NOW)
        with self.assertRaises(ValueError):collect(self.rpc,MINT,lambda:NOW+31)

    def test_pagination_requires_terminal_cursor_and_same_slot(self):
        def pages(slot=123,cursor=None):
            def rpc(method,params):
                if method!='getProgramAccountsV2':return self.rpc(method,params)
                return {'context':{'slot':slot},'value':{'accounts':copy.deepcopy(self.rows),'paginationKey':cursor}}
            return rpc
        out=collect(pages(),MINT,lambda:NOW,method='getProgramAccountsV2')
        self.assertEqual(out['value'],1)
        with self.assertRaises(ValueError):collect(pages(cursor='repeat'),MINT,lambda:NOW,method='getProgramAccountsV2')
        calls=0
        def drift(method,params):
            nonlocal calls
            if method!='getProgramAccountsV2':return self.rpc(method,params)
            calls+=1
            return {'context':{'slot':122+calls},'value':{'accounts':[], 'paginationKey':'next' if calls==1 else None}}
        with self.assertRaises(ValueError):collect(drift,MINT,lambda:NOW,method='getProgramAccountsV2')

    def test_short_nonterminal_page_does_not_claim_complete(self):
        calls=0
        def paged(method,params):
            nonlocal calls
            if method!='getProgramAccountsV2':return self.rpc(method,params)
            calls+=1
            return {'context':{'slot':123},'value':{'accounts':[] if calls==1 else copy.deepcopy(self.rows),
                'paginationKey':'continue' if calls==1 else None}}
        result=collect(paged,MINT,lambda:NOW,method='getProgramAccountsV2')
        self.assertEqual(result['context']['pages'],2);self.assertEqual(result['status'],'VALID')

    def test_complete_snapshot_larger_than_old_4096_bound(self):
        from sniper.solana_wire import b58encode
        from sniper.crew_review import _holders
        original=self.rows[0];self.info['supply']='4499';self.rows=[]
        for i in range(4499):
            row=copy.deepcopy(original);row['pubkey']=b58encode((i+1).to_bytes(32,'big'))
            info=row['account']['data']['parsed']['info'];info['owner']=row['pubkey'];info['tokenAmount']['amount']='1'
            self.rows.append(row)
        result=collect(self.rpc,MINT,lambda:NOW)
        self.assertEqual(result['value'],4499)
        _holders({'holder_coverage':result,'observations':{'holders_now':result},
            'dossier_fields':{'observations':{'holders_now':{'value':4499,'observed_at':NOW}}}})
