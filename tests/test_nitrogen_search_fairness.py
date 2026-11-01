import unittest
from types import SimpleNamespace
from sniper.nitrogen.providers import Providers
from sniper.krypton.demo import address

class NitrogenSearchFairnessTests(unittest.TestCase):
    def test_noisy_first_wallet_cannot_consume_all_receipt_work(self):
        state={};requests=[]
        class EmptyDB:
            def execute(self,*args):return self
            def fetchone(self):return None
        def provider(name,value=None):
            if value is not None:state[name]=value
            return state.get(name)
        wallets=[address(n) for n in (1,2,3,4)]
        book=SimpleNamespace(cfg={'tracked_wallets':wallets,'whale_max_age_seconds':300},db=EmptyDB(),provider=provider)
        p=object.__new__(Providers);p.book=book;p.clock=lambda:1000
        def rpc(method,args):
            if method=='getSignaturesForAddress':
                requests.append(('wallet',args[0]))
                return [{'signature':args[0]+str(n),'err':None,'confirmationStatus':'finalized','blockTime':999} for n in range(20)]
            requests.append(('receipt',args[0]));return None
        p.rpc=rpc
        self.assertEqual(p.search(),[])
        self.assertEqual(len([x for x in requests if x[0]=='receipt']),4)
        self.assertEqual([x[1] for x in requests if x[0]=='receipt'],[w+'0' for w in wallets])
        requests.clear();p.search()
        self.assertEqual([x[1] for x in requests if x[0]=='wallet'],wallets[1:]+wallets[:1])
