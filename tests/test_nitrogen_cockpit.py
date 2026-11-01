"""Read-only presentation, account boundaries and durable journal contracts."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper.nitrogen.book import Book
from sniper.nitrogen.config import load,encode
from sniper.nitrogen.demo import address,purchase,evidence,SyntheticModel,response
from sniper.nitrogen.view import view
from sniper.nitrogen.cockpit import history,journal

ROOT=Path(__file__).resolve().parents[1]


class CockpitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.now=1800000000;cfg=load(ROOT);cfg['tracked_wallets']=[address(1)]
        (self.root/'nitrogen_bot').mkdir();(self.root/'nitrogen_bot/config.json').write_text(encode(cfg))
        self.book=Book(self.root,cfg,initialize=True,demo=True,clock=lambda:self.now);self.addCleanup(self.book.close)
    def stage(self,n=20,answer=None):
        b=self.book;lid=b.discover_purchase(purchase(self.now,address(n),address(n+1)),self.now);e=evidence(b,b.lead(lid),self.now)
        b.assess(lid,e,SyntheticModel(b,[answer or response()]),self.now);return e
    def test_projection_does_not_reconstruct_or_write_history(self):
        before=self.book.db.total_changes
        s=view(self.root,self.now);self.assertEqual(s['cockpit']['history'],[])
        self.assertEqual(self.book.db.total_changes,before)
        self.assertFalse((self.book.folder/'cockpit.sqlite3').exists())
    def test_recorded_values_and_unknown_marks_remain_exact(self):
        b=self.book;e=self.stage();b.approve(b.pending()[0]['id'],e,self.now);b.publish()
        first=b.snapshot(self.now)['summary']['equity'];self.now+=61;b.publish()
        h=history(self.root,b.identity);self.assertEqual(h[0]['equity'],first);self.assertIsNone(h[-1]['equity'])
        self.assertEqual([r['at'] for r in h],[self.now-61,self.now]);self.assertEqual(len(h),2)
        b.publish();self.assertEqual(len(history(self.root,b.identity)),2)
        view(self.root,self.now);self.assertEqual(len(history(self.root,b.identity)),2)
    def test_foreign_sidecar_never_leaks(self):
        b=self.book;b.publish()
        with closing(sqlite3.connect(b.folder/'cockpit.sqlite3')) as db:
            db.execute('UPDATE identity SET account=?',('radon:other',));db.commit()
        self.assertTrue(view(self.root,self.now)['cockpit']['unavailable'])
        self.assertEqual(view(self.root,self.now)['summary']['starting_cash'],'500')
    def test_presentation_failure_cannot_change_account(self):
        b=self.book;before=b.snapshot(self.now)
        with patch('sniper.nitrogen.cockpit.record',side_effect=sqlite3.OperationalError('read only')):b.publish()
        self.assertEqual(b.snapshot(self.now),before)
    def test_vetoes_and_kills_are_cumulative_not_recent_page(self):
        b=self.book;self.stage(answer=response('pass'))
        for i in range(110):b.decision(address(22),'EVIDENCE_BLOCKED',{'lead':'missing','reason':'source_unavailable'},self.now+i)
        c=view(self.root,self.now+110)['cockpit'];self.assertEqual(c['vetoes'],1)
        self.assertEqual(c['rejections'],[{'rule':'action_not_buy','label':'Jev action PASS','count':1}])
        self.assertEqual(len(c['events']),100);self.assertGreater(c['total_events'],100)
        self.assertEqual(c['roles']['JUDGE']['action'],'JUDGE')
    def test_failed_risk_distinct_per_candidate_and_rule(self):
        b=self.book;payload={'lead':'L','checks':[{'name':'liquidity','state':'FAIL'},{'name':'pool_age','state':'UNKNOWN'}]}
        b.decision(address(22),'RISK_REJECTED',payload,self.now);b.decision(address(22),'RISK_REJECTED',payload,self.now+1)
        c=view(self.root,self.now+1)['cockpit'];self.assertEqual(len(c['rejections']),1);self.assertEqual(c['rejections'][0]['count'],1)
        self.assertNotIn('pool_age',[r['rule'] for r in c['rejections']])
    def test_full_journal_cursor_paging_is_account_bound_and_durable(self):
        b=self.book;self.stage();cursor=b.identity+':0';seen=[]
        while True:
            page=journal(self.root,after=cursor,limit=2);self.assertEqual(page['prev_cursor'],cursor)
            if not page['events']:break
            seen.extend(e['id'] for e in page['events']);cursor=page['cursor']
        self.assertEqual(len(seen),len(set(seen)));self.assertEqual(len(seen),5)
        with self.assertRaisesRegex(ValueError,'account mismatch'):journal(self.root,after='other:0')
        older=journal(self.root,before=4);self.assertEqual([e['seq'] for e in older['events']],[3,2,1])
        self.assertEqual(journal(self.root,after=cursor)['events'],[])
    def test_entry_exit_and_judge_notifications_use_recorded_identity(self):
        b=self.book;e=self.stage();oid=b.pending()[0]['id'];b.approve(oid,e,self.now)
        self.now+=1;b.watch(dict(e['quote'],price='2.2',observed_at=self.now,sol_observed_at=self.now),self.now)
        events=journal(self.root)['events'];fills=[e for e in events if e['action'] in ('FILLED','EXIT')]
        self.assertTrue(all(e['fill_id'] for e in fills));self.assertEqual(len({e['id'] for e in fills}),2)
        self.assertEqual(next(e for e in fills if e['action']=='EXIT')['title'],'Partial take recorded')
        self.assertEqual(next(e for e in fills if e['action']=='FILLED')['title'],'Paper entry filled')
        self.assertFalse(next(e for e in events if e['action']=='JUDGE')['toast'])
    def test_no_observations_is_not_zero_rejections(self):
        c=view(self.root,self.now)['cockpit'];self.assertFalse(c['observations_available']);self.assertEqual(c['tokens'],[])
        self.assertIsNone(c['best_trade']);self.assertIsNone(c['worst_trade'])
    def test_quote_identity_price_and_change_have_source_times(self):
        self.stage();c=view(self.root,self.now)['cockpit'];t=c['tokens'][0]
        self.assertEqual(t['mint'],address(20));self.assertEqual(t['at'],self.now)
        self.assertAlmostEqual(t['change_pct'],(1/.95-1)*100);self.assertIsNone(t['image'])


if __name__=='__main__':unittest.main()
