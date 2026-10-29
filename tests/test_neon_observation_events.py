"""Research telemetry is durable and cannot admit a candidate or expose errors."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from sniper.neon import initialize, NeonBook, publish
from sniper.neon_sources import Research
from rug_desk.trader_acceptance import key

NOW=1793229000

class NeonObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=Path(self.temp.name)
        initialize(self.app);self.book=NeonBook(self.app/'neon_bot')
    def tearDown(self):
        self.book.close();self.temp.cleanup()
    def events(self,kind):
        return [json.loads(r[0]) for r in self.book.db.execute('SELECT payload FROM observation_events WHERE kind=? ORDER BY seq',(kind,))]
    def test_real_call_boundary_is_observed_and_intake_is_unqualified(self):
        payload={'board':'solana','window':'30d','capturedAt':datetime.fromtimestamp(NOW,timezone.utc).isoformat(),
                 'data':[{'address':key(77),'handle':'replay_leader'}]}
        def transport(*args):
            self.assertEqual(self.events('source_observation')[-1]['status'],'RUNNING')
            return payload
        get=Mock(side_effect=transport);research=Research(self.book,self.app,transport=get,clock=lambda:NOW,rpc=Mock())
        with self.book.run('signals',now=NOW):research.refresh_board()
        observations=self.events('source_observation')
        self.assertEqual([r['status'] for r in observations],['RUNNING','OK'])
        self.assertTrue(all(r['run_id'] for r in observations))
        intake=self.events('candidate_intake');self.assertEqual(len(intake),1)
        self.assertFalse(intake[0]['qualified']);self.assertEqual(intake[0]['candidate_kind'],'trader_research')
        research.refresh_board();get.assert_called_once()
        self.assertEqual(len(self.events('candidate_intake')),1);self.assertEqual(self.book.fills(),[])
    def test_provider_failure_stays_safe_and_does_not_make_a_trade(self):
        secret='PRIVATE_ENDPOINT_TOKEN_NOT_FOR_UI'
        rpc=Mock();rpc.verify_chain.side_effect=ValueError(secret)
        research=Research(self.book,self.app,rpc=rpc,clock=lambda:NOW)
        with self.assertRaises(ValueError):research.observe([])
        rows=self.events('source_observation')
        self.assertEqual([r['status'] for r in rows],['RUNNING','ERROR'])
        self.assertEqual(self.book.db.execute("SELECT status FROM neon_sources WHERE name='rpc'").fetchone()[0],'ERROR')
        self.assertNotIn(secret,json.dumps(rows));self.assertEqual(self.book.fills(),[])
    def test_publication_records_pnl_and_only_changed_readiness(self):
        first=len(self.events('readiness_changed'));pnl=len(self.events('pnl_observation'))
        publish(self.book,self.app,NOW);middle=len(self.events('readiness_changed'))
        publish(self.book,self.app,NOW+1)
        self.assertEqual(len(self.events('readiness_changed')),middle)
        self.assertGreaterEqual(middle,first)
        self.assertGreater(len(self.events('pnl_observation')),pnl)
        self.assertEqual(self.book.fills(),[])

if __name__=='__main__':unittest.main()
