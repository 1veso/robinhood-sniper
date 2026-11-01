import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from sniper.adapters.evm import RpcClient, RpcError

MESSAGE = 'Under the Free tier plan, you can make eth_getLogs requests with up to a 10 block range. Upgrade to PAYG for expanded block range.'

class LogLimitsTests(unittest.TestCase):
    def client(self, *, batch=False, fail=None):
        seen=[]
        def transport(p):
            if isinstance(p,list):
                rows=[transport(v) for v in p]
                return rows[::-1]
            q=p['params'][0]; lo=int(q['fromBlock'],16); hi=int(q['toBlock'],16)
            seen.append((lo,hi,q.get('address'),q.get('topics')))
            if hi-lo+1>10:
                return {'jsonrpc':'2.0','id':p['id'],'error':{'code':-32600,'message':MESSAGE}}
            if fail is not None and lo>=fail: raise RpcError('unavailable')
            return {'jsonrpc':'2.0','id':p['id'],'result':[{'blockNumber':hex(n)} for n in range(lo,hi+1)]}
        r=RpcClient(transport=transport,max_retries=0,sleeper=lambda _:None)
        r.batch_enabled=batch
        return r,seen

    def test_exact_interval_and_filters_with_reordered_batches(self):
        for batch in (False,True):
            r,seen=self.client(batch=batch)
            rows=r.call('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x15e','address':'pool','topics':['swap']}])
            self.assertEqual([int(x['blockNumber'],16) for x in rows],list(range(100,351)))
            self.assertTrue(all(hi-lo<10 and a=='pool' and t==['swap'] for lo,hi,a,t in seen[1:]))
            before=len(seen)
            r.call('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x77'}])
            self.assertEqual(len(seen)-before,2)

    def test_no_partial_results(self):
        r,_=self.client(fail=110)
        with self.assertRaises(RpcError):r.call('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x77'}])

    def test_unrelated_provider_error_never_falls_back(self):
        r=RpcClient(transport=lambda p:{'jsonrpc':'2.0','id':p['id'],'error':{'code':-32600,'message':'Not permitted'}},max_retries=0)
        with self.assertRaises(RpcError):r.call('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x77'}])
        self.assertIsNone(r._log_range_limit)

    def test_http_400_error_envelope_is_classified(self):
        r=RpcClient(max_retries=0)
        error=urllib.error.HTTPError('https://secret.invalid/key',400,'bad',{},io.BytesIO(json.dumps({'jsonrpc':'2.0','id':1,'error':{'code':-32600,'message':MESSAGE}}).encode()))
        with patch('urllib.request.urlopen',side_effect=error):
            envelope=r._http({'jsonrpc':'2.0','id':1,'method':'eth_getLogs','params':[]})
        self.assertEqual(envelope['error']['code'],-32600)

    def test_out_of_interval_log_rejected(self):
        r=RpcClient(transport=lambda p:{'jsonrpc':'2.0','id':p['id'],'result':[{'blockNumber':'0xff'}]},max_retries=0)
        r._log_range_limit=10
        with self.assertRaises(RpcError):r.call('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x77'}])
