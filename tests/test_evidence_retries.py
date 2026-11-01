import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sniper.krypton.demo import Fictional,FictionalGrok,address
from sniper.krypton.config import load
from sniper.krypton.paper import Paper
from sniper.krypton.service import scan
from sniper.helium.paper import Paper as TeamPaper
from sniper.helium.service import scan as team_scan
from sniper.helium.demo import FictionalProviders,FictionalReviewer,FictionalGrok as TeamGrok

ROOT=Path(__file__).resolve().parents[1]

class EvidenceRetryTests(unittest.TestCase):
    def scenario(self,team=False):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        now=[1793345000];clock=lambda:now[0]
        cfg=load(ROOT/'krypton_bot')
        book=(TeamPaper if team else Paper)(Path(tmp.name)/'runtime',cfg,initialize=True,demo=True,clock=clock)
        self.addCleanup(book.close)
        provider=(FictionalProviders if team else Fictional)(clock)
        provider.profiles=lambda:[address(1)]
        model=(TeamGrok if team else FictionalGrok)(book,clock)
        reviewer=FictionalReviewer(book,clock) if team else None
        run=lambda:team_scan(ROOT,book,provider,model,reviewer,clock) if team else scan(ROOT,book,provider,model,clock)
        return book,provider,model,now,run

    def test_outage_then_recovery_enters_only_after_evidence(self):
        for team in (False,True):
            with self.subTest(team=team):
                b,p,m,now,run=self.scenario(team)
                with patch.object(p,'holders',side_effect=ValueError('provider unavailable')):run()
                self.assertEqual(m.calls,0)
                self.assertEqual(len(b.positions()),0)
                self.assertEqual(b.db.execute('SELECT state FROM candidates').fetchone()[0],'PENDING')
                self.assertEqual(b.pending(),[])
                now[0]+=60
                self.assertEqual(run(),[])
                self.assertEqual(len(b.positions()),1)
                self.assertEqual(m.calls,1)
                run()
                self.assertEqual(m.calls,1)

    def test_outages_have_finite_retry_limit_without_age_reset(self):
        for team in (False,True):
            with self.subTest(team=team):
                b,p,m,now,run=self.scenario(team);first=now[0]
                with patch.object(p,'quote',side_effect=ValueError('No valid pool observation')):
                    for _ in range(4):run();now[0]+=60
                row=b.db.execute('SELECT state,first_seen FROM candidates').fetchone()
                self.assertEqual(tuple(row),('ERROR',first))
                self.assertEqual(m.calls,0)
                self.assertEqual(len(b.positions()),0)
                self.assertEqual(b.db.execute('SELECT count(*) FROM decisions').fetchone()[0],3)

    def test_model_failure_is_never_replayed(self):
        for team in (False,True):
            with self.subTest(team=team):
                b,p,m,now,run=self.scenario(team)
                with patch.object(m,'ask',side_effect=ValueError('ambiguous paid response')) as ask:
                    run();now[0]+=60;run()
                    self.assertEqual(ask.call_count,1)
                self.assertEqual(b.db.execute('SELECT state FROM candidates').fetchone()[0],'ERROR')
