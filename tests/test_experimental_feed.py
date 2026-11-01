import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sniper.experimental_feed import Feed,SourceUnavailable
from sniper.experimental_paper import Account,CATALOG,evaluate
from sniper.adapters.solana_execution import MAINNET_GENESIS,USDC,WSOL
from sniper.krypton.demo import address

NOW=1793553000

@unittest.skipUnless(importlib.util.find_spec('solders'),'PDA dependency unavailable')
class ExperimentalFeedTests(unittest.TestCase):
 def setUp(self):
  from test_pumpswap_evidence import PumpEvidenceTests
  from test_solana_executor import token
  self.fixture=PumpEvidenceTests('test_negative_virtual_reserves_fee_math_and_real_liquidity');self.fixture.setUp()
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  (self.root/'runtime').mkdir();(self.root/'runtime/holder-rpc-url.txt').write_text('https://rpc.example.invalid')
  f=self.fixture;self.holders=[address(n) for n in range(70,75)]
  for n,key in enumerate(self.holders):f.accounts[key]=token(f.mint,address(80+n),10**10)
  self.calls=[]
 def transport(self,method,url,body,headers,timeout):
  self.calls.append((method,body['method'] if body else url))
  f=self.fixture
  if method=='GET':
   if 'token-profiles' in url:return [{'chainId':'solana','tokenAddress':f.mint}]
   native=url.endswith('/'+WSOL)
   return [{'chainId':'solana','baseToken':{'address':WSOL if native else f.mint,'symbol':'FIXTURE'},'quoteToken':{'address':USDC if native else WSOL},
    'pairAddress':address(55) if native else f.pool,'liquidity':{'usd':1000000},'priceUsd':'100' if native else '.09999',
    'pairCreatedAt':(NOW-3600)*1000,'txns':{'h1':{'buys':120,'sells':100},'m5':{'buys':12,'sells':10}},
    'priceChange':{'h1':5,'m5':1},'volume':{'h1':100000}}]
  name,args=body['method'],body['params']
  if name=='getGenesisHash':value=MAINNET_GENESIS
  elif name=='getAccountInfo':value={'context':{'slot':100},'value':f.accounts[args[0]]}
  elif name=='getMultipleAccounts':value={'context':{'slot':100},'value':[f.accounts[k] for k in args[0]]}
  elif name=='getTokenLargestAccounts':value={'context':{'slot':100},'value':[{'address':k} for k in self.holders]}
  elif name=='getBlockTime':value=NOW-10
  else:raise AssertionError(name)
  return {'jsonrpc':'2.0','id':1,'result':value}
 def test_raw_chain_capture_qualifies_proxies_and_archives_public_evidence(self):
  feed=Feed(self.root,clock=lambda:NOW,transport=self.transport);e=feed.capture(self.fixture.mint)
  self.assertEqual(e['chain']['slot'],100);self.assertEqual(e['fee_bps'],'55')
  for bot in CATALOG:self.assertTrue(all(evaluate(bot,e,NOW).values()))
  self.assertTrue((self.root/'runtime/experiments/evidence'/(e['raw_capture_sha256']+'.json')).exists())
  self.assertTrue(all(method=='GET' or name in feed.reader.ALLOWED_RPC for method,name in self.calls))
 def test_wrong_chain_and_bad_pool_fail_closed(self):
  with self.assertRaises(SourceUnavailable):Feed(self.root,transport=lambda *args:{'id':1,'result':'wrong-chain'})
  self.fixture.accounts[self.fixture.pool]['owner']=address(12)
  with self.assertRaises(ValueError):Feed(self.root,clock=lambda:NOW,transport=self.transport).capture(self.fixture.mint)
 def test_worker_e2e_uses_separate_ledgers_and_fills_once(self):
  import experimental_paper as worker
  for bot in CATALOG:Account(self.root,bot,initialize=True,reference='Isolated E2E test',now=NOW).close()
  with patch.object(worker,'Feed',side_effect=lambda root:Feed(root,clock=lambda:NOW,transport=self.transport)),patch.object(worker.time,'time',return_value=NOW):
   first=worker.run(self.root);second=worker.run(self.root)
  self.assertEqual(len(first['buys']),8);self.assertEqual(second['buys'],[])
  self.assertEqual(first['errors'],[])

class SourceBackoffTests(unittest.TestCase):
 def test_source_failure_is_not_retried_in_same_or_immediate_next_run(self):
  import experimental_paper as worker
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   for bot in CATALOG:Account(root,bot,initialize=True,reference='Backoff test').close()
   with patch.object(worker,'Feed',side_effect=SourceUnavailable('TimeoutError')) as feed:
    first=worker.run(root);second=worker.run(root)
   self.assertEqual(feed.call_count,1);self.assertEqual(second['state'],'SOURCE_BACKOFF')
   self.assertEqual(first['buys'],[])
