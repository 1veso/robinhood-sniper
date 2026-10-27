"""Isolated execution lifecycle tests; no keys, RPC calls or paper accounts."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper.execution import ExecutionJournal, AutomaticExecutor


WALLET = '0x'+'1'*40
BASE = '0x'+'2'*40
TOKEN = '0x'+'3'*40
POOL = '0x'+'4'*40


def policy(**changes):
    return {'account_id':'acceptance-account', 'chain_id':4663, 'wallet':WALLET,
        'base_token':BASE, 'capital_raw':1000, 'max_order_raw':5,
        'max_total_exposure_raw':20, 'daily_loss_raw':20,
        'max_gas_wei_day':100, 'max_tx_gas_wei':10, 'max_open_positions':4,
        'max_entries_per_day':6, 'deployment_authorized':False, 'acceptance_mode':True,
        'max_quote_age_seconds':30, 'rebroadcast_seconds':2, **changes}


def intent(journal, name='buy-1', now=1000, token=TOKEN):
    p = journal.policy
    return {'id':name, 'account_id':p['account_id'], 'policy_hash':p['policy_hash'],
        'chain_id':4663, 'wallet':WALLET, 'kind':'swap', 'side':'buy',
        'token_in':BASE, 'token_out':token, 'pool':POOL,
        'amount_in_raw':'5', 'min_output_raw':'90', 'quoted_output_raw':'100',
        'quote_at':now, 'deadline':now+30,
        'entry_evidence':{'safety_passed':True, 'required_provider_response':True,
            'safety_at':now, 'liquidity_usd':'30000',
            'buyers':[{'wallet':'0x'+str(n)*40,'tx_hash':'0x'+str(n)*64,
                       'pool':POOL,'token':token,'observed_at':now} for n in (5,6)]}}


def exit_intent(journal, name='sell-1', now=1004, quantity=50, output=6, reason='take_profit'):
    i = intent(journal,name,now)
    i.update(side='sell', token_in=TOKEN, token_out=BASE, amount_in_raw=str(quantity),
             min_output_raw=str(output), quoted_output_raw=str(output),
             position_id='buy-1',reason=reason)
    i.pop('entry_evidence')
    return i


class FakeBackend:
    is_test = True

    def __init__(self, journal):
        self.journal=journal
        self.sign_calls=[]; self.broadcast_calls=[]; self.outcomes={}
        self.nonce=0; self.raise_broadcast=False; self.deny_sign=False
        self.crash_sign=False; self.needs_approval=False

    def prepare(self, i):
        if self.needs_approval and i['kind']=='swap':
            a=copy.deepcopy(i)
            a.update(id=i['id']+':approval',kind='approval',parent_intent_id=i['id'],approval_amount_raw=i['amount_in_raw'])
            return {'requires_approval':True,'approval_intent':a}
        return {'order_id':i['id'],'policy_hash':i['policy_hash'],'nonce':self.nonce,
                'expires_at':i['deadline'],'max_fee_wei':'10','intent':copy.deepcopy(i),
                'tx':{'nonce':self.nonce,'value':0}}

    def sign(self, p):
        if not self.journal.signing_authorized(p):
            raise PermissionError('Durable authority missing')
        self.sign_calls.append(copy.deepcopy(p))
        if self.deny_sign:raise PermissionError('Denied')
        if self.crash_sign:raise KeyboardInterrupt('Crash at signing boundary')
        return {'nonce':p['nonce'],'raw_transaction':'0x'+format(p['nonce']+1,'064x'),
                'tx_hash':'0x'+format(p['nonce']+1,'064x'),'prepared':copy.deepcopy(p)}

    def broadcast(self, s):
        if not self.journal.broadcast_authorized(s):
            raise PermissionError('Broadcast authority missing')
        self.broadcast_calls.append(copy.deepcopy(s))
        if self.raise_broadcast:raise TimeoutError('Provider accepted then disconnected')
        return s['tx_hash']

    def reconcile(self, o):
        state=self.outcomes.get(o['id'],'pending')
        if state not in ('confirmed','reverted'):
            return {'status':state}
        self.nonce=max(self.nonce,o['nonce']+1)
        approval=o['intent']['kind']!='swap'
        if approval and state=='confirmed':self.needs_approval=False
        i=o['intent']
        return {'status':state,'receipt':{'tx_hash':o['tx_hash'],'block_hash':'0x'+'a'*64,
            'block_number':100,'observed_at':1003,'canonical':True,'finalized':True},
            'fill':{'input_raw':i['amount_in_raw'] if state=='confirmed' and not approval else '0',
                    'output_raw':i['quoted_output_raw'] if state=='confirmed' and not approval else '0',
                    'fee_wei':'7','wallet':WALLET,'token_in':i['token_in'],'token_out':i['token_out']}}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'independent-execution.sqlite3'
        self.journal=ExecutionJournal(self.path,policy(),create=True)
        self.backend=FakeBackend(self.journal)
        self.executor=AutomaticExecutor(self.journal,self.backend)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def buy(self):
        self.journal.enqueue(intent(self.journal),1000)
        self.assertEqual(self.executor.tick(1000)['status'],'SIGNED')
        self.assertEqual(self.journal.snapshot()['fill_count'],0)
        self.assertEqual(self.executor.tick(1001)['status'],'SUBMITTED')
        self.backend.outcomes['buy-1']='confirmed'
        self.assertEqual(self.executor.tick(1003)['status'],'CONFIRMED')

    def test_default_disabled_and_test_backend_cannot_use_live_authority(self):
        self.journal.close()
        self.path.unlink()
        self.journal=ExecutionJournal(self.path,policy(acceptance_mode=False),create=True)
        b=FakeBackend(self.journal)
        self.assertEqual(AutomaticExecutor(self.journal,b).tick(1000)['status'],'DISABLED')
        self.assertFalse(b.sign_calls)
        self.journal.close();self.path.unlink()
        self.journal=ExecutionJournal(self.path,policy(acceptance_mode=False,deployment_authorized=True),create=True)
        self.assertEqual(AutomaticExecutor(self.journal,FakeBackend(self.journal)).tick(1000)['status'],'DISABLED')

    def test_policy_binding_survives_restart_and_no_missing_reset(self):
        self.journal.close()
        with self.assertRaises(ValueError):ExecutionJournal(self.path,policy(capital_raw=2000))
        self.journal=ExecutionJournal(self.path,policy())
        self.assertEqual(self.journal.snapshot()['cash_raw'],'1000')
        with self.assertRaises(ValueError):ExecutionJournal(Path(self.temp.name)/'missing.db',policy())

    def test_pending_quote_and_submission_never_create_fills(self):
        i=intent(self.journal);self.journal.enqueue(i,1000)
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.executor.tick(1000);self.executor.tick(1001)
        self.assertEqual(self.journal.snapshot()['cash_raw'],'1000')
        self.assertEqual(self.journal.positions(),[])
        self.assertEqual(self.journal.snapshot()['fill_count'],0)
        self.assertEqual(self.journal.notices(),[])

    def test_timeout_after_acceptance_restart_retains_hash_and_no_resign(self):
        self.journal.enqueue(intent(self.journal),1000)
        self.executor.tick(1000);self.backend.raise_broadcast=True
        self.assertEqual(self.executor.tick(1001)['status'],'UNKNOWN')
        saved=self.journal.order('buy-1')['signed']
        self.journal.close();self.journal=ExecutionJournal(self.path,policy())
        self.backend.journal=self.journal;self.executor=AutomaticExecutor(self.journal,self.backend)
        self.assertEqual(self.executor.tick(1004)['status'],'UNKNOWN')
        self.assertEqual(self.backend.broadcast_calls,[saved,saved])
        self.assertEqual(len(self.backend.sign_calls),1)
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.backend.outcomes['buy-1']='confirmed'
        self.assertEqual(self.executor.tick(1005)['status'],'CONFIRMED')
        self.assertEqual(self.journal.snapshot()['cash_raw'],'995')

    def test_expired_unknown_keeps_exposure_and_never_gets_replacement_nonce(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.executor.tick(1001)
        self.assertEqual(self.executor.tick(1050)['status'],'UNKNOWN')
        self.assertEqual(len(self.backend.broadcast_calls),1)
        self.assertEqual(len(self.backend.sign_calls),1)
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        with self.assertRaises(ValueError):self.journal.enqueue(intent(self.journal,'other',1050),1050)

    def test_confirmed_entry_partial_and_close_exact_basis_and_fees(self):
        self.buy()
        self.assertEqual(self.journal.positions()[0]['remaining_qty_raw'],'100')
        self.journal.enqueue(exit_intent(self.journal),1004)
        self.executor.tick(1004);self.backend.outcomes['sell-1']='confirmed';self.executor.tick(1005)
        p=self.journal.positions()[0]
        self.assertEqual(p['remaining_qty_raw'],'50')
        self.assertEqual(p['cost_basis_raw'],'3')
        self.assertTrue(p['take_profit_done'])
        self.journal.enqueue(exit_intent(self.journal,'close',1006,quantity=50,output=4,reason='trailing_stop'),1006)
        self.executor.tick(1006);self.backend.outcomes['close']='confirmed';self.executor.tick(1007)
        s=self.journal.snapshot()
        self.assertEqual(s['positions'],[])
        self.assertEqual(s['cash_raw'],'1005')
        self.assertEqual(s['realized_base_before_native_fees_raw'],'5')
        self.assertEqual(s['native_fees_wei'],'21')
        self.assertFalse(s['pnl_complete'])
        self.assertEqual(s['fill_count'],3)
        self.executor.tick(1010)
        self.assertEqual(self.journal.snapshot()['native_fees_wei'],'21')

    def test_reverted_receipt_books_fee_once_without_holdings(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.backend.outcomes['buy-1']='reverted';self.executor.tick(1001)
        s=self.journal.snapshot()
        self.assertEqual(s['native_fees_wei'],'7')
        self.assertEqual(s['cash_raw'],'1000')
        self.assertEqual(s['positions'],[])
        self.assertEqual(s['reserved_base_raw'],'0')
        self.executor.tick(1002);self.assertEqual(self.journal.snapshot()['native_fees_wei'],'7')

    def test_exit_runs_before_entry_and_entry_halt_does_not_strand_holdings(self):
        self.buy()
        self.journal.enqueue(intent(self.journal,'second',1004,token='0x'+'7'*40),1004)
        self.journal.enqueue(exit_intent(self.journal),1004)
        self.journal.set_halt('entries','test',1004)
        result=self.executor.tick(1004)
        self.assertEqual(result['order_id'],'sell-1')
        self.backend.outcomes['sell-1']='confirmed';self.executor.tick(1005)
        self.assertEqual(self.executor.tick(1006)['status'],'IDLE')
        self.assertEqual(self.journal.order('second')['status'],'QUEUED')

    def test_all_halt_reconciles_existing_transaction_without_new_broadcast(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.journal.set_halt('all','test',1000)
        self.executor.tick(1001)
        self.assertEqual(self.backend.broadcast_calls,[])
        self.backend.outcomes['buy-1']='confirmed';self.executor.tick(1002)
        self.assertEqual(self.journal.snapshot()['fill_count'],1)

    def test_entry_halt_between_sign_and_first_broadcast_keeps_buy_unsubmitted(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.journal.set_halt('entries','concurrent halt',1001)
        self.executor.tick(1001)
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.backend.outcomes['buy-1']='confirmed'
        self.executor.tick(1002)
        self.assertEqual(self.journal.snapshot()['fill_count'],1)

    def test_approval_callback_rechecks_halt_after_key_unlock(self):
        self.backend.needs_approval=True
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        def unlock_then_halt(p):
            self.assertTrue(self.journal.signing_authorized(p))
            self.journal.set_halt('entries','halt during key unlock',1001)
            self.assertFalse(self.journal.signing_authorized(p))
            raise PermissionError('Signing authority revoked after key unlock')
        self.backend.sign=unlock_then_halt
        self.assertEqual(self.executor.tick(1001)['status'],'REJECTED')
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertIsNone(self.journal.order('buy-1:approval')['signed'])

    def test_signing_grant_expires_during_key_unlock_without_takeover(self):
        self.journal.enqueue(intent(self.journal),1000)
        def unlock_too_slow(p):
            self.assertTrue(self.journal.signing_authorized(p))
            key=(p['order_id'],p['worker_fence'],p['lease_epoch'])
            deadline=self.journal._signing_deadlines[key]
            with patch('sniper.execution.time.monotonic',return_value=deadline+1):
                self.assertFalse(self.journal.signing_authorized(p))
            raise PermissionError('Signing lease expired')
        self.backend.sign=unlock_too_slow
        self.assertEqual(self.executor.tick(1000)['status'],'REJECTED')
        self.assertEqual(self.backend.broadcast_calls,[])

    def test_broadcast_callback_rechecks_halt_and_exact_bytes_after_rpc_work(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        signed=self.journal.order('buy-1')['signed']
        self.assertFalse(self.journal.broadcast_authorized(signed))
        def rpc_then_halt(s):
            self.assertTrue(self.journal.broadcast_authorized(s))
            changed=copy.deepcopy(s);changed['raw_transaction']='0x00'
            self.assertFalse(self.journal.broadcast_authorized(changed))
            self.journal.set_halt('entries','halt during RPC validation',1001)
            self.assertFalse(self.journal.broadcast_authorized(s))
            raise PermissionError('Send authority revoked')
        self.backend.broadcast=rpc_then_halt
        self.assertEqual(self.executor.tick(1001)['status'],'UNKNOWN')
        self.assertFalse(self.journal.broadcast_authorized(signed))
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')

    def test_signer_denial_has_no_broadcast_or_fill(self):
        self.journal.enqueue(intent(self.journal),1000);self.backend.deny_sign=True
        self.assertEqual(self.executor.tick(1000)['status'],'REJECTED')
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'0')

    def test_crash_during_signing_requires_recovery_and_retains_reservation(self):
        self.journal.enqueue(intent(self.journal),1000);self.backend.crash_sign=True
        with self.assertRaises(KeyboardInterrupt):self.executor.tick(1000)
        self.backend.crash_sign=False
        self.assertEqual(self.executor.tick(1001)['status'],'REVIEW')
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.journal.set_halt(None,now=1001)
        self.assertEqual(self.executor.tick(1002)['status'],'REVIEW')
        self.assertEqual(len(self.backend.sign_calls),1)

    def test_reorg_never_creates_fill_or_releases_uncertain_exposure(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.backend.outcomes['buy-1']='reorged'
        self.assertEqual(self.executor.tick(1001)['status'],'REVIEW')
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.assertEqual(self.journal.snapshot()['fill_count'],0)
        self.assertTrue(self.journal.snapshot()['full_halt'])

    def test_approval_prerequisite_gas_only_then_fresh_parent(self):
        self.backend.needs_approval=True
        self.journal.enqueue(intent(self.journal),1000)
        self.assertEqual(self.executor.tick(1000)['status'],'WAITING_APPROVAL')
        self.assertEqual(self.executor.tick(1001)['order_id'],'buy-1:approval')
        self.backend.outcomes['buy-1:approval']='confirmed';self.executor.tick(1002)
        self.assertEqual(self.journal.snapshot()['cash_raw'],'1000')
        self.assertEqual(self.journal.snapshot()['native_fees_wei'],'7')
        self.assertEqual(self.journal.snapshot()['fill_count'],0)
        self.assertEqual(self.executor.tick(1003)['status'],'SIGNED')
        self.assertEqual(self.journal.order('buy-1')['nonce'],1)

    def test_parent_quote_refresh_after_confirmation_preserves_allocation(self):
        self.backend.needs_approval=True
        self.journal.enqueue(intent(self.journal),1000)
        self.executor.tick(1000);self.executor.tick(1001)
        self.backend.outcomes['buy-1:approval']='confirmed';self.executor.tick(1100)
        def refresh(i):
            i['quote_at']=1101;i['deadline']=1131;i['entry_evidence']['safety_at']=1101
            for b in i['entry_evidence']['buyers']:b['observed_at']=1101
            i['quoted_output_raw']='101';i['min_output_raw']='91'
            return i
        self.backend.refresh_intent=refresh
        self.assertEqual(self.executor.tick(1101)['status'],'SIGNED')
        self.assertEqual(self.journal.order('buy-1')['intent']['quoted_output_raw'],'101')
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM execution_audit WHERE kind='quote_revision'").fetchone()[0],1)

    def test_refresh_cannot_change_trade_amount_or_signed_identity(self):
        self.journal.enqueue(intent(self.journal),1000)
        def refresh(i):i['amount_in_raw']='6';return i
        self.backend.refresh_intent=refresh
        self.assertEqual(self.executor.tick(1000)['status'],'PENDING')
        self.assertEqual(self.backend.sign_calls,[])
        self.assertEqual(self.journal.order('buy-1')['intent']['amount_in_raw'],'5')

    def test_entry_evidence_duplicate_quorum_stale_and_unknown_safety_block(self):
        for alter in (lambda i:i['entry_evidence'].update(safety_passed=False),
                      lambda i:i['entry_evidence'].update(liquidity_usd='NaN'),
                      lambda i:i['entry_evidence']['buyers'][1].update(tx_hash=i['entry_evidence']['buyers'][0]['tx_hash']),
                      lambda i:i.update(quote_at=900)):
            i=intent(self.journal);alter(i)
            with self.assertRaises(ValueError):self.journal.enqueue(i,1000)
        self.assertEqual(self.journal.orders(),[])

    def test_risk_gas_and_concurrent_worker_limits(self):
        i=intent(self.journal);i['amount_in_raw']='6'
        with self.assertRaises(ValueError):self.journal.enqueue(i,1000)
        self.journal.enqueue(intent(self.journal),1000)
        second=AutomaticExecutor(self.journal,self.backend)
        self.assertTrue(self.executor._lease(1000))
        self.assertEqual(second.tick(1000)['status'],'BUSY')
        self.executor._release()
        prepare=self.backend.prepare
        def expensive(i):p=prepare(i);p['max_fee_wei']='11';return p
        self.backend.prepare=expensive
        self.assertEqual(self.executor.tick(1000)['status'],'REJECTED')
        self.assertEqual(self.backend.sign_calls,[])

    def test_conflicting_intent_and_receipt_replay_rejected(self):
        i=intent(self.journal);self.journal.enqueue(i,1000)
        self.assertEqual(self.journal.enqueue(i,2000)['id'],'buy-1')
        changed=copy.deepcopy(i);changed['amount_in_raw']='4'
        with self.assertRaises(ValueError):self.journal.enqueue(changed,1000)
        self.executor.tick(1000);self.backend.outcomes['buy-1']='confirmed';self.executor.tick(1003)
        order=self.journal.order('buy-1');result=copy.deepcopy(order['receipt'])
        self.journal._finalize(order,result,1004)
        result['fill']['fee_wei']='6'
        with self.assertRaises(ValueError):self.journal._finalize(order,result,1005)
        self.assertEqual(self.journal.snapshot()['native_fees_wei'],'7')

    def test_provider_unknown_is_not_permission_to_rebroadcast(self):
        self.journal.enqueue(intent(self.journal),1000);self.executor.tick(1000)
        self.backend.outcomes['buy-1']='unknown'
        self.assertEqual(self.executor.tick(1001)['status'],'UNKNOWN')
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')

    def test_expired_worker_cannot_sign_or_commit_after_takeover(self):
        self.journal.enqueue(intent(self.journal),1000)
        prepare=self.backend.prepare
        def taken_over(i):
            p=prepare(i)
            with self.journal.transaction():
                self.journal._set('worker_owner','new-process')
                self.journal._set('worker_lease_epoch',999)
                self.journal._set('worker_lease_until',2000)
            return p
        self.backend.prepare=taken_over
        self.assertEqual(self.executor.tick(1000)['status'],'LEASE_LOST')
        self.assertEqual(self.backend.sign_calls,[])
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertIsNone(self.journal.order('buy-1')['signed'])
        self.assertEqual(self.journal.snapshot()['reserved_base_raw'],'5')

    def test_signing_fence_lost_preserves_recovery_boundary(self):
        self.journal.enqueue(intent(self.journal),1000)
        sign=self.backend.sign
        def taken_over(p):
            s=sign(p)
            with self.journal.transaction():
                self.journal._set('worker_owner','new-process')
                self.journal._set('worker_lease_epoch',999)
                self.journal._set('worker_lease_until',1100)
            return s
        self.backend.sign=taken_over
        self.assertEqual(self.executor.tick(1000)['status'],'LEASE_LOST')
        self.assertEqual(self.journal.order('buy-1')['status'],'SIGNING')
        self.assertEqual(self.backend.broadcast_calls,[])
        self.assertEqual(self.executor.tick(1101)['status'],'REVIEW')
        self.assertEqual(len(self.backend.sign_calls),1)

    def test_cannot_initialize_inside_paper_database_or_reset_missing_identity(self):
        other=Path(self.temp.name)/'paper.sqlite3'
        with sqlite3.connect(other) as connection:
            connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            connection.execute("INSERT INTO metadata VALUES('cash','1000')")
        connection.close()
        with self.assertRaises(ValueError):ExecutionJournal(other,policy(),create=True)
        with sqlite3.connect(other) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('metadata',)])
        connection.close()
        with self.journal.transaction():self.journal.db.execute("DELETE FROM execution_metadata WHERE key='policy_hash'")
        self.journal.close()
        with self.assertRaises(ValueError):ExecutionJournal(self.path,policy(),create=True)
        # Restore the fixture only so tearDown closes a valid test journal.
        with sqlite3.connect(self.path) as connection:
            from sniper.execution import validate_policy
            connection.execute('INSERT INTO execution_metadata VALUES(?,?)',('policy_hash',validate_policy(policy())['policy_hash']))
        connection.close()
        self.journal=ExecutionJournal(self.path,policy())


if __name__ == '__main__':
    unittest.main()
