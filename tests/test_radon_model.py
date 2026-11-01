import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.radon.book import Book
from sniper.radon.config import load, encode
from sniper.radon.model import Model

ROOT=Path(__file__).resolve().parents[1]


def response(text=None):
    url='https://x.com/fixture/status/12345'
    value={'verdict':'BUY','confidence':0.8,'why':'Exact contract mentioned '+url,'x_read':'Recent post '+url,'main_risk':'Speculative fixture'}
    return {'status':'completed','output':[
        {'type':'custom_tool_call','name':'x_keyword_search','status':'completed'},
        {'type':'message','content':[{'type':'output_text','text':text or json.dumps(value),'annotations':[{'type':'url_citation','url':url}]}]}],
        'usage':{'input_tokens':100,'output_tokens':100,'total_tokens':200,'cost_in_usd_ticks':10000000,
                 'server_side_tool_usage_details':{'x_posts_fetched':2,'x_users_fetched':1}}}


class RadonModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1800000000
        self.book=Book(self.root,load(ROOT/'radon_bot'),initialize=True,clock=lambda:self.now);self.addCleanup(self.book.close)
        self.model=Model(self.root,self.book,clock=lambda:self.now,transport=lambda *args:response())
        self.patchers=[patch.object(self.model,'key',return_value='secret-fixture-never-journal'),patch.object(self.model,'gate',return_value={'enabled':True})]
        for p in self.patchers:p.start();self.addCleanup(p.stop)

    def test_account_bound_response_and_both_budget_journals(self):
        result=self.model.ask('event-1',{'mint':'fixture'})
        self.assertEqual(result['account_id'],self.book.identity);self.assertEqual(result['event_id'],'event-1')
        self.assertTrue(result['request_id'].startswith('radon:x:'))
        self.assertTrue((self.book.folder/'ai-budget.sqlite3').exists())
        self.assertTrue((self.root/'rug_desk/runtime/ai-budget.sqlite3').exists())
        raw=self.book.db.execute('SELECT payload FROM api_requests').fetchone()[0]
        self.assertNotIn('secret-fixture-never-journal',raw)
        self.assertEqual(json.loads(raw)['state'],'RECEIVED')

    def test_invalid_answer_retains_billed_usage(self):
        self.model.transport=lambda *args:response('not JSON')
        with self.assertRaises(ValueError):self.model.ask('event-1',{})
        audit=json.loads(self.book.db.execute('SELECT payload FROM api_requests').fetchone()[0])
        self.assertEqual(audit['state'],'ERROR');self.assertIsNotNone(audit['usage']['billed_usd'])

    def test_funded_proof_is_test_only_and_bound(self):
        self.model.transport=lambda *args:response('OK')
        result=self.model.test('inference')
        proof=json.loads((self.book.folder/'inference-test.json').read_text())
        self.assertEqual(result,proof);self.assertEqual(proof['binding']['account_id'],self.book.identity)
        self.assertTrue(proof['test_only']);self.assertEqual(self.book.db.execute('SELECT count(*) FROM fills').fetchone()[0],0)

    def test_daily_budget_bounds_requests_and_keeps_request_attribution(self):
        self.model.ask('one',{});self.model.ask('two',{})
        with self.assertRaises(ValueError):self.model.ask('three',{})
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM api_requests').fetchone()[0],2)


if __name__=='__main__':unittest.main()
