"""Persistence, provenance and read-only guarantees for Helium's cockpit."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper.helium.config import load
from sniper.helium.cockpit import candidate, journal, market
from sniper.helium.demo import FictionalProviders, FictionalGrok, FictionalReviewer, address
from sniper.helium.paper import Paper
from sniper.helium.service import scan, watch
from sniper.helium.view import view

ROOT=Path(__file__).resolve().parents[1]


class HeliumCockpitBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1793345000;self.clock=lambda:self.now
        self.cfg=load(ROOT/'helium_bot')
        self.book=Paper(self.root/'helium_bot/runtime',self.cfg,initialize=True,demo=True,clock=self.clock)
        self.addCleanup(lambda:self.book.close())
        self.providers=FictionalProviders(self.clock);self.providers.profiles=lambda:[address(1)]
        self.model=FictionalGrok(self.book,self.clock);self.reviewer=FictionalReviewer(self.book,self.clock)

    def entry(self):
        self.assertEqual(scan(self.root,self.book,self.providers,self.model,self.reviewer,self.clock),[])

    def state(self):
        return view(self.root,self.now)

    def test_fill_notification_is_in_the_financial_transaction(self):
        self.entry();before=self.state();cursor=before['helium_workspace']['cursor']
        self.now+=1;self.providers.price='2'
        q=self.providers.costs(self.providers.quote(address(1),self.book.positions()[0]['pool']))
        with patch('sniper.helium.cockpit.observe',side_effect=RuntimeError('observation transaction failure')):
            with self.assertRaisesRegex(RuntimeError,'observation transaction'):
                self.book.watch(q,self.now)
        after=self.state()
        self.assertEqual(after['summary']['fill_count'],1)
        self.assertEqual(len(after['positions']),1)
        self.assertEqual(after['summary']['cash'],before['summary']['cash'])
        self.assertEqual(journal(self.root,after=cursor)['events'],[])
        self.assertTrue(self.book.watch(q,self.now))
        events=journal(self.root,after=cursor)['events']
        self.assertEqual([e['side'] for e in events if e['kind']=='fill'],['SELL'])

    def test_cursor_catchup_cannot_lose_fill_beyond_recent_snapshot(self):
        cursor=self.state()['helium_workspace']['cursor'];self.entry()
        for index in range(125):
            self.book.provider('fixture',{'state':'OK','finished_at':self.now+index})
        snapshot=self.state()['helium_workspace']
        self.assertTrue(snapshot['events_truncated'])
        self.assertFalse(any(e['kind']=='fill' for e in snapshot['events']))
        events=[]
        while True:
            page=journal(self.root,after=cursor,limit=7)
            self.assertEqual(page['prev_cursor'],cursor)
            events.extend(page['events']);cursor=page['cursor']
            if not page['has_more']:break
        self.assertEqual(len(events),len({e['id'] for e in events}))
        self.assertEqual([e['side'] for e in events if e['kind']=='fill'],['BUY'])
        self.assertEqual(cursor,snapshot['cursor'])
        self.assertEqual(journal(self.root,after=cursor)['events'],[])
        for bad in ('another-account:1',self.book.identity+':999999',self.book.identity+':-1'):
            with self.assertRaises(ValueError):journal(self.root,after=bad)

    def test_recent_api_success_does_not_refresh_an_old_market_quote(self):
        other_root=self.root/'ordinary-account'
        ordinary=Paper(other_root/'helium_bot/runtime',self.cfg,initialize=True,clock=self.clock)
        self.addCleanup(ordinary.close)
        with ordinary.transaction():
            market(ordinary,self.providers.quote(address(1)),self.now,'candidate_evidence')
        self.assertEqual(view(other_root,self.now)['helium_workspace']['data']['state'],'live')
        self.now+=61
        ordinary.provider('dexscreener',{'state':'OK','finished_at':self.now})
        result=view(other_root,self.now)
        self.assertEqual(result['helium_workspace']['data']['state'],'stale')
        self.assertEqual(result['helium_workspace']['data']['observed_at'],self.now-61)
        self.assertEqual(self.state()['helium_workspace']['data']['state'],'replay')

    def test_read_only_old_account_view_does_not_migrate_or_backfill(self):
        self.entry();identity=self.book.identity;cash=self.book.get('cash')
        self.book.close();path=self.root/'helium_bot/runtime/book.sqlite3'
        with sqlite3.connect(path) as db:
            for table in ('cockpit_events','account_observations','market_observations'):
                db.execute('DROP TABLE '+table)
            db.execute("DELETE FROM meta WHERE key LIKE 'cockpit_%'")
        with sqlite3.connect(path) as db:before='\n'.join(db.iterdump())
        old=view(self.root,self.now)
        self.assertEqual(old['account_id'],identity);self.assertEqual(old['equity_history'],[])
        self.assertFalse(old['helium_workspace']['recording_ready'])
        with sqlite3.connect(path) as db:self.assertEqual(before,'\n'.join(db.iterdump()))
        self.now+=1
        self.book=Paper(path.parent,self.cfg,demo=True,clock=self.clock)
        migrated=self.state()
        self.assertEqual(self.book.identity,identity);self.assertEqual(self.book.get('cash'),cash)
        self.assertEqual(len(migrated['equity_history']),1)
        self.assertEqual(migrated['equity_history'][0]['observed_at'],self.now)
        self.assertEqual(migrated['summary']['fill_count'],1)
        self.assertFalse(any(e['kind']=='fill' for e in migrated['helium_workspace']['events']))

    def test_missing_quote_then_recovery_clears_guard_error_without_invented_exit(self):
        self.entry();self.now+=61
        with patch.object(self.providers,'quote',side_effect=ValueError('missing')):
            self.assertTrue(watch(self.root,self.book,self.providers,self.clock))
        failed=self.state()
        self.assertIsNone(failed['equity_history'][-1]['chart_equity'])
        self.assertTrue(failed['positions'][0]['latest_quote_failed'])
        self.assertEqual(failed['positions'][0]['exit_status'],'quote_unavailable_hold')
        self.assertEqual(failed['helium_workspace']['rejections'],[])
        self.assertEqual(journal(self.root,reason_filter='Exit quote unavailable; holding retained')['total_matching_decisions'],0)
        self.assertTrue(any(e['stage']=='guard' and e['outcome']=='error'
                            for e in failed['helium_workspace']['events']))
        self.assertEqual(failed['helium_workspace']['worker']['watch']['error_count'],1)
        self.now+=1;self.providers.price='1.1'
        self.assertEqual(watch(self.root,self.book,self.providers,self.clock),[])
        recovered=self.state()
        guard=next(p for p in recovered['helium_workspace']['pipeline'] if p['id']=='guard')
        self.assertNotIn('unavailable',guard['reason'])
        self.assertFalse(recovered['positions'][0]['latest_quote_failed'])
        self.assertIsNotNone(recovered['equity_history'][-1]['chart_equity'])
        self.assertEqual(recovered['summary']['fill_count'],1)

    def test_rejection_drilldown_is_bounded_complete_and_redacted(self):
        for index in range(62):
            self.book.decision(address(2),'SKIP',{'reason':'Provider https://private.example/'+str(index)+' Bearer hidden',
                'checks':[{'name':'lp_locked','state':'UNKNOWN','reason':'password=private','scope':'https://private.example/key','value':'api_key=private'}]},self.now)
        group=self.state()['helium_workspace']['rejections'][0]
        self.assertEqual(group['count'],62)
        self.assertNotIn('private',group['reason'])
        first=journal(self.root,reason_filter=group['reason'])
        self.assertEqual(first['total_matching_decisions'],62)
        self.assertEqual(len(first['decisions']),50);self.assertTrue(first['has_more_decisions'])
        second=journal(self.root,reason_filter=group['reason'],before=first['next_before'])
        self.assertEqual(len(second['decisions']),12);self.assertFalse(second['has_more_decisions'])
        self.assertFalse({d['id'] for d in first['decisions']} & {d['id'] for d in second['decisions']})
        detail=candidate(self.root,address(2))
        self.assertEqual(len(detail['decisions']),50)
        self.assertNotIn('private',json.dumps(detail))
        with self.assertRaises(ValueError):candidate(self.root,'../runtime')

    def test_model_block_shows_passed_filter_and_blocked_analyst(self):
        with patch.object(self.reviewer,'gate',return_value={'enabled':False,'blockers':['Reviewer spending disabled']}):
            self.entry()
        pipeline={p['id']:p for p in self.state()['helium_workspace']['pipeline']}
        self.assertEqual(pipeline['filter']['state'],'passed')
        self.assertEqual(pipeline['analyst']['state'],'blocked')
        self.assertEqual(pipeline['execution']['state'],'not_run')
        self.assertEqual((self.model.calls,self.reviewer.calls),(0,0))

    def test_abandoned_scan_does_not_animate_forever(self):
        self.book.run_start('scan',self.now)
        self.book.discover([address(1)],self.now)
        self.book.begin_task(address(1),self.now)
        self.now+=181
        workspace=self.state()['helium_workspace']
        self.assertEqual(workspace['worker']['scan']['state'],'overdue')
        self.assertNotEqual(workspace['pipeline'][0]['state'],'running')
        self.assertEqual(workspace['pipeline'][0]['state'],'interrupted')

    def test_source_quote_age_can_expire_before_accounting_mark_age(self):
        self.entry();self.now+=10
        quote=self.providers.costs(self.providers.quote(address(1),self.book.positions()[0]['pool']))
        quote['observed_at']=self.now-59
        self.assertFalse(self.book.watch(quote,self.now))
        recorded=self.state()
        historical=recorded['equity_history'][-1]
        self.assertEqual(historical['observed_at'],self.now)
        self.assertIsNotNone(historical['chart_equity'])
        self.assertTrue(recorded['positions'][0]['source_price_current'])
        accounting_equity=self.book.equity(self.now)
        self.now+=2
        stale=self.state();position=stale['positions'][0]
        self.assertTrue(position['accounting_mark_current'])
        self.assertFalse(position['source_price_current'])
        self.assertFalse(position['mark_current'])
        self.assertIsNone(position['unrealized_pnl'])
        self.assertIsNotNone(position['accounting_unrealized_pnl'])
        self.assertEqual(stale['summary']['accounting_unresolved_positions'],0)
        self.assertEqual(stale['summary']['source_stale_positions'],1)
        self.assertFalse(stale['summary']['valuation_current'])
        self.assertIsNone(stale['summary']['net_pnl'])
        self.assertIsNone(stale['summary']['return_pct'])
        self.assertEqual(self.book.equity(self.now),accounting_equity)
        # The read did not rewrite a valuation that was usable when recorded.
        self.assertEqual(stale['equity_history'][-1],historical)
        run_id=self.book.run_start('scan',self.now)
        self.book.run_finish(run_id,self.now,[])
        later=self.state()['equity_history'][-1]
        self.assertEqual(later['observed_at'],self.now)
        self.assertEqual(later['equity'],historical['equity'])
        self.assertIsNone(later['chart_equity'])
        self.assertIsNone(later['net_pnl'])
        self.assertEqual(later['accounting_unresolved_positions'],0)
        self.assertEqual(later['source_stale_positions'],1)


if __name__=='__main__':unittest.main()
