import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from sniper.adapters.evm import RpcBudgetExceeded,ReorgError,CHAIN_ID
from sniper.adapters.evm_store import EvmStore
from sniper.exploratory_source import ExploratorySource

class CollectionResumeTests(unittest.TestCase):
    def test_restart_resumes_without_advancing_canonical_cursor_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.sqlite3';calls=[];fail=[True];tip=[1200]
            rpc=Mock(batch_enabled=False)
            def call(method,params=None):
                if method=='eth_blockNumber':return hex(tip[0]+5)
                if method=='eth_getLogs':
                    start=int(params[0]['fromBlock'],16);calls.append(start)
                    if start==500 and fail[0]:raise RpcBudgetExceeded('fixture deadline')
                    return []
                raise AssertionError(method)
            rpc.call.side_effect=call
            def source(store):
                s=ExploratorySource(rpc,store,Mock(),clock=lambda:10000)
                s.collector._recover_reorg=lambda:None
                s.collector.block=lambda n:{'number':n,'hash':'0x'+format(n,'064x'),'timestamp':10000}
                return s
            store=EvmStore(path)
            with self.assertRaises(RpcBudgetExceeded):source(store).collect()
            self.assertIsNone(store.cursor(CHAIN_ID))
            self.assertEqual(store.db.execute('SELECT count(*) FROM evm_events').fetchone()[0],0)
            store.close()
            fail[0]=False;tip[0]=1300
            store=EvmStore(path)
            try:
                result=source(store).collect()
                self.assertEqual(calls,[0,500,500,1000])
                self.assertEqual(result['through_block'],1200)
                self.assertEqual(store.cursor(CHAIN_ID)['block_number'],1200)
                self.assertEqual(store.db.execute('SELECT count(*) FROM exploration_collection_progress').fetchone()[0],0)
            finally:store.close()

    def test_changed_anchor_discards_unadmitted_progress(self):
        store=EvmStore();self.addCleanup(store.close)
        rpc=Mock(batch_enabled=False)
        rpc.call.side_effect=lambda method,params=None:'0x4b5' if method=='eth_blockNumber' else []
        s=ExploratorySource(rpc,store,Mock(),clock=lambda:10000)
        s.collector._recover_reorg=lambda:None
        reads=[0]
        def block(n):
            reads[0]+=1
            if reads[0]==2:raise RpcBudgetExceeded('fixture deadline before final proof')
            return {'number':n,'hash':'0x'+format(reads[0], '064x'),'timestamp':10000}
        s.collector.block=block
        with self.assertRaises(RpcBudgetExceeded):s.collect()
        self.assertIsNone(store.cursor(CHAIN_ID))
        with self.assertRaises(ReorgError):s.collect()
        self.assertIsNone(store.cursor(CHAIN_ID))
        self.assertEqual(store.db.execute('SELECT count(*) FROM exploration_collection_progress').fetchone()[0],0)
