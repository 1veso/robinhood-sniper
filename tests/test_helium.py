import copy
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.helium.config import load, digest, encode, TEAM_POLICY_HASH
from sniper.helium.paper import Paper
from sniper.helium.demo import FictionalProviders, FictionalReviewer, FictionalGrok, address
from sniper.helium.service import scan, watch
from sniper.krypton.paper import Paper as KryptonPaper

ROOT = Path(__file__).resolve().parents[1]


class HeliumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1793345000
        self.clock = lambda:self.now
        self.cfg = load(ROOT/'helium_bot')
        self.book = Paper(self.root/'helium_bot/runtime',self.cfg,initialize=True,demo=True,clock=self.clock)
        self.addCleanup(lambda:self.book.close())
        self.providers = FictionalProviders(self.clock)
        self.model = FictionalGrok(self.book,self.clock)
        self.reviewer = FictionalReviewer(self.book,self.clock)

    def run_scan(self):
        return scan(self.root,self.book,self.providers,self.model,self.reviewer,self.clock)

    def one_candidate(self,n=1):
        self.providers.profiles = lambda:[address(n)]

    def evidence(self):
        q = self.providers.costs(self.providers.quote(address(1)))
        return {'quote':q,'facts':self.providers.holders(address(1),self.cfg),
                'lp':self.providers.lp(address(1),q['pool'])}

    def test_baseline_is_unchanged(self):
        self.assertEqual(self.cfg,load(ROOT/'krypton_bot'))
        self.assertEqual((ROOT/'helium_bot/config.json').read_bytes(),(ROOT/'krypton_bot/config.json').read_bytes())

    def test_independent_four_candidate_lifecycle_and_original_take_profit(self):
        for _ in range(4):
            self.assertEqual(self.run_scan(),[])
            self.now += 60
        self.assertEqual((self.model.calls,self.reviewer.calls),(3,2))
        self.assertEqual(len(self.book.positions()),1)
        self.assertLessEqual(Decimal('1000')-Decimal(self.book.get('cash')),Decimal('20'))
        self.providers.price = '2'
        self.assertEqual(watch(self.root,self.book,self.providers,self.clock),[])
        result = self.book.snapshot(self.now)
        self.assertEqual(result['decision_counts'],{'BUY':1,'SELL':1,'SKIP':3})
        self.assertEqual(result['summary']['completed_trades'],1)
        self.assertEqual(result['bot'],'helium')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM team_tasks').fetchone()[0],4)
        buy = self.book.db.execute("SELECT payload FROM decisions WHERE action='BUY'").fetchone()[0]
        self.assertTrue(json.loads(buy)['team_task_id'])
        self.run_scan()
        self.assertEqual((self.model.calls,self.reviewer.calls),(3,2))

    def test_watch_is_terminal_no_entry_and_never_resets_candidate_age(self):
        self.one_candidate()
        self.reviewer.label = 'verify_more'
        self.assertEqual(self.run_scan(),[])
        self.now += 60
        self.run_scan()
        self.assertEqual((self.model.calls,self.reviewer.calls),(1,1))
        self.assertEqual(self.book.positions(),[])
        candidate = self.book.db.execute('SELECT * FROM candidates').fetchone()
        self.assertEqual(candidate['state'],'WATCH')
        self.assertEqual(candidate['first_seen'],self.now-60)

    def test_disabled_reviewer_prevents_wasting_a_grok_call(self):
        self.one_candidate()
        with patch.object(self.reviewer,'gate',return_value={'enabled':False,'blockers':['disabled']}):
            self.assertEqual(self.run_scan(),[])
        self.assertEqual((self.model.calls,self.reviewer.calls),(0,0))
        self.assertEqual(self.book.db.execute('SELECT action FROM decisions').fetchone()[0],'BLOCKED')

    def test_hard_failure_never_calls_either_model(self):
        self.one_candidate(2)
        self.assertEqual(self.run_scan(),[])
        self.assertEqual((self.model.calls,self.reviewer.calls),(0,0))

    def test_rehashed_forged_buy_cannot_bypass_typed_reviewer_result(self):
        self.one_candidate()
        self.reviewer.label = 'reject'
        original = self.reviewer.ask
        def forged(*args):
            return dict(original(*args),decision='BUY')
        with patch.object(self.reviewer,'ask',side_effect=forged):
            self.assertTrue(self.run_scan())
        self.assertEqual(self.book.positions(),[])
        self.assertEqual(self.book.get('cash'),'1000')

    def test_bound_review_rejects_other_account_or_stale_decision(self):
        self.one_candidate()
        original = self.reviewer.ask
        with patch.object(self.reviewer,'ask',side_effect=lambda *a:dict(original(*a),account_id='different-account')):
            self.assertTrue(self.run_scan())
        self.assertEqual(self.book.positions(),[])

    def test_stale_review_does_not_enter(self):
        self.one_candidate()
        original = self.reviewer.ask
        with patch.object(self.reviewer,'ask',side_effect=lambda *a:dict(original(*a),judged_at=self.now-61)):
            self.assertTrue(self.run_scan())
        self.assertEqual(self.book.positions(),[])

    def test_changed_inputs_after_review_do_not_enter(self):
        self.one_candidate()
        original = self.reviewer.ask
        def changed(evidence,analysis,task_id):
            result = original(evidence,analysis,task_id)
            evidence['quote']['liquidity'] = '99999'
            return result
        with patch.object(self.reviewer,'ask',side_effect=changed):
            self.assertTrue(self.run_scan())
        self.assertEqual(self.book.positions(),[])

    def test_permission_revoked_before_financial_transaction_blocks_entry(self):
        self.one_candidate()
        gate = lambda:{'enabled':self.reviewer.calls == 0,'blockers':[]}
        with patch.object(self.reviewer,'gate',side_effect=gate):
            self.assertTrue(self.run_scan())
        self.assertEqual(self.book.positions(),[])

    def test_parent_entry_method_still_dispatches_helium_final_guard(self):
        evidence = self.evidence()
        mint = address(1)
        self.book.discover([mint],self.now)
        self.book.pin(mint,evidence['quote']['pool'])
        with self.assertRaisesRegex(ValueError,'Helium team review'):
            KryptonPaper.enter(self.book,mint,evidence,self.model.ask(evidence),self.now,lambda:True)
        self.assertEqual(self.book.positions(),[])

    def test_restart_preserves_work_and_does_not_repeat_ambiguous_model_step(self):
        mint = address(1)
        self.one_candidate()
        self.book.discover([mint],self.now)
        task_id = self.book.begin_task(mint,self.now)
        self.book.record_artifact(task_id,'evidence',self.evidence(),self.now)
        self.book.handoff(task_id,'analysis','Analyst','Request started',self.now)
        identity = self.book.identity
        self.book.close()
        self.book = Paper(self.root/'helium_bot/runtime',self.cfg,demo=True,clock=self.clock)
        self.model = FictionalGrok(self.book,self.clock)
        self.reviewer = FictionalReviewer(self.book,self.clock)
        self.assertEqual(self.run_scan(),[])
        self.assertEqual(self.book.identity,identity)
        self.assertEqual((self.model.calls,self.reviewer.calls),(0,0))
        self.assertEqual(self.book.db.execute('SELECT state FROM team_tasks').fetchone()[0],'ERROR')
        self.assertEqual(self.book.artifact(task_id,'evidence'),self.evidence())

    def test_accounts_cannot_be_opened_as_each_other(self):
        with self.assertRaisesRegex(ValueError,'another bot'):
            KryptonPaper(self.book.folder,self.cfg,demo=True)
        other = KryptonPaper(self.root/'krypton_bot/runtime',self.cfg,initialize=True,demo=True)
        try:
            with self.assertRaisesRegex(ValueError,'own account'):
                Paper(other.folder,self.cfg,initialize=True,demo=True)
            self.assertNotEqual(other.identity,self.book.identity)
            self.assertIsNone(other.db.execute("SELECT name FROM sqlite_master WHERE name='team_tasks'").fetchone())
        finally:
            other.close()

    def test_models_and_team_policy_cannot_disable_original_stop_exit(self):
        self.one_candidate()
        self.assertEqual(self.run_scan(),[])
        self.book.set('team_policy_hash','changed')
        self.providers.price = '.5'
        with (patch.object(self.model,'gate',side_effect=AssertionError('Exit touched model')),
              patch.object(self.reviewer,'gate',side_effect=AssertionError('Exit touched reviewer'))):
            self.assertEqual(watch(self.root,self.book,self.providers,self.clock),[])
        self.assertEqual(self.book.positions(),[])
        payload = self.book.db.execute("SELECT payload FROM fills WHERE side='SELL'").fetchone()[0]
        self.assertEqual(json.loads(payload)['reason'],'stop_loss')

    def test_estimated_reviewer_cost_is_not_a_verified_invoice(self):
        audit = {'provider':'jev','reservation_usd':'.01',
                 'usage':{'billed_usd':None,'estimated_usd':'.000042','cost_verified':False}}
        self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',('helium:jev:test',encode(audit)))
        equity,_ = self.book.equity(self.now)
        self.assertEqual(equity,Decimal('999.999958'))
        result = self.book.snapshot(self.now)
        self.assertEqual(result['summary']['unknown_api_bills'],1)
        self.assertIsNone(result['summary']['net_pnl'])

    def test_completed_artifact_cannot_be_replaced(self):
        self.book.discover([address(1)],self.now)
        task_id = self.book.begin_task(address(1),self.now)
        evidence = self.evidence()
        self.book.record_artifact(task_id,'evidence',evidence,self.now)
        evidence = copy.deepcopy(evidence)
        evidence['quote']['price'] = '2'
        with self.assertRaisesRegex(ValueError,'immutable'):
            self.book.record_artifact(task_id,'evidence',evidence,self.now)

    def test_real_adapters_and_persisted_model_audits_complete_paper_entry(self):
        """Exercise the production path with mocked HTTP, not fictional model decisions."""
        from sniper.helium.analyst import Grok
        from sniper.helium.reviewer import Reviewer
        from sniper.helium.demo import fictional_response
        root = self.root/'http-fixture'
        runtime = root/'runtime'
        runtime.mkdir(parents=True)
        (runtime/'xai-api-key.txt').write_text('isolated-fixture-xai')
        (runtime/'typesafe-api-key.txt').write_text('isolated-fixture-jev')
        book = Paper(root/'helium_bot/runtime',self.cfg,initialize=True,clock=self.clock)
        self.addCleanup(book.close)
        providers = FictionalProviders(self.clock)
        providers.profiles = lambda:[address(1)]
        calls = []
        def grok_transport(*args,**kwargs):
            calls.append('grok')
            return {'choices':[{'finish_reason':'stop','message':{'content':encode({
                'risk':3,'reasons':['liquidity=100000 max_single_holder=0.02 top10_share=0.20'],
                'missing_data':[]})}}], 'usage':{'cost_in_usd_ticks':1000000}}
        def jev_transport(*args,**kwargs):
            calls.append('jev')
            return fictional_response()
        model = Grok(root,book,self.clock,grok_transport)
        reviewer = Reviewer(root,book,self.clock,jev_transport)
        binding = model.binding()
        (book.folder/'paid-ai.json').write_text(encode({'enabled':True,'operator_authorized':True,
            'approval_reference':'isolated test fixture only','binding':binding}))
        (book.folder/'inference-test.json').write_text(encode({'binding':binding,'validated':True,'tested_at':self.now}))
        (book.folder/'paid-jev.json').write_text(encode({'enabled':True,'operator_authorized':True,
            'approval_reference':'isolated test fixture only','binding':reviewer.binding(),'daily_budget_usd':'.02'}))
        with patch('rug_desk.model_gate.status',return_value={'enabled':True}):
            self.assertEqual(scan(root,book,providers,model,reviewer,self.clock),[])
            self.assertTrue(model.gate()['enabled'])
        self.assertEqual(calls,['grok','jev'])
        self.assertEqual(len(book.positions()),1)
        self.assertEqual(book.db.execute('SELECT count(*) FROM api_requests').fetchone()[0],2)
        result = book.snapshot(self.now)
        self.assertEqual(result['summary']['unknown_api_bills'],1)
        self.assertIsNone(result['summary']['net_pnl'])
        task = book.db.execute('SELECT * FROM team_tasks').fetchone()
        self.assertEqual(task['state'],'BUY')
        self.assertNotIn('demo',book.artifact(task['task_id'],'review'))


if __name__ == '__main__':
    unittest.main()
