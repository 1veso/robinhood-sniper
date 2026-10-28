"""Confirmed exit management never becomes premature accounting."""
import copy
from pathlib import Path
import tempfile
import unittest
from sniper.execution import ExecutionJournal,AutomaticExecutor
from test_execution import policy,intent,exit_intent,FakeBackend

class ProvisionalBackend(FakeBackend):
    def reconcile(self,o):
        state=self.outcomes.get(o['id'],'pending')
        if state not in ('included','included_revert'):return super().reconcile(o)
        self.outcomes[o['id']]='reverted' if state=='included_revert' else 'confirmed'
        result=super().reconcile(o);self.outcomes[o['id']]=state
        result.update(outcome=result['status'],status='included')
        result['receipt'].update(finalized=False,confirmation_depth=3)
        return result

class ProvisionalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'execution.db'
        self.p=policy(exit_settlement_mode='confirmed_exit_management',provisional_exit_review_reference='isolated-test-review',
                      max_unfinalized_transactions=4,require_finalized=True,confirmations=3)
        self.j=ExecutionJournal(self.path,self.p,create=True);self.b=ProvisionalBackend(self.j);self.w=AutomaticExecutor(self.j,self.b)
    def tearDown(self):self.j.close();self.temp.cleanup()
    def included_buy(self):
        self.j.enqueue(intent(self.j),1000);self.w.tick(1000);self.w.tick(1001)
        self.b.outcomes['buy-1']='included';self.w.tick(1002)
        self.assertEqual(self.j.order('buy-1')['status'],'INCLUDED')
    def test_exit_before_finality_then_ordered_final_accounting(self):
        self.included_buy()
        self.assertEqual(self.j.snapshot()['fill_count'],0);self.assertEqual(self.j.snapshot()['cash_raw'],'1000')
        self.assertEqual(self.j.positions(),[]);self.assertEqual(self.j.management_positions()[0]['remaining_qty_raw'],'100')
        self.j.close();self.j=ExecutionJournal(self.path,self.p);self.b.journal=self.j;self.w=AutomaticExecutor(self.j,self.b)
        self.j.enqueue(exit_intent(self.j),1004)
        self.assertEqual(self.w.tick(1004)['status'],'SIGNED');self.w.tick(1005)
        self.b.outcomes['sell-1']='included';self.w.tick(1006)
        self.assertEqual(self.j.management_positions()[0]['remaining_qty_raw'],'50')
        self.assertTrue(self.j.management_positions()[0]['take_profit_done'])
        self.assertEqual(self.j.snapshot()['fill_count'],0);self.assertEqual(self.j.snapshot()['reserved_base_raw'],'5')
        self.b.outcomes.update({'buy-1':'confirmed','sell-1':'confirmed'});self.w.tick(1007)
        self.assertEqual(self.j.snapshot()['fill_count'],2);self.assertEqual(self.j.snapshot()['cash_raw'],'1001')
        self.assertEqual(self.j.positions()[0]['remaining_qty_raw'],'50');self.assertEqual(self.j.snapshot()['native_fees_wei'],'14')
        self.w.tick(1008);self.assertEqual(self.j.snapshot()['fill_count'],2)
    def test_extra_entry_and_double_sale_rejected(self):
        self.included_buy()
        with self.assertRaises(ValueError):self.j.enqueue(intent(self.j,name='buy-2',now=1004,token='0x'+'8'*40),1004)
        self.j.enqueue(exit_intent(self.j,quantity=100),1004);self.w.tick(1004);self.w.tick(1005)
        self.b.outcomes['sell-1']='included';self.w.tick(1006)
        self.assertEqual(self.j.management_positions(),[])
        with self.assertRaises(ValueError):self.j.enqueue(exit_intent(self.j,name='sell-2',now=1007,quantity=1),1007)
    def test_lost_inclusion_halts_with_reservation_and_no_fake_fill(self):
        self.included_buy();self.b.outcomes['buy-1']='pending'
        self.assertEqual(self.w.tick(1003)['status'],'REVIEW')
        self.assertTrue(self.j.snapshot()['full_halt']);self.assertEqual(self.j.snapshot()['reserved_base_raw'],'5')
        self.assertEqual(self.j.snapshot()['fill_count'],0)
    def test_provisional_receipt_cannot_change_at_finality(self):
        self.included_buy();self.b.outcomes['buy-1']='confirmed'
        original=self.b.reconcile
        def changed(o):
            r=original(o);r['receipt']['block_hash']='0x'+'b'*64;return r
        self.b.reconcile=changed
        self.assertEqual(self.w.tick(1003)['status'],'REVIEW');self.assertEqual(self.j.snapshot()['fill_count'],0)
    def test_approval_inclusion_cannot_reset_signed_parent(self):
        self.b.needs_approval=True;self.j.enqueue(intent(self.j),1000)
        self.w.tick(1000);self.w.tick(1001);self.w.tick(1002)
        self.b.outcomes['buy-1:approval']='included';self.w.tick(1003)
        self.assertEqual(self.j.order('buy-1')['status'],'SIGNED')
        self.w.tick(1004);self.assertEqual(self.j.order('buy-1')['status'],'SUBMITTED')
        self.assertEqual(len([s for s in self.b.sign_calls if s['order_id']=='buy-1']),1)
    def test_depth_and_review_mandatory(self):
        bad=copy.deepcopy(self.p);bad.pop('provisional_exit_review_reference')
        with self.assertRaises(ValueError):ExecutionJournal(Path(self.temp.name)/'bad.db',bad,create=True)
        self.j.enqueue(intent(self.j),1000);self.w.tick(1000);self.w.tick(1001)
        self.b.outcomes['buy-1']='included';result=self.b.reconcile(self.j.order('buy-1'));result['receipt']['confirmation_depth']=2
        with self.assertRaises(ValueError):self.j.record_inclusion(self.j.order('buy-1'),result,1002,lambda:None)
        self.assertEqual(self.j.management_positions(),[])
