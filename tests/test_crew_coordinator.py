"""Isolated coordinator lifecycle; no external service or strategy mutations."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rug_desk.book import Book
from sniper.crew import initialize,run,status,settings,Coordinator
from sniper.crew_evidence import Producer,EvidenceStore,encode,digest
from sniper.crew_review import review,persist_review,request_recheck
from sniper.jev_review import Journal
from sniper.jev_dossier import build
from test_rug_desk import NOW,MINT,POOL,key,quote
from test_crew_evidence import UnavailableRpc


class Market:
    def __init__(self,clock):self.clock=clock;self.calls=[]
    def quote(self,mint,pool):
        self.calls.append((mint,pool))
        return {**quote(mint,now=int(self.clock())),'pool':pool,'sol_usd':'200','sol_observed_at':int(self.clock())}


class FakeProducer(Producer):
    calls=[]
    def __init__(self,*args,**kwargs):
        kwargs['rpc']=UnavailableRpc()
        super().__init__(*args,**kwargs)
    def capture(self,identity,mint,pool,**kwargs):
        self.calls.append((mint,pool,self.budget_seconds))
        return super().capture(identity,mint,pool,raw_report={'mint':mint,'totalHolders':5},**kwargs)


class CrewCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=Book(self.root/'rug_desk',initialize=True)
        self.source.enable_costs(NOW-1);self.addCleanup(self.source.close)
        self.clock_value=NOW;self.clock=lambda:self.clock_value
        # Provider/CPU work is simulated. Host contention must not consume the
        # fixture's budget and change how many source rows the scenario imports.
        self.monotonic_value=100.0;self.monotonic=lambda:self.monotonic_value
        self.market=Market(self.clock);FakeProducer.calls=[]
        patcher=patch('sniper.crew_comparison.code_binding',return_value={'fixture':'a'*64})
        patcher.start();self.addCleanup(patcher.stop)
    def init(self):return initialize(self.root,authorization_reference='isolated fixture authorization',now=NOW)
    def source_event(self,kind,payload,at=None,recorded=None):
        at=self.clock_value if at is None else at;recorded=at if recorded is None else recorded
        with self.source.transaction():
            return self.source.db.execute('INSERT INTO observation_events(event_key,observed_at,recorded_at,kind,payload) VALUES(?,?,?,?,?)',
                (str(self.source.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM observation_events').fetchone()[0]),at,recorded,kind,encode(payload))).lastrowid
    def event(self,kind,data,at=None,**changes):
        s=settings(self.root)
        payload={'schema':'crew_source_input_v1','source_account_id':s['source_account_id'],
                 'source_config_sha256':s['source_config_sha256'],'comparison_id':s['comparison_id'],'input':data}
        payload.update(changes)
        return self.source_event('crew_'+kind+'_input',payload,at)
    def candidate(self,mint=MINT,pool=POOL,at=None):
        at=self.clock_value if at is None else at
        return {'result':{'mint':mint,'pool':{**self.market.quote(mint,pool),'observed_at':at},'facts':{},'stage':'facts','entry':False},
                'baseline_packet':None,'crew_packet':None,'producer_records':[]}
    def worker(self):return run(self.root,clock=self.clock,monotonic=self.monotonic,producer_factory=FakeProducer,market=self.market)
    def coord(self):
        c=Coordinator(self.root,settings(self.root),clock=self.clock)
        self.addCleanup(c.close);return c
    def test_disabled_missing_status_read_does_not_initialize(self):
        self.assertEqual(status(self.root)['status'],'NOT_INITIALIZED')
        self.assertEqual(self.worker()['status'],'DISABLED')
        self.assertFalse((self.root/'runtime/crew').exists())
    def test_explicit_init_highwater_separate_identity_no_clock(self):
        old=self.source_event('crew_candidate_input',{'old':'history'})
        identity=deepcopy(self.source.identity);v=self.init()
        self.assertEqual(v['source_highwater'],old)
        self.assertEqual(v['source_account_id'],identity['account_id'])
        r=self.worker();self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['imported'],0)
        self.assertEqual(self.source.identity,identity);self.assertEqual(self.source.fills(),[])
        for report in r['comparison']['variants'].values():
            self.assertEqual(report['state'],'SETUP_BLOCKED');self.assertEqual(report['new_fills'],0)
        with self.assertRaises(ValueError):self.init()
    def test_missing_authorization_does_not_create_accounts(self):
        with self.assertRaises(ValueError):initialize(self.root,authorization_reference=' ',now=NOW)
        self.assertFalse((self.root/'runtime/crew-comparison').exists())
    def test_prospective_intake_exact_pool_and_provider_status(self):
        self.init();seq=self.event('candidate',self.candidate());r=self.worker()
        self.assertEqual(r['status'],'OK');self.assertEqual(r['cursor'],seq)
        self.assertEqual(len(FakeProducer.calls),1);self.assertLessEqual(FakeProducer.calls[0][2],10)
        self.assertEqual(r['provider_status']['exit'],'UNKNOWN')
        self.assertTrue(r['provider_status']['errors']);self.assertEqual(r['paid_calls'],0)
        self.assertEqual(r['comparison']['variants']['baseline']['new_fills'],0)
        self.assertEqual(self.source.fills(),[])
        self.clock_value+=1;r2=self.worker();self.assertEqual(r2['intake']['imported'],0)
        c=self.coord();self.assertEqual(c.db.execute('SELECT COUNT(*) FROM imported').fetchone()[0],1)
    def test_market_payload_full_cost_fields_preserved(self):
        self.init();q=self.market.quote(MINT,POOL);self.event('market',q);r=self.worker()
        self.assertEqual(r['status'],'OK')
        c=self.coord();p=json.loads(c.comparison.db.execute("SELECT payload FROM observations WHERE kind='watch'").fetchone()[0])
        self.assertEqual(p['quotes'][0],q)
    def test_wrong_binding_future_and_late_inputs_are_audited_gaps(self):
        self.init();self.event('candidate',self.candidate(),comparison_id='changed')
        self.source_event('crew_capture_gap',{'reason':'fixture'},NOW+1,NOW+1)
        self.worker();self.clock_value+=2
        self.event('market',self.market.quote(MINT,POOL));self.worker()
        self.event('candidate',self.candidate(at=NOW),at=NOW);r=self.worker()
        self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['gaps'],1)
        c=self.coord();reasons=[x[0] for x in c.db.execute('SELECT reason FROM imported ORDER BY seq')]
        self.assertEqual(reasons,['invalid_source_input','invalid_source_input',None,'late_source_input'])
        self.assertEqual(self.source.fills(),[])
    def test_tape_capture_then_cursor_crash_replays_idempotently(self):
        self.init();data=self.candidate();seq=self.event('candidate',data)
        c=self.coord();event_key=settings(self.root)['source_stream_id']+':'+str(seq)
        c.comparison.capture(event_key,'candidate',NOW,data,captured_at=NOW)
        with closing(Journal(self.root)) as j:
            out=c.import_inputs(j,deadline=float('inf'))
        self.assertEqual(out['imported'],1)
        self.assertEqual(c.comparison.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
        self.assertEqual(c.cursor(),seq)
    def test_gap_then_cursor_crash_retains_original_gap_time(self):
        self.init();seq=self.source_event('crew_capture_gap',{'reason':'fixture'})
        c=self.coord();key=settings(self.root)['source_stream_id']+':'+str(seq)
        row=dict(self.source.db.execute('SELECT * FROM observation_events WHERE seq=?',(seq,)).fetchone())
        source={'id':key,'kind':row['kind'],'observed_at':row['observed_at'],'recorded_at':row['recorded_at'],'payload_sha256':digest(row['payload'])}
        c._gap('gap:'+key,'source_capture_gap',source);self.clock_value+=5
        with closing(Journal(self.root)) as j:c.import_inputs(j,deadline=float('inf'))
        row=c.comparison.db.execute('SELECT * FROM observations').fetchone()
        self.assertEqual(row['observed_at'],NOW);self.assertEqual(c.cursor(),seq)
    def test_32_rows_bounded_intake_and_eight_pinned_target_limit(self):
        self.init()
        for i in range(40):self.event('candidate',self.candidate(mint=key(i+70),pool=key(i+110)))
        c=self.coord()
        with closing(Journal(self.root)) as j:out=c.import_inputs(j,deadline=float('inf'))
        self.assertEqual(out['imported'],32);self.assertEqual(c.db.execute('SELECT COUNT(*) FROM targets').fetchone()[0],8)
        self.assertEqual(out['source_backlog'],8)
    def test_known_backlog_and_early_gap_do_not_jump_tape_clock(self):
        self.init();self.event('candidate',self.candidate(),comparison_id='bad')
        for i in range(40):self.event('candidate',self.candidate(mint=key(i+70),pool=key(i+110)))
        self.clock_value=NOW+20
        r=self.worker();self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['source_backlog'],9)
        self.assertNotIn('supplemental_quote',r)
        c=self.coord();self.assertEqual(c.comparison.db.execute('SELECT MAX(observed_at) FROM observations').fetchone()[0],NOW)
        r=self.worker();self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['gaps'],0)
    def test_expiring_intake_budget_commits_only_completed_prefix(self):
        self.init()
        for i in range(4):self.event('candidate',self.candidate(mint=key(i+70),pool=key(i+110)))
        c=self.coord()
        c.monotonic=lambda:2.0 if c.db.execute('SELECT COUNT(*) FROM imported').fetchone()[0] else 0.0
        with closing(Journal(self.root)) as journal:out=c.import_inputs(journal,deadline=1.0)
        self.assertEqual(out['imported'],1);self.assertEqual(out['source_backlog'],3)
        self.assertEqual(c.cursor(),1)
        self.assertEqual(c.comparison.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
    def test_stream_or_settings_change_blocks_without_reset(self):
        self.init()
        with self.source.transaction():self.source.db.execute("UPDATE metadata SET value='changed' WHERE key='observation_stream_id'")
        with self.assertRaises(ValueError):self.worker()
        self.assertEqual(self.source.fills(),[])
    def test_expired_deadline_no_provider_or_run(self):
        self.init();r=run(self.root,deadline=0,clock=self.clock,producer_factory=FakeProducer)
        self.assertEqual(r['status'],'DEFERRED');self.assertEqual(FakeProducer.calls,[])
    def test_due_recheck_is_fresh_skip_no_previous_model(self):
        self.init();data=self.candidate();data['result']['facts']={'observed_at':NOW-100,'checks':{'fixture':True}}
        self.event('candidate',data);self.worker()
        raw=data['result'];row={'mint':MINT,'at':NOW,'stage':'facts','reason':'fixture','payload':encode(raw)}
        d,b=build('rug_desk',self.source.identity,row);packet=review(d,b,now=NOW)
        with closing(Journal(self.root)) as journal:
            persist_review(journal.db,packet);identifier=request_recheck(journal.db,packet['packet_id'],now=NOW)
            due=journal.db.execute('SELECT due_at FROM crew_rechecks WHERE request_id=?',(identifier,)).fetchone()[0]
        self.clock_value=due
        with patch('sniper.crew.review',wraps=review) as reviewed:r=self.worker()
        self.assertEqual(r['status'],'OK');self.assertEqual(r['recheck']['state'],'COMPLETE')
        fresh=reviewed.call_args.args[0]
        self.assertEqual(fresh['source_refs'][fresh['provider_checks_source_ref']]['observed_at'],NOW-100)
        self.assertEqual(fresh['snapshot_at'],due)
        with closing(Journal(self.root)) as journal:
            row=journal.db.execute('SELECT * FROM crew_rechecks WHERE request_id=?',(identifier,)).fetchone()
            self.assertEqual(row['attempts'],1);self.assertEqual(row['state'],'COMPLETE')
            p=json.loads(journal.db.execute('SELECT payload FROM crew_packets WHERE packet_id=?',(row['result_packet_id'],)).fetchone()[0])
            self.assertGreater(p['snapshot_at'],NOW);self.assertFalse(p['execution_authority']);self.assertEqual(p['verdict'],'VETO')
            self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM assessments WHERE attempted=1').fetchone()[0],0)
        self.assertEqual(self.source.fills(),[])
    def test_actual_failed_check_capture_keeps_rejected_candidate_and_target(self):
        self.init();raw=self.candidate()['result']
        with patch('sniper.crew_capture.time.time',return_value=NOW), patch('sniper.paper_events.time.time_ns',return_value=NOW*10**9):
            self.source.claim(MINT,NOW)
            self.source.finish(MINT,NOW,'facts','SKIP','fixture_missing_facts',raw)
        r=self.worker()
        self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['gaps'],0)
        c=self.coord()
        self.assertEqual(c.db.execute('SELECT COUNT(*) FROM targets').fetchone()[0],1)
        self.assertEqual(c.comparison.db.execute("SELECT COUNT(*) FROM observations WHERE kind='candidate'").fetchone()[0],1)
        with closing(Journal(self.root)) as journal:
            self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM crew_packets').fetchone()[0],1)
            self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM crew_rechecks').fetchone()[0],1)
        self.assertNotIn('later_rejected_outcomes',r['comparison']['variants']['baseline'])
        self.assertLess(len(encode(r)),10000)
    def test_complete_isolated_evidence_capture_coordinator_both_books_and_original_exit(self):
        from test_crew_comparison import CrewComparisonTests,full_quote
        from test_crew_review import replace_record
        from sniper.jev import POLICY
        from sniper.crew_evidence import REVISION
        with self.source.transaction():
            self.source.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode({'account_id':self.source.identity['account_id'],
                'started_at':NOW-1,'ends_at':NOW+10000}),))
        self.init()
        helper=CrewComparisonTests();helper.root=self.root/'rug_desk';helper.source=self.source;helper.account=self.source.identity['account_id']
        p=helper.full_payload();assessment=p['baseline_packet']['assessment'];d=assessment['dossier'];b=assessment['baseline']
        with closing(EvidenceStore(self.root)) as store:
            for i in range(len(p['producer_records'])):
                replace_record(d,p['producer_records'],i,lambda r:r.update(source_id=store.source_id,producer_revision=REVISION))
                store.put(p['producer_records'][i])
        assessment['id']=digest({'dossier':d,'policy':POLICY})
        with closing(Journal(self.root)) as journal:
            with journal.db:
                journal.db.execute('INSERT INTO assessments(id,candidate_id,account_id,snapshot_at,started,finished,status,dossier,baseline,result,attempted,watch_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (assessment['id'],d['candidate_id'],d['account_id'],NOW,NOW,NOW,'COMPLETE',encode(d),encode(b),encode(assessment['result']),1,0))
        raw=p['result'];raw['jev']={'assessment_id':assessment['id']}
        with patch('sniper.crew_capture.time.time',return_value=NOW),patch('sniper.paper_events.time.time_ns',return_value=NOW*10**9),\
                patch('sniper.crew_comparison.paid_authority',return_value={'observed_at':NOW,'grok_enabled':True,'jev_enabled':True}):
            self.source.claim(MINT,NOW)
            self.source.finish(MINT,NOW,'judge','BUY','isolated_complete_fixture',raw)
        r=self.worker();self.assertEqual(r['status'],'OK');self.assertEqual(r['intake']['gaps'],0)
        c=self.coord()
        for book in c.comparison.portfolios().values():
            self.assertEqual(len(book.fills()),1)
            self.assertIsNotNone(book.fills()[0]['execution_costs'])
        self.clock_value=NOW+1
        with patch('sniper.paper_events.time.time_ns',return_value=(NOW+1)*10**9):
            self.source.record_quote(full_quote(NOW+1,price='.5'),NOW+1,'isolated_stop')
        r=self.worker();self.assertEqual(r['status'],'OK')
        for book in c.comparison.portfolios().values():
            self.assertEqual(len(book.fills()),2);self.assertEqual(book.positions(),[])
        self.assertEqual(self.source.fills(),[])
        self.assertEqual(r['paid_calls'],0);self.assertEqual(r['signing_calls'],0);self.assertEqual(r['broadcast_calls'],0)


if __name__=='__main__':unittest.main()
