import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sniper.execution_service import OpportunityInbox,shadow_cycle,exit_decision,plan_exits,GuardedBackend,validate_deployment
from sniper.execution_acceptance import run_acceptance
from sniper.models import Candidate,SafetyEvidence,MarketQuote
from sniper.execution import ExecutionJournal
from decimal import Decimal as D

BASE='0x'+'2'*40;TOKEN='0x'+'3'*40;POOL='0x'+'4'*40


class ExecutionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def source(self):
        folder=self.root/'runtime/exploration';folder.mkdir(parents=True)
        (folder/'trial.json').write_text(json.dumps({'account_id':'paper-a','source_id':'source-a'}))
        c=sqlite3.connect(folder/'ledger.sqlite3')
        c.executescript("CREATE TABLE metadata(key TEXT,value TEXT); INSERT INTO metadata VALUES('account_id','paper-a');CREATE TABLE decisions(payload TEXT);CREATE TABLE fills(payload TEXT);")
        for n,verdict in enumerate(('PAPER_BUY','SKIP')):
            candidate=Candidate(str(n),'eip155:4663/erc20:'+TOKEN,'X',1000,[],SafetyEvidence('eip155:4663/erc20:'+TOKEN,1000,{},'fixture'),metadata={'source_id':'source-a'})
            quote=MarketQuote(candidate.asset_id,POOL,1000,D(1),D(30000))
            c.execute('INSERT INTO decisions VALUES(?)',(json.dumps({'candidate':candidate.as_dict(),'quote':quote.as_dict(),'result':{'verdict':verdict}}),))
        c.execute("INSERT INTO fills VALUES('must never be parsed or mirrored')");c.commit();c.close()
        return folder

    def test_input_cursor_persists_and_imports_inputs_for_all_verdicts_not_fills(self):
        self.source();path=self.root/'inbox.sqlite3'
        with closing_inbox(path) as inbox:
            first=inbox.import_paper_inputs(self.root,limit=1)
            self.assertEqual(first['remaining'],1);self.assertEqual(inbox.summary()['observed_candidates'],1)
        with closing_inbox(path) as inbox:
            second=inbox.import_paper_inputs(self.root,limit=1)
            self.assertEqual(second['checkpoint'],2);self.assertEqual(inbox.summary()['observed_candidates'],2)
            self.assertEqual(inbox.import_paper_inputs(self.root)['rows_read'],0)

    def test_replaced_source_is_rejected(self):
        folder=self.source()
        with closing_inbox(self.root/'inbox.sqlite3') as inbox:
            inbox.import_paper_inputs(self.root)
            c=sqlite3.connect(folder/'ledger.sqlite3');c.execute("UPDATE metadata SET value='different'");c.commit();c.close()
            with self.assertRaises(ValueError):inbox.import_paper_inputs(self.root)

    def test_shadow_never_loads_keys_connects_or_creates_execution_fills(self):
        self.source()
        with patch('sniper.execution_service.live_backend',side_effect=AssertionError('must not construct live backend')):
            result=shadow_cycle(self.root,1000)
        self.assertEqual(result['mode'],'SHADOW');self.assertEqual(result['signing_calls'],0);self.assertEqual(result['broadcast_calls'],0)
        self.assertFalse((self.root/'runtime/executor/live/ledger.sqlite3').exists())
        self.assertEqual(result['observed_candidates'],2)

    def position(self,**changes):
        return {'position_id':'p','token':TOKEN,'pool':POOL,'original_qty_raw':'100','remaining_qty_raw':'100',
            'entry_cost_raw':'100','peak_price':'1','take_profit_done':False,'opened_at':1000,**changes}

    def test_exit_rules_use_reconciled_quantities_and_only_one_take_profit(self):
        p=self.position()
        self.assertEqual(exit_decision(p,{'quoted_output_raw':'65','quote_at':1001},1001)['reason'],'stop_loss')
        tp=exit_decision(p,{'quoted_output_raw':'300','quote_at':1001},1001)
        self.assertEqual((tp['reason'],tp['amount_in_raw']),('take_profit','50'))
        self.assertFalse(p['take_profit_done'])
        p=self.position(remaining_qty_raw='50',take_profit_done=True,peak_price='3')
        self.assertEqual(exit_decision(p,{'quoted_output_raw':'100','quote_at':1001},1001)['reason'],'trailing_stop')
        self.assertEqual(exit_decision(self.position(),{'quoted_output_raw':'110','quote_at':22600},22600)['reason'],'time_stop')
        self.assertIsNone(exit_decision(p,{'quoted_output_raw':'100','quote_at':900},1001))

    def test_one_missing_route_does_not_prevent_other_exit(self):
        positions=[self.position(position_id='bad'),self.position(position_id='good',token='0x'+'7'*40)]
        class J:
            policy={'base_token':BASE,'account_id':'a','chain_id':4663,'wallet':'0x'+'1'*40,'policy_hash':'p'}
            def positions(self):return positions
            def orders(self):return []
            def update_position_peak(self,*args):pass
            def enqueue(self,i,now):self.queued=i
        class A:
            def quote_swap(self,token,base,pool,amount):
                if token==TOKEN:raise TimeoutError()
                return {'token_in':token,'token_out':base,'pool':pool,'amount_in_raw':str(amount),'quoted_output_raw':'50','quote_at':1001}
        j=J();backend=SimpleNamespace(adapter=A(),clock=lambda:1001)
        planned,errors=plan_exits(j,backend,1001)
        self.assertEqual(len(planned),1);self.assertEqual(errors[0]['position_id'],'bad');self.assertEqual(j.queued['position_id'],'good')

    def test_acceptance_recovers_timeout_and_does_not_touch_strategy_accounts(self):
        result=run_acceptance(self.root/'acceptance')
        self.assertTrue(result['passed']);self.assertEqual(result['real_broadcast_calls'],0)
        self.assertEqual(result['snapshot']['fill_count'],3);self.assertEqual(result['snapshot']['native_fees_wei'],'9')
        self.assertTrue(result['excluded_from_strategy_performance'])

    def test_live_deployment_cannot_be_inferred_from_profitability(self):
        with self.assertRaises(ValueError):validate_deployment({'performance_ready_for_human_review':True})

    def test_native_cost_ceiling_is_inside_position_risk_not_added_on_top(self):
        p={'account_id':'risk-test','chain_id':4663,'wallet':'0x'+'1'*40,'base_token':BASE,'capital_raw':1000000000,
            'max_order_raw':5000000,'max_total_exposure_raw':20000000,'daily_loss_raw':20000000,
            'max_gas_wei_day':10**18,'max_tx_gas_wei':25*10**13,'base_decimals':6}
        j=ExecutionJournal(self.root/'risk.sqlite3',p,create=True)
        try:
            market=SimpleNamespace(quote=lambda token,now:SimpleNamespace(price_usd=D(1) if token==BASE else D(1000)))
            backend=GuardedBackend(SimpleNamespace(),self.root/'unused',j.policy,market,clock=lambda:1000,journal=j)
            # Nine bounded fees cost $2.25; a $5 ticket would exceed the total $5 full-loss budget.
            with self.assertRaises(ValueError):backend.check_equity_risk({'id':'i','amount_in_raw':'5000000'})
            self.assertEqual(backend.entry_size('i'),2750000)
        finally:j.close()

    def test_daily_equity_halt_includes_open_losses_and_stays_latched(self):
        p={'account_id':'risk-test','chain_id':4663,'wallet':'0x'+'1'*40,'base_token':BASE,'capital_raw':1000,
            'max_order_raw':5,'max_total_exposure_raw':20,'daily_loss_raw':20,'max_gas_wei_day':100,'max_tx_gas_wei':10,'base_decimals':0}
        j=ExecutionJournal(self.root/'risk.sqlite3',p,create=True)
        market=SimpleNamespace(quote=lambda token,now:SimpleNamespace(price_usd=D(1)))
        backend=GuardedBackend(SimpleNamespace(),self.root/'unused',j.policy,market,clock=lambda:1000,journal=j)
        try:
            backend.check_equity_risk({'id':'i','amount_in_raw':'1'})
            with j.transaction():j._set('cash_raw',970)
            with self.assertRaises(ValueError):backend.check_equity_risk({'id':'i','amount_in_raw':'1'})
            with j.transaction():j._set('cash_raw',1000)
            with self.assertRaises(ValueError):backend.check_equity_risk({'id':'i','amount_in_raw':'1'})
        finally:j.close()


class closing_inbox:
    def __init__(self,path):self.inbox=OpportunityInbox(path)
    def __enter__(self):return self.inbox
    def __exit__(self,*args):self.inbox.close()
