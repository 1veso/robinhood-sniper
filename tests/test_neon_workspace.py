"""Read-only NEON presentation over the original candidate/account/exit engine."""
import json
import http.client
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase,main
from unittest.mock import patch

from rug_desk.book import encode
from rug_desk.check import check
from rug_desk.watch import apply_quote,watch
from rug_desk.trader_acceptance import ReplayJudge,ReplayMarket,ReplayRugcheck,key
from sniper.neon import initialize,NeonBook
from sniper.neon_workspace import candidate,journal
from sniper.solana_console import view,events
from sniper.paper_events import emit,cursor
from sniper.console import make_server,ConsoleData

NOW=1793228700

class NeonWorkspaceTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Path(self.tmp.name)
        initialize(self.app);self.book=NeonBook(self.app/'neon_bot')
        with self.book.transaction():self.book.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode({'started_at':NOW-1,'ends_at':NOW+86400}),))
        self.market=ReplayMarket(lambda:NOW);self.rug=ReplayRugcheck(lambda:NOW);self.judge=ReplayJudge()
    def tearDown(self):self.book.close();self.tmp.cleanup()
    def check(self,mint=None,market=None,rug=None):
        return check(self.book,mint or key(1),market=market or self.market,rugcheck=rug or self.rug,judge=self.judge,clock=lambda:NOW)
    def state(self):return view(self.app,'neon',NOW)
    def test_completed_entry_counts_closed_win_denominator_and_next_exit_step(self):
        self.assertTrue(self.check()['entry']);p=self.book.positions()[0]
        initial=self.state();self.assertEqual(initial['neon_workspace']['counts']['paper_entries'],1)
        self.assertEqual(initial['neon_workspace']['pipeline'][0]['state'],'passed')
        self.assertEqual(initial['neon_workspace']['counts']['closed_positions'],0)
        self.assertIsNone(initial['neon_workspace']['counts']['closed_win_rate_pct'])
        self.assertEqual(initial['snapshot']['positions'][0]['next_take_step'],1)
        q=self.market.quote(key(1),key(2));q['price']='2.2'
        self.assertEqual(apply_quote(self.book,p['id'],q,NOW),'take_profit_1')
        marked=self.state()['snapshot']['positions'][0]
        self.assertEqual(marked['next_take_step'],2);self.assertTrue(marked['trailing_active'])
        self.assertEqual(marked['exit_status'],'partial_recorded')
        q['price']='1.4';self.assertEqual(apply_quote(self.book,p['id'],q,NOW),'trailing_exit')
        final=self.state();self.assertEqual(final['snapshot']['open_positions'],0)
        self.assertEqual(final['neon_workspace']['counts']['closed_positions'],1)
        self.assertEqual(final['neon_workspace']['counts']['closed_wins'],1)
        self.assertEqual(final['neon_workspace']['counts']['closed_win_rate_pct'],'100')
    def test_early_rejection_matrix_and_downstream_not_run(self):
        class Missing:
            def report(self,mint):return {'mint':mint,'pool':None,'observed_at':NOW,'failure':'authority_revoked: required evidence missing','checks':{'not_rugged':True,'authority_revoked':None},'details':{}}
        self.assertFalse(self.check(rug=Missing())['entry'])
        detail=candidate(self.app,key(1));self.assertEqual(len(detail['checks']),14)
        self.assertEqual(detail['checks'][1]['result'],'missing')
        self.assertEqual(detail['checks'][2]['result'],'not run')
        self.assertFalse(detail['entry_recorded']);self.assertEqual(detail['fills'],[])
        pipe={r['id']:r for r in self.state()['neon_workspace']['pipeline']}
        self.assertEqual(pipe['filter']['state'],'blocked');self.assertEqual(pipe['analyst']['state'],'not_run')
        self.assertEqual(pipe['execution']['state'],'not_run')
    def test_buy_then_failed_refresh_is_not_entry(self):
        class Broken:
            def __init__(self,market):self.market=market;self.calls=0
            def quote(self,*args):
                self.calls+=1
                if self.calls==2:raise ValueError('Provider secret MUST NOT appear')
                return self.market.quote(*args)
        result=self.check(market=Broken(self.market));self.assertEqual(result['verdict'],'BUY');self.assertFalse(result['entry'])
        state=self.state();self.assertEqual(state['neon_workspace']['counts']['paper_entries'],0)
        pipe={r['id']:r for r in state['neon_workspace']['pipeline']}
        self.assertEqual(pipe['analyst']['state'],'passed');self.assertEqual(pipe['execution']['state'],'error')
        self.assertNotIn('MUST NOT',json.dumps(journal(self.app)))
    def test_journal_filters_totals_replay_and_precise_time(self):
        self.check();start=cursor(self.book.db)
        with self.book.transaction():
            emit(self.book,'acceptance-source','source_observation',NOW,{'source_name':'rpc','status':'ERROR','reason':'https://private.example/key secret'})
        page=journal(self.app,stage_filter='scout',category_filter='provider',limit=1)
        self.assertEqual(page['total'],1);self.assertEqual(len(page['items']),1)
        self.assertNotIn('private.example',json.dumps(page))
        self.assertIsInstance(page['items'][0]['recorded_at_ms'],int)
        packet=events(self.app,'neon',start);self.assertEqual(packet['prev_cursor'],start)
        self.assertEqual(packet,events(self.app,'neon',start))
        with self.book.transaction():
            self.book.db.execute("INSERT INTO metadata VALUES('ui_replay',?)",(encode({'account_id':self.book.identity['account_id'],'synthetic':True,'excluded_from_strategy':True}),))
        self.assertEqual(self.state()['neon_workspace']['replay']['label'],'REPLAY')
    def test_missing_mark_preserves_position_and_valuation_warning(self):
        self.check()
        class Missing:
            def quote(self,*a):raise ValueError('Unavailable')
        watch(self.book,market=Missing(),clock=lambda:NOW)
        state=self.state();self.assertEqual(state['snapshot']['open_positions'],1)
        self.assertEqual(state['snapshot']['positions'][0]['exit_status'],'price_unavailable_hold')
        self.assertEqual(len(self.book.fills()),1)
    def test_new_leader_rejection_does_not_inherit_prior_passes_and_reason_dedup(self):
        from sniper.paper_events import stage
        self.check()
        with self.book.transaction():
            stage(self.book,key(1),NOW+1,'leader','COMPLETED',verdict='SKIP',reason='Leader not selected',event_id='new-rejected-leader')
        pipe={r['id']:r for r in self.state()['neon_workspace']['pipeline']}
        self.assertEqual(pipe['scout']['state'],'blocked')
        self.assertEqual(pipe['filter']['state'],'not_run');self.assertEqual(pipe['execution']['state'],'not_run')
        with self.book.transaction():
            stage(self.book,key(3),NOW,'facts','COMPLETED',verdict='SKIP',reason='Missing report')
            stage(self.book,key(3),NOW,'facts','COMPLETED',verdict='SKIP',reason='Missing report',event_id='same-terminal')
        page=journal(self.app,reason_filter='Missing report')
        self.assertEqual(page['total'],2);self.assertEqual(page['rejections'][0]['count'],1)
    def test_missing_precision_stays_unknown_and_sensitive_observations_stay_server_side(self):
        start=cursor(self.book.db)
        with self.book.transaction():
            emit(self.book,'private-error','candidate_stage',NOW,{'mint':key(1),'stage':'facts','status':'COMPLETED','verdict':'SKIP','reason':'HTTP https://private.example/key?apikey=abc Bearer private-secret'})
            self.book.db.execute("INSERT INTO observation_events(event_key,observed_at,recorded_at,kind,payload) VALUES(?,?,?,?,?)",('legacy',NOW,NOW,'position_changed',encode({'id':'position','mint':key(1),'feed_error':'https://private.example/credential'})))
        packet=events(self.app,'neon',start)
        self.assertEqual(packet['event_count'],len(packet['events']))
        self.assertEqual(packet['events'][0]['previous_cursor'],start)
        self.assertEqual(packet['events'][1]['previous_cursor'],packet['events'][0]['id'])
        self.assertNotIn('private.example',json.dumps(packet));self.assertNotIn('private-secret',json.dumps(packet))
        self.assertIsNone(packet['events'][-1]['recorded_at_ms'])
        self.assertIs(packet['events'][-1]['data']['feed_error'],True)
    def test_malformed_candidate_numbers_and_wrong_replay_binding(self):
        self.check()
        with self.book.transaction():
            raw=json.loads(self.book.db.execute('SELECT payload FROM checks WHERE mint=?',(key(1),)).fetchone()[0])
            raw['judgment']['confidence']='NaN';raw['pool']['liquidity']='NaN'
            self.book.db.execute('UPDATE checks SET payload=? WHERE mint=?',(encode(raw),key(1)))
            self.book.db.execute("INSERT INTO metadata VALUES('ui_replay',?)",(encode({'account_id':'other-account','synthetic':True,'excluded_from_strategy':True}),))
        detail=candidate(self.app,key(1));self.assertEqual(len(detail['checks']),14)
        self.assertEqual(detail['checks'][-1]['result'],'missing');self.assertIsNone(detail['judgment']['confidence'])
        self.assertEqual(detail['checks'][9]['result'],'missing');self.assertIsNone(self.state()['neon_workspace']['replay'])
    def test_abandoned_running_and_provider_failure_counts_are_explicit(self):
        with self.book.transaction():
            self.book.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',('old','signals',NOW-1000,None,'running','{}'))
            emit(self.book,'source-error','source_observation',NOW,{'source_name':'rpc','status':'ERROR','reason':'RPC request failed'})
        workspace=self.state()['neon_workspace']
        self.assertNotEqual(workspace['worker']['state'],'running');self.assertEqual(workspace['worker']['running_jobs'],[])
        self.assertEqual(workspace['counts']['provider_failures'],1);self.assertEqual(workspace['counts']['worker_failures'],0)
    def test_last_watch_completion_survives_recent_scan_window(self):
        self.check()
        with self.book.transaction():
            self.book.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',('old-watch','watch',NOW-2000,NOW-1990,'ok','{}'))
            self.book.db.executemany('INSERT INTO runs VALUES(?,?,?,?,?,?)',[(str(n),'signals',NOW-1000+n,NOW-999+n,'ok','{}') for n in range(40)])
        state=self.state()
        self.assertEqual(state['neon_workspace']['worker']['last_watch_at'],NOW-1990)
        self.assertEqual(state['snapshot']['positions'][0]['last_watcher_completion'],NOW-1990)
    def test_duplicate_attempt_has_no_inherited_passes_and_keeps_guard(self):
        from sniper.paper_events import stage
        self.check()
        with self.book.transaction():
            stage(self.book,key(1),NOW,'entry','COMPLETED',verdict='BUY',entry=True,event_id='same-leader-id')
            stage(self.book,key(1),NOW+1,'deduplication','COMPLETED',verdict='SKIP',reason='duplicate_event',event_id='same-leader-id')
        state=self.state();pipeline={r['id']:r for r in state['neon_workspace']['pipeline']}
        self.assertEqual(pipeline['scout']['state'],'rejected')
        for lane in ('filter','analyst','execution'):self.assertEqual(pipeline[lane]['state'],'not_run')
        self.assertEqual(pipeline['guard']['count'],1)
        self.assertEqual(state['neon_workspace']['counts']['paper_entries'],1)
    def test_read_only_endpoints_and_wait_resume(self):
        server=make_server(self.app,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(path,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            try:c.request('GET',path,headers=headers or {});r=c.getresponse();return r.status,json.loads(r.read())
            finally:c.close()
        try:
            self.check();start=cursor(self.book.db)
            with patch.object(ConsoleData,'jobs',return_value={'items':[]}):
                status,data=get('/api/state?bot=neon');self.assertEqual(status,200)
            self.assertEqual(data['account_id'],self.book.identity['account_id'])
            self.assertEqual(get('/api/neon-candidate?mint='+key(1))[0],200)
            self.assertEqual(get('/api/neon-journal?stage=execution&outcome=filled')[1]['total'],2) # Fill + completed admission are distinct.
            now=time.monotonic();code,empty=get('/api/events?bot=neon&after='+start+'&wait_ms=100')
            self.assertEqual(code,200);self.assertEqual(empty['events'],[]);self.assertGreaterEqual(time.monotonic()-now,.09)
            self.assertEqual(empty['prev_cursor'],start);self.assertIn('server_time_ms',empty)
            self.assertEqual(get('/api/neon-journal?stage=madeup')[0],409)
            self.assertEqual(get('/api/neon-journal',{'Origin':'https://evil.example'})[0],403)
            self.assertEqual(get('/api/neon-candidate?mint=../runtime')[0],409)
            self.assertEqual(len(self.book.fills()),1)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':main()
