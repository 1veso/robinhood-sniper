"""Isolated model provenance, entry wiring and autonomous exit checks."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch
from sniper.execution_solana import SolanaJournal
from sniper.execution_solana_service import SolanaGuardedBackend,collect_candidate,plan_exits,validate_deployment
from sniper.execution import encode
from rug_desk.book import Book
from rug_desk.rugcheck import ORDER
from rug_desk.grok import Judge
from test_rug_desk import response
from test_solana_executor import fixture,NOW,MINT,WALLET

class SolanaServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);pool,_,p=fixture();self.pool=pool
        book=Book(self.root/'rug_desk',initialize=True);book.close()
        (self.root/'runtime').mkdir(exist_ok=True);(self.root/'runtime/xai-api-key.txt').write_text('OFFLINE-ONLY')
        self.j=SolanaJournal(self.root/'execution.sqlite3',p,create=True)
        self.mark={'mint':MINT,'pool':pool,'price':'1','liquidity':'100000','observed_at':NOW,
                   'created_at_ms':(NOW-3600)*1000,'h1':{'buys':30,'sells':10}}
        self.facts={'mint':MINT,'pool':pool,'observed_at':NOW,'failure':None,'checks':{k:True for k in ORDER}}
        self.market=SimpleNamespace(quote=lambda *a:self.mark,profiles=lambda:[MINT],native_price=lambda:{'sol_usd':'150','sol_observed_at':NOW},
                                    client=SimpleNamespace(quote=lambda *a,**k:SimpleNamespace(price_usd='1')))
        self.judge=Judge(self.root/'rug_desk',enabled=True,transport=Mock(return_value=response()),clock=lambda:NOW)
        self.judge.recurring_enabled=lambda:True
        def quote(source,target,pool,amount):
            return {'token_in':source,'token_out':target,'pool':pool,'amount_in_raw':str(amount),'quoted_output_raw':'100000000',
                'min_output_raw':'99000000','quote_at':NOW,'deadline':NOW+30}
        self.adapter=SimpleNamespace(quote_swap=quote)
        self.backend=SolanaGuardedBackend(self.adapter,self.j,self.root,market=self.market,
            rugcheck=SimpleNamespace(report=lambda mint:self.facts),judge=self.judge,clock=lambda:NOW)
    def tearDown(self):self.j.close();self.temp.cleanup()
    def test_real_model_audit_reaches_queue_separate_from_fills_and_seen_once(self):
        result=collect_candidate(self.j,self.backend)
        self.assertEqual(result['stage'],'QUEUED');self.assertEqual(result['verdict'],'BUY')
        self.assertEqual(self.j.snapshot()['fill_count'],0);self.assertEqual(len(self.j.orders()),1)
        self.assertEqual(collect_candidate(self.j,self.backend)['stage'],'NO_NEW_CANDIDATE')
        i=self.j.orders()[0]['intent'];self.assertTrue(self.backend.entry_evidence(i)['model_validated'])
        audit=next((self.root/'rug_desk/runtime/x-responses').glob('*.json'))
        import json
        value=json.loads(audit.read_text());value['request']['input'][1]['content']=encode({'mint':MINT,'pool':{'pool':WALLET}})
        audit.write_text(encode(value))
        with self.assertRaises(ValueError):self.backend.entry_evidence(i)
    def test_disabled_gate_never_calls_model_or_creates_seen_record(self):
        self.judge.recurring_enabled=lambda:False
        with self.assertRaises(ValueError):collect_candidate(self.j,self.backend)
        self.assertEqual(self.judge.transport.call_count,0);self.assertEqual(self.j.orders(),[])
    def test_missing_required_risk_blocks_before_paid_call(self):
        self.facts['checks'].pop('authority_revoked')
        result=collect_candidate(self.j,self.backend)
        self.assertEqual(result['verdict'],'SKIP');self.assertEqual(self.judge.transport.call_count,0)
        self.assertEqual(self.j.snapshot()['fill_count'],0)
    def test_binding_changed_and_stale_evidence_block(self):
        collect_candidate(self.j,self.backend);i=self.j.orders()[0]['intent']
        self.backend.clock=lambda:NOW+301
        with self.assertRaises(ValueError):self.backend.entry_evidence(i)
        self.backend.clock=lambda:NOW
        (self.root/'runtime/xai-api-key.txt').write_text('ROTATED-TEST')
        with self.assertRaises(ValueError):self.backend.entry_evidence(i)
    def test_authority_gate_does_not_unlock_and_exit_ignores_disabled_model(self):
        self.assertFalse(self.backend.authorize_sign({}))
        self.judge.recurring_enabled=lambda:False
        positions=[{'position_id':'p','token':MINT,'pool':self.pool,'remaining_qty_raw':'100','original_qty_raw':'100',
            'entry_cost_raw':'100','peak_price':'1','entry_liquidity':'100000','tp_step':0,'opened_at':NOW-1}]
        class J:
            policy=self.j.policy
            def positions(s):return positions
            def orders(s):return []
            def update_position_peak(s,*a):pass
            def enqueue(s,i,now):s.queued=i
        j=J();self.adapter.quote_swap=lambda source,target,pool,amount:{'quoted_output_raw':str(amount*3),'quote_at':NOW}
        planned,errors=plan_exits(j,self.backend,NOW)
        self.assertEqual(errors,[]);self.assertEqual(j.queued['reason'],'take_profit_1')
        self.assertEqual(len(planned),1)
    def test_no_deployment_from_paper_or_profit_flag(self):
        p=deepcopy(self.j.policy);p['performance_profitable']=True
        with self.assertRaises(ValueError):validate_deployment(p,self.root)

    def test_model_costs_remain_separate_and_unknown_bills_block_entries(self):
        from rug_desk.report import summary
        collect_candidate(self.j,self.backend)
        self.assertEqual(self.j.snapshot()['api_unknown_requests'],0)
        self.assertGreater(float(self.j.snapshot()['api_billed_usd']),0)
        book=Book(self.root/'rug_desk')
        try:self.assertEqual(summary(book,NOW)['api_usage'],[])
        finally:book.close()
        self.j.db.execute('INSERT INTO execution_model_costs VALUES(?,?,?,?)',('unknown',NOW,None,'0.50'))
        with self.assertRaisesRegex(ValueError,'spending is unresolved'):self.backend.risk_check()
        self.assertEqual(self.j.snapshot()['api_unknown_requests'],1)

    def test_disabled_or_removed_model_credential_does_not_disable_deployment_exits(self):
        from rug_desk.model_gate import binding
        p=deepcopy(self.j.policy);p.pop('policy_hash',None)
        p.update(deployment_authorized=True,acceptance_mode=False,strategy_binding=binding(self.root/'rug_desk'))
        for k in ('user_approval_reference','independent_review_reference','wallet_simulation_reference','strategy_review_reference','signer_path'):p[k]='OFFLINE-ONLY'
        p['program_pins']={'a':1,'b':2,'c':3}
        (self.root/'runtime/solana-trading-address.txt').write_text(WALLET)
        (self.root/'runtime/xai-api-key.txt').unlink()
        self.assertTrue(validate_deployment(p,self.root)['deployment_authorized'])
        self.assertFalse(Judge(self.root/'rug_desk').recurring_enabled())

    def test_berlin_day_boundary_is_preserved(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for stamp in ('2026-10-28T00:30:00','2026-07-20T00:30:00'):
            local=datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('Europe/Berlin'))
            self.assertEqual(self.j.day_start(int(local.timestamp())),int(local.replace(hour=0,minute=0).timestamp()))
