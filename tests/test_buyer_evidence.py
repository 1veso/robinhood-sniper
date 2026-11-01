import copy
import unittest
from sniper.buyer_evidence import receipt, collect, BUY, decode58
from sniper import pumpswap_evidence as pump
from sniper.solana_wire import b58encode
from sniper.adapters.solana_execution import MAINNET_GENESIS
from test_rug_desk import MINT,POOL,NOW

WALLET=b58encode(bytes([5])*32);TOKEN=b58encode(bytes([6])*32)
SIG=b58encode(bytes([7])*64)


def transaction():
    return {'slot':123,'blockTime':NOW,'transaction':{'signatures':[SIG],'message':{
        'accountKeys':[{'pubkey':WALLET,'signer':True},{'pubkey':TOKEN,'signer':False}],
        'instructions':[{'programId':pump.PROGRAM,'data':b58encode(next(iter(BUY))+bytes(16)),
        'accounts':[POOL,WALLET,MINT,MINT,pump.cp.WSOL,TOKEN,TOKEN,TOKEN,TOKEN]}]}},
        'meta':{'err':None,'preTokenBalances':[],
        'postTokenBalances':[{'accountIndex':1,'mint':MINT,'owner':WALLET,'uiTokenAmount':{'amount':'50'}}]}}


class BuyerEvidenceTests(unittest.TestCase):
    def test_signed_receipt_is_authority_evidence_not_controller_identity(self):
        out=receipt(transaction(),SIG,MINT,POOL,NOW-3600,NOW)
        self.assertEqual(out['buyers'][0]['net_acquired_raw'],'50')
        self.assertEqual(out['controller_independence'],'UNKNOWN')
        self.assertEqual(len(decode58(SIG)),64)
    def test_failure_spoof_missing_signature_and_multiple_buys_rejected(self):
        for mode in ('failed','signature','owner','multi','truncated','time'):
            tx=transaction()
            if mode=='failed':tx['meta']['err']={'InstructionError':[0,1]}
            if mode=='signature':tx['transaction']['message']['accountKeys'][0]['signer']=False
            if mode=='owner':tx['meta']['postTokenBalances'][0]['owner']=POOL
            if mode=='multi':tx['transaction']['message']['instructions']*=2
            if mode=='truncated':tx['transaction']['message']['instructions'][0]['data']=b58encode(next(iter(BUY)))
            if mode=='time':tx['blockTime']=NOW+1
            with self.subTest(mode=mode),self.assertRaises(ValueError):receipt(tx,SIG,MINT,POOL,NOW-3600,NOW)
    def test_unrelated_program_and_pool_never_count(self):
        tx=transaction();tx['transaction']['message']['instructions'][0]['programId']=MINT
        self.assertEqual(receipt(tx,SIG,MINT,POOL,NOW-3600,NOW)['buyers'],[])
        self.assertEqual(receipt(transaction(),SIG,MINT,MINT,NOW-3600,NOW)['buyers'],[])
    def test_collection_is_bounded_and_preserves_unknown_independence(self):
        calls=[]
        def rpc(method,params):
            calls.append(method)
            if method=='getGenesisHash':return MAINNET_GENESIS
            if method=='getSignaturesForAddress':return [{'signature':SIG,'slot':123,'blockTime':NOW,'err':None}]
            if method=='getTransaction':return transaction()
            self.fail(method)
        out=collect(rpc,MINT,POOL,lambda:NOW)
        self.assertEqual(out['buyer_wallets'],[WALLET]);self.assertEqual(out['observed_buy_receipts'],1)
        self.assertIsNone(out['verified_independent_buyers']);self.assertEqual(out['status'],'UNKNOWN')
        self.assertEqual(len(calls),3)

    def test_supported_raydium_buy_has_same_authority_limit(self):
        tx=transaction();ix=tx['transaction']['message']['instructions'][0]
        ix.update(programId=pump.cp.PROGRAM,data=b58encode(pump.cp.SWAP+bytes(16)),
            accounts=[WALLET,MINT,MINT,POOL,TOKEN,TOKEN,TOKEN,TOKEN,MINT,MINT,pump.cp.WSOL,MINT,MINT])
        result=receipt(tx,SIG,MINT,POOL,NOW-3600,NOW)
        self.assertEqual(result['buyers'][0]['instruction'],'raydium_swap_base_input')
        self.assertEqual(result['controller_independence'],'UNKNOWN')
