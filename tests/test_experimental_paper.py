from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from sniper.experimental_paper import Account,CATALOG,evaluate,snapshot
from sniper.experimental_feed import Reader
from sniper.krypton.demo import address

NOW=1793553000

def evidence(now=NOW,n=1):
 return {'source_mode':'fixture','fee_bps':'30','quote':{'mint':address(n),'pool':address(40+n),'symbol':'FIXTURE',
  'price':'1','liquidity':'200000','sol_usd':'150','observed_at':now,'sol_observed_at':now,'created_at_ms':(now-3600)*1000},
  'chain':{'genesis_verified':True,'commitment':'finalized','slot':100,'block_time':now-10,'mint':address(n),'pool':address(40+n),
   'mint_disabled':True,'freeze_disabled':True,'pool_structure_verified':True},
  'activity':{'buys_h1':120,'sells_h1':100,'buys_m5':12,'sells_m5':10,'volume_h1':'100000','change_h1':'5','change_m5':'1',
   'observed_owner_share':'.02','observed_top10_share':'.20'}}

class ExperimentalPaperTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def account(self,bot='xenon',fixture=True):
  a=Account(self.root,bot,initialize=True,reference='Explicit test enrollment',now=NOW,fixture=fixture);self.addCleanup(a.close);return a
 def test_eight_distinct_accounts_complete_costed_lifecycle(self):
  identities=[]
  for bot in CATALOG:
   a=self.account(bot);identities.append(a.identity);start=D(a.get('cash'))
   self.assertTrue(a.observe(evidence(),NOW));self.assertFalse(a.observe(evidence(),NOW));p=a.positions()[0]
   self.assertEqual(D(a.get('cash'))+D(p['basis'])+D(a.get('rent_reserved')),start)
   self.assertGreater(D(p['entry_price']),D(1));self.assertEqual(a.db.execute('SELECT count(*) FROM fills').fetchone()[0],1)
   e=evidence(NOW+30);e['quote']['price']='1.6'
   self.assertTrue(a.watch(e,NOW+30));self.assertEqual(a.positions(),[])
   self.assertEqual(a.db.execute('SELECT count(*) FROM fills').fetchone()[0],2)
  self.assertEqual(len(set(identities)),8)
  self.assertEqual(len(snapshot(self.root,NOW+30)['accounts']),8)
 def test_original_files_unchanged_and_fixture_cannot_enter_live_experiment(self):
  original=self.root/'rug_desk/runtime/book.sqlite3';original.parent.mkdir(parents=True);original.write_bytes(b'ORIGINAL SENTINEL')
  a=self.account(fixture=False)
  with self.assertRaises(ValueError):a.observe(evidence(),NOW)
  self.assertEqual(a.positions(),[]);self.assertEqual(original.read_bytes(),b'ORIGINAL SENTINEL')
 def test_reenrollment_preserves_balance_identity_and_window(self):
  a=self.account();a.observe(evidence(),NOW);identity=a.identity;cash=a.get('cash');end=a.get('ends_at');a.close()
  b=Account(self.root,'xenon',initialize=True,reference='Repeat',now=NOW+100,fixture=True)
  self.addCleanup(b.close);self.assertEqual((b.identity,b.get('cash'),b.get('ends_at')),(identity,cash,end))
 def test_missing_stale_wrong_pool_and_known_failures_do_not_fill(self):
  a=self.account()
  for path,value in [(('quote','observed_at'),NOW-61),(('chain','pool'),address(5)),(('chain','freeze_disabled'),False),(('activity','buys_h1'),None)]:
   e=evidence();e[path[0]][path[1]]=value
   with self.assertRaises((ValueError,TypeError)):a.observe(e,NOW)
  for field,value in [('liquidity','100'),('created_at_ms',NOW*1000)]:
   e=evidence();e['quote'][field]=value;self.assertFalse(a.observe(e,NOW))
  e=evidence();e['activity']['observed_owner_share']='.3';e['activity']['observed_top10_share']='.4';self.assertFalse(a.observe(e,NOW))
  self.assertEqual(a.db.execute('SELECT count(*) FROM fills').fetchone()[0],0)
 def test_variant_rules_are_independent(self):
  e=evidence();e['activity']['change_h1']='-5';e['activity']['change_m5']='-1'
  self.assertFalse(evaluate('xenon',e,NOW)['variant_rule']);self.assertFalse(evaluate('oxygen',e,NOW)['variant_rule'])
  self.assertTrue(evaluate('argon',e,NOW)['variant_rule'])
 def test_position_bound_and_trial_expiry(self):
  a=self.account()
  for n in (1,2,3):self.assertTrue(a.observe(evidence(n=n),NOW))
  self.assertFalse(a.observe(evidence(n=4),NOW));self.assertEqual(len(a.positions()),3)
  end=a.get('ends_at');self.assertFalse(a.observe(evidence(end,n=5),end))
  self.assertTrue(a.watch(evidence(end),end))
 def test_stale_exit_retains_position_and_stop_uses_fresh_mark(self):
  a=self.account();a.observe(evidence(),NOW);cash=a.get('cash')
  with self.assertRaises(ValueError):a.watch(evidence(),NOW+121)
  self.assertEqual(a.get('cash'),cash);self.assertEqual(len(a.positions()),1)
  e=evidence(NOW+122);e['quote']['price']='.7';self.assertTrue(a.watch(e,NOW+122))
 def test_read_only_transport_has_no_order_or_simulation_methods(self):
  for name in ('sendTransaction','simulateTransaction','getBalance','signTransaction'):
   with self.assertRaises(ValueError):Reader(transport=lambda *a:None).rpc(name,[])
