"""Trader selection and event integration: offline providers, isolated accounts."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from rug_desk import config
from rug_desk.book import Book, encode
from rug_desk.grok import Judge, validate
from rug_desk.traders import Selections, screen_trader, validate_rank, POLICY
from rug_desk.leader_signals import process_event, event_identity
from rug_desk.trader_acceptance import trader, event, key, ReplayJudge, ReplayMarket, ReplayRugcheck, run_acceptance

NOW=1793100000

class TraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'desk'
        self.book=Book(self.root,initialize=True)
        with self.book.transaction():self.book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
        self.book.enable_costs(NOW)
        self.store=Selections(self.book);self.clock=lambda:NOW;self.judge=ReplayJudge()
        self.market=ReplayMarket(self.clock);self.rug=ReplayRugcheck(self.clock)
    def tearDown(self):
        self.book.close();self.tmp.cleanup()
    def choose(self, records=None, judge=None):
        return self.store.select([trader(NOW)] if records is None else records,judge=judge or self.judge,clock=self.clock)
    def send(self, signal=None, **deps):
        return process_event(self.book,signal or event(NOW),clock=self.clock,judge=deps.get('judge',self.judge),
            market=deps.get('market',self.market),rugcheck=deps.get('rugcheck',self.rug))
    def test_six_acceptance_cases_and_persistence(self):
        result=run_acceptance(self.root/'isolated',NOW)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(len(result['simulated_fills']),3)
        self.assertTrue(result['excluded_from_strategy_performance'])
        self.assertEqual(result['paid_model_calls'],0)
    def test_existing_numeric_history_screen_and_cost_requirements(self):
        row=trader(NOW);good=screen_trader(row,NOW,replay=True)
        self.assertEqual(good['metrics']['closed_positions'],20)
        self.assertEqual(good['metrics']['average_hold_minutes'],'60')
        for field in ('gas_usd','source_ref','verified'):
            bad=copy.deepcopy(row);del bad['history']['records'][0][field]
            with self.subTest(field=field),self.assertRaises((ValueError,KeyError)):
                screen_trader(bad,NOW,replay=True)
        bad=copy.deepcopy(row);bad['history']['records']=bad['history']['records'][:38]
        with self.assertRaisesRegex(ValueError,'20_closed'):screen_trader(bad,NOW,replay=True)
        for record in row['history']['records']:
            record['token']=key(100)
        with self.assertRaisesRegex(ValueError,'concentration'):screen_trader(row,NOW,replay=True)
    def test_article_flat_stats_and_missing_risk_cannot_qualify(self):
        result=self.choose([{'handle':'example','chain':'solana','wallet':key(77),'pnl_30d_usd':100000}])
        self.assertTrue(result['empty_selection']);self.assertEqual(result['leaders'],[])
        for field in ('gaps','opening_inventory'):
            row=trader(NOW);del row['history'][field]
            with self.subTest(field=field),self.assertRaises(ValueError):
                screen_trader(row,NOW,replay=True)

    def test_invalid_token_confidence_skips_before_entry(self):
        self.choose();model=ReplayJudge()
        model.ask=lambda *args,**kwargs:{'verdict':'BUY','confidence':2,'validated':True,'why':'Invalid fixture'}
        result=self.send(judge=model)
        self.assertEqual(result['verdict'],'SKIP')
        self.assertFalse(result['entry']);self.assertEqual(self.book.fills(),[])
    def test_explicit_empty_publication_replaces_previous_selection_without_model(self):
        self.choose();model=Mock();model.rank_traders.side_effect=AssertionError('No call needed')
        result=self.choose([],model)
        self.assertTrue(result['empty_selection']);model.rank_traders.assert_not_called()
        exported=json.loads((self.book.runtime/'leaders.json').read_text())
        self.assertEqual(exported['leaders'],[])
        self.assertEqual(self.send()['verdict'],'SKIP')
    def test_failed_pending_stale_and_corrupt_updates_block(self):
        first=self.choose()
        pending=self.store.begin(NOW)
        self.assertEqual(self.send()['reason'],'selection_updating')
        self.assertEqual(self.store.status(NOW+181)['status'],'UPDATE_STALLED')
        self.store.publish(pending,NOW,'FAILED',{'leaders':[],'policy':POLICY})
        self.assertEqual(self.send(event(NOW,key(3)))['reason'],'selection_failed')
        self.choose()
        self.assertEqual(self.store.status(NOW+POLICY['selection_seconds'])['status'],'STALE')
        result=self.store.select(lambda: (_ for _ in ()).throw(ValueError('broken JSON')),clock=self.clock)
        self.assertEqual(result['status'],'FAILED');self.assertEqual(result['leaders'],[])
        self.assertNotEqual(result['selection_id'],first['selection_id'])
    def test_superseded_model_completion_cannot_restore_previous_leaders(self):
        old=self.store.begin(NOW);new=self.choose([])
        self.store.publish(old,NOW,'COMPLETE',{'policy':POLICY,'leaders':[{'handle':'old'}],'expires_at':NOW+100})
        self.assertEqual(self.store.status(NOW)['selection_id'],new['selection_id'])
        self.assertTrue(self.store.status(NOW)['empty_selection'])
    def test_paid_ranking_disabled_reads_no_key_and_clears_previous_selection(self):
        self.choose();transport=Mock(side_effect=AssertionError('Paid call forbidden'))
        result=self.choose(judge=Judge(self.root,transport=transport))
        self.assertEqual(result['status'],'FAILED');self.assertEqual(result['leaders'],[])
        transport.assert_not_called()
        self.assertFalse((self.book.runtime/'ai-budget.sqlite3').exists())
    def test_model_finite_confidence_verdict_and_exact_eligible_distinct_leaders(self):
        candidates=[screen_trader(trader(NOW,name='leader'+str(i),wallet=key(70+i)),NOW,replay=True) for i in range(4)]
        good={'verdict':'SELECT','confidence':.8,'leaders':[{'handle':'leader0','rank':1,'evidence_fields':['win_rate']}]}
        variants=[]
        for value in (True,-.1,1.01,float('nan'),float('inf'),'0.8'):
            bad=copy.deepcopy(good);bad['confidence']=value;variants.append(bad)
        for verdict in ('COPY','BUY','garbage'):
            bad=copy.deepcopy(good);bad['verdict']=verdict;variants.append(bad)
        bad=copy.deepcopy(good);bad['leaders'][0]['handle']='invented';variants.append(bad)
        bad=copy.deepcopy(good);bad['leaders']*=2;variants.append(bad)
        bad=copy.deepcopy(good);bad['leaders']=[{'handle':'leader'+str(i),'rank':i+1,'evidence_fields':['win_rate']} for i in range(4)];variants.append(bad)
        bad=copy.deepcopy(good);bad['leaders'][0]['evidence_fields']=['shared_funder'];variants.append(bad)
        bad=copy.deepcopy(good);bad['leaders'][0]['why']='invented controller';variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad),self.assertRaises((ValueError,TypeError)):
                validate_rank({'status':'completed','output_text':json.dumps(bad)},candidates)
        self.assertTrue(validate_rank({'status':'completed','output_text':json.dumps(good)},candidates)['validated'])
        with self.assertRaises(ValueError):validate_rank({'status':'incomplete','output_text':json.dumps(good)},candidates)
        with self.assertRaises(ValueError):validate_rank({'status':'completed','output_text':'prose '+json.dumps(good)},candidates)
    def test_low_confidence_is_completed_empty_not_accepted(self):
        model=ReplayJudge()
        model.rank_traders=lambda candidates:validate_rank({'status':'completed','output_text':encode({'verdict':'SELECT','confidence':.69,
            'leaders':[{'handle':candidates[0]['handle'],'rank':1,'evidence_fields':['win_rate']}]})},candidates)
        self.assertTrue(self.choose(judge=model)['empty_selection'])
    def test_missing_follower_evidence_cannot_be_cited_as_zero(self):
        row=trader(NOW);row.pop('followers')
        candidates=[screen_trader(row,NOW,replay=True)]
        out={'verdict':'SELECT','confidence':.8,'leaders':[{'handle':row['handle'],'rank':1,'evidence_fields':['followers']}]}
        with self.assertRaises(ValueError):validate_rank({'status':'completed','output_text':encode(out)},candidates)
    def test_unapproved_wrong_wallet_non_solana_missing_signature_and_stale_skip_before_providers(self):
        self.choose();provider=Mock(side_effect=AssertionError('No provider needed'))
        cases=[]
        for field,value in (('handle','other'),('wallet',key(78)),('chain','eip155:4663'),('transaction_signature',''),('occurred_at',NOW-301)):
            bad=event(NOW);bad[field]=value;cases.append(bad)
        for bad in cases:self.assertEqual(self.send(bad,judge=provider,market=provider,rugcheck=provider)['verdict'],'SKIP')
        self.assertEqual(provider.mock_calls,[]);self.assertEqual(self.book.fills(),[])
    def test_other_chain_event_cannot_poison_solana_transaction_identity(self):
        self.choose();bad=event(NOW);bad['chain']='eip155:4663'
        self.assertEqual(self.send(bad)['verdict'],'SKIP')
        self.assertTrue(self.send()['entry'])
    def test_duplicate_transaction_identity_ignores_provider_ids_and_survives_restart(self):
        self.choose();signal=event(NOW);self.assertTrue(self.send(signal)['entry'])
        self.book.close();self.book=Book(self.root);self.store=Selections(self.book)
        signal['provider_event_id']='different'
        repeated=self.send(signal)
        self.assertFalse(repeated['entry']);self.assertEqual(len(self.book.fills()),1)
        self.assertEqual(repeated['stage'],'deduplication')

    def test_concurrent_same_event_has_one_entry(self):
        self.choose()
        def ingest(_):
            book=Book(self.root)
            try:
                return process_event(book,event(NOW),clock=self.clock,judge=ReplayJudge(),
                                     market=ReplayMarket(self.clock),rugcheck=ReplayRugcheck(self.clock))
            finally:book.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(ingest,range(2)))
        self.assertEqual(sum(bool(r['entry']) for r in results),1)
        self.assertEqual(len(self.book.fills()),1)
    def test_selection_change_during_judgment_blocks_atomic_entry(self):
        self.choose();model=ReplayJudge()
        def ask(*args,**kwargs):
            self.choose([])
            return ReplayJudge().ask(*args,**kwargs)
        model.ask=ask
        result=self.send(judge=model)
        self.assertFalse(result['entry']);self.assertEqual(result['verdict'],'SKIP')
        self.assertEqual(self.book.fills(),[])
    def test_entry_and_leader_decision_are_atomic_on_hook_failure(self):
        self.choose()
        with patch('rug_desk.leader_signals.record_decision',side_effect=[RuntimeError('interrupted'),None]):
            result=self.send()
        self.assertFalse(result['entry']);self.assertEqual(self.book.fills(),[])
        self.assertEqual(self.book.positions(),[])
    def test_post_commit_export_failure_never_reports_unfilled(self):
        self.choose()
        with patch.object(self.book,'export',side_effect=OSError('disk')):
            result=self.send()
        self.assertTrue(result['entry']);self.assertEqual(len(self.book.fills()),1)
        self.assertIn('reporting_error',result)
        self.assertEqual(self.book.db.execute('SELECT status FROM leader_events').fetchone()[0],'PAPER_FILLED')
    def test_paper_limits_and_missing_required_risk_still_apply(self):
        self.choose()
        for n in range(10,14):
            self.book.open_paper(self.market.quote(key(n),key(2)),self.judge.ask(),NOW)
        providers=Mock()
        result=self.send(market=providers,rugcheck=providers,judge=providers)
        self.assertFalse(result['entry']);self.assertEqual(result['stage'],'limits')
        self.assertEqual(providers.mock_calls,[])
    def test_replay_cannot_enter_existing_strategy_account(self):
        production=Book(self.root/'nonreplay',initialize=True)
        try:
            store=Selections(production)
            self.assertTrue(store.select([trader(NOW)],judge=self.judge,clock=self.clock)['empty_selection'])
            out=process_event(production,event(NOW),clock=self.clock)
            self.assertIn('replay_requires',out['reason']);self.assertEqual(production.fills(),[])
        finally:production.close()
    def test_rank_uses_same_budget_transport_model_and_no_search_calls(self):
        runtime=self.root.parent/'runtime';runtime.mkdir()
        (runtime/'xai-api-key.txt').write_text('fixture-not-a-real-key')
        candidates=[screen_trader(trader(NOW),NOW,replay=True)]
        out={'verdict':'SELECT','confidence':.8,'leaders':[{'handle':candidates[0]['handle'],'rank':1,'evidence_fields':['win_rate']}]}
        transport=Mock(return_value={'status':'completed','output_text':encode(out),
            'usage':{'input_tokens':40,'output_tokens':40,'total_tokens':80,'cost_in_usd_ticks':1000}})
        result=Judge(self.root,enabled=True,transport=transport,clock=self.clock).rank_traders(candidates)
        self.assertTrue(result['validated'])
        payload=transport.call_args.args[2]
        self.assertEqual(payload['model'],config.MODEL);self.assertNotIn('tools',payload)
        self.assertTrue((self.book.runtime/'ai-budget.sqlite3').exists())
        self.assertEqual(len(list((self.book.runtime/'x-responses').glob('*.json'))),1)
        # Exercise the actual client envelope through publication, not only its
        # standalone validator. Audit metadata must survive without becoming stats.
        selected=self.choose(judge=Judge(self.root,enabled=True,transport=transport,clock=self.clock))
        self.assertTrue(selected['entry_enabled'],selected)
        self.assertEqual(selected['ranking']['judged_at'],NOW)
        self.assertEqual(selected['ranking']['binding']['account_id'],self.book.identity['account_id'])
        self.assertEqual(selected['leaders'][0]['metrics'],candidates[0]['metrics'])

if __name__=='__main__':unittest.main()
