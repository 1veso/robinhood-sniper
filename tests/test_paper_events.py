"""Offline book -> candidate stages -> paper fill -> snapshot -> reconnect checks."""
from decimal import Decimal as D
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from rug_desk.book import Book,encode
from rug_desk.check import check
from rug_desk.watch import apply_quote,watch
from rug_desk.trader_acceptance import ReplayJudge,ReplayMarket,ReplayRugcheck,key
from sniper.neon import initialize,NeonBook
from sniper.console import ConsoleData,make_server,reader
from sniper.solana_console import view,events,chart
from sniper.paper_events import cursor

NOW=1793227000

class PaperEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Path(self.tmp.name)
        self.book=Book(self.app/'rug_desk',initialize=True);self.book.enable_costs(NOW)
        initialize(self.app);self.neon=NeonBook(self.app/'neon_bot')
        self.market=ReplayMarket(lambda:NOW);self.rug=ReplayRugcheck(lambda:NOW);self.judge=ReplayJudge()
    def tearDown(self):
        self.book.close();self.neon.close();self.tmp.cleanup()
    def check(self,mint=None):
        return check(self.book,mint or key(1),market=self.market,rugcheck=self.rug,judge=self.judge,clock=lambda:NOW)
    def page(self,start):return events(self.app,'solana',start,500)
    def test_isolated_identity_existing_roots_and_no_invented_capital(self):
        a=view(self.app,'solana',NOW);b=view(self.app,'neon',NOW)
        self.assertNotEqual(a['account_id'],b['account_id'])
        self.assertIsNone(a['snapshot']['equity']);self.assertIsNone(a['snapshot']['starting_cash'])
        self.assertEqual(a['snapshot']['net_pnl'],'0')
        self.assertEqual(b['snapshot']['starting_cash'],'1000') # Existing persisted Neon policy.
        before=b['event_stream']['cursor'];self.check()
        self.assertEqual(view(self.app,'neon',NOW)['event_stream']['cursor'],before)
        self.assertEqual(self.neon.fills(),[])
    def test_candidate_fill_event_and_original_stop_are_distinct(self):
        start=view(self.app,'solana',NOW)['event_stream']['cursor']
        self.assertTrue(self.check()['entry']);page=self.page(start)
        stages=[e['data'] for e in page['events'] if e['type']=='candidate_stage']
        self.assertTrue(any(e['stage']=='facts' and e['status']=='PASSED' for e in stages))
        self.assertTrue(any(e['stage']=='entry' and e['entry'] for e in stages))
        self.assertEqual(sum(e['type']=='paper_fill' for e in page['events']),1)
        quote=self.market.quote(key(1),key(2));quote['price']='.5'
        self.assertEqual(apply_quote(self.book,self.book.positions()[0]['id'],quote,NOW),'stop')
        state=view(self.app,'solana',NOW)
        self.assertEqual(state['snapshot']['open_positions'],0)
        self.assertEqual(len(state['closed']),1);self.assertEqual(len(state['fills']),2)
        self.assertIsNone(state['snapshot']['equity'])
        self.assertLess(D(state['snapshot']['net_pnl']),0)
    def test_rejected_candidate_and_replay_create_no_extra_fill_or_event(self):
        self.rug.missing=True;start=view(self.app,'solana',NOW)['event_stream']['cursor']
        result=self.check();self.assertFalse(result['entry'])
        first=self.page(start);self.assertTrue(any(e['type']=='candidate_stage' and e['data']['verdict']=='SKIP' for e in first['events']))
        self.assertFalse(any(e['type']=='paper_fill' for e in first['events']))
        self.check();self.assertEqual(self.page(first['cursor'])['events'],[])
        self.assertEqual(self.book.fills(),[])
    def test_paged_order_restart_replay_and_cross_account_cursor_rejection(self):
        start=view(self.app,'solana',NOW)['event_stream']['cursor'];self.check()
        first=events(self.app,'solana',start,2);self.assertTrue(first['has_more'])
        self.book.close();self.book=Book(self.app/'rug_desk')
        self.assertEqual(events(self.app,'solana',start,2),first)
        rest=self.page(first['cursor']);ids=[x['id'] for x in first['events']+rest['events']]
        self.assertEqual(len(ids),len(set(ids)))
        seq=[x['seq'] for x in first['events']+rest['events']];self.assertEqual(seq,sorted(seq))
        self.assertFalse(self.page(rest['cursor'])['events'])
        with self.assertRaises(ValueError):events(self.app,'neon',rest['cursor'])
        with self.assertRaises(ValueError):events(self.app,'solana',rest['cursor'].rsplit(':',1)[0]+':999999')
    def test_snapshot_cursor_and_book_share_one_sqlite_snapshot(self):
        with reader(self.book.runtime/'book.sqlite3') as db:
            start=cursor(db)
            self.check()
            from contextlib import contextmanager
            @contextmanager
            def pinned(_):yield db
            with patch('sniper.console.reader',pinned):state=view(self.app,'solana',NOW)
            self.assertEqual(state['event_stream']['cursor'],start);self.assertEqual(state['fills'],[])
        self.assertEqual(len(view(self.app,'solana',NOW)['fills']),1)
        self.assertTrue(self.page(start)['events'])
    def test_fill_and_event_rollback_atomically(self):
        before=cursor(self.book.db)
        with patch.object(self.book,'_event',side_effect=RuntimeError('fixture fail before commit')):
            with self.assertRaises(RuntimeError):self.book.open_paper(self.market.quote(key(1),key(2)),self.judge.ask(),NOW)
        self.assertEqual(self.book.fills(),[]);self.assertEqual(self.book.positions(),[])
        self.assertEqual(cursor(self.book.db),before)
    def test_market_identity_timestamp_dedup_and_unavailable_exit(self):
        self.check();q=self.market.quote(key(1),key(2));pid=self.book.positions()[0]['id']
        apply_quote(self.book,pid,q,NOW);head=cursor(self.book.db)
        apply_quote(self.book,pid,q,NOW);self.assertEqual(cursor(self.book.db),head)
        series=chart(self.app,'solana',key(1),key(2));self.assertTrue(series['points'])
        self.assertEqual({p['at'] for p in series['points']},{NOW})
        self.assertFalse(chart(self.app,'solana',key(1),key(9))['points'])
        market=Mock();market.quote.side_effect=ValueError('missing quote')
        watch(self.book,market=market,clock=lambda:NOW+1)
        state=view(self.app,'solana',NOW+1)
        self.assertEqual(state['snapshot']['open_positions'],1);self.assertEqual(len(state['fills']),1)
        self.assertEqual(state['snapshot']['unresolved_positions'],1)
    def test_observed_readiness_changes_without_hardcoded_setup_label(self):
        with self.book.transaction():self.book.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode({'started_at':NOW-1,'ends_at':NOW+100}),))
        report={'identity':self.book.identity,'generated_at':NOW,'paid_ai_enabled':True}
        (self.book.runtime/'report.json').write_text(encode(report))
        self.assertFalse(view(self.app,'solana',NOW)['setup_blocked'])
        self.assertEqual(view(self.app,'solana',NOW)['readiness']['state'],'ACTIVE')
        self.assertIsNone(view(self.app,'solana',NOW)['readiness']['entry_enabled']) # Never grants entry authority.
        self.assertEqual(view(self.app,'solana',NOW+3000)['readiness']['state'],'STALE')
        self.assertEqual(view(self.app,'solana',NOW+101)['status']['trial_state'],'EXITS_ONLY')
    def test_unknown_model_bill_and_missing_history_not_zero_equity(self):
        path=self.book.runtime/'ai-budget.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE advisory_reservations(request_id TEXT);CREATE TABLE advisory_usage(request_id TEXT,billed_usd TEXT);INSERT INTO advisory_reservations VALUES("unknown");')
        db.close()
        state=view(self.app,'solana',NOW)
        self.assertIsNone(state['snapshot']['net_pnl']);self.assertFalse(state['snapshot']['api_costs_known'])
        self.assertEqual(state['equity_history'],[])
    def test_config_pin_and_disabled_credentials_are_preserved(self):
        from rug_desk import config
        from rug_desk.grok import Judge
        transport=Mock(side_effect=AssertionError('External paid call forbidden'))
        judge=Judge(self.neon.root,transport=transport,budget_root=self.book.root,billing_account_id=self.neon.identity['account_id'])
        self.assertFalse(judge.recurring_enabled());self.assertEqual(judge.ask(key(1),{}, {})['verdict'],'SKIP')
        transport.assert_not_called()
        with patch.object(config,'MAX_OPEN',99):
            with self.assertRaises(RuntimeError):Book(self.book.root)
            with self.assertRaises(ValueError):view(self.app,'solana',NOW)
    def test_http_snapshot_sse_reconnect_and_read_only_boundary(self):
        def server():
            s=make_server(self.app,port=0);t=threading.Thread(target=s.serve_forever,daemon=True);t.start();return s,t
        def get(s,path,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',s.server_port,timeout=10)
            try:c.request('GET',path,headers=headers or {});r=c.getresponse();return r.status,r.read(),r.getheader('Content-Type')
            finally:c.close()
        s,t=server()
        try:
            with patch.object(ConsoleData,'jobs',return_value={'items':[]}):
                code,body,_=get(s,'/api/state?bot=solana')
            self.assertEqual(code,200);start=json.loads(body)['event_stream']['cursor'];self.check()
            code,body,_=get(s,'/api/events?bot=solana&after='+start+'&limit=2');page=json.loads(body)
            self.assertEqual(code,200);last=page['cursor']
        finally:s.shutdown();s.server_close();t.join()
        s,t=server()
        try:
            code,body,mime=get(s,'/api/events?bot=solana&after='+start,{'Accept':'text/event-stream','Last-Event-ID':last})
            self.assertEqual(code,200);self.assertIn('text/event-stream',mime)
            self.assertNotIn(('id: '+last+'\n').encode(),body)
            self.assertIn(b'event: observation',body)
            self.assertEqual(get(s,'/api/events?bot=../neon&after='+last)[0],409)
            self.assertEqual(get(s,'/api/events?bot=solana&after='+last,{'Origin':'https://evil.example'})[0],403)
            self.assertEqual(len(self.book.fills()),1)
        finally:s.shutdown();s.server_close();t.join()

if __name__=='__main__':unittest.main()
