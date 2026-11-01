"""Offline transport doubles: separate paid authority, shared caps and invoices."""
from contextlib import closing
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from sniper.nitrogen.book import Book
from sniper.nitrogen.config import load,encode
from sniper.nitrogen.model import Model,BudgetBlocked,reconcile
from sniper.nitrogen.demo import response

ROOT=Path(__file__).resolve().parents[1]


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.book=Book(self.root,load(ROOT),initialize=True,clock=lambda:1800000000);self.addCleanup(self.book.close)
        (self.root/'runtime/jev-review').mkdir(parents=True)
        (self.root/'runtime/jev-review/settings.json').write_text(encode({'enabled':False,'mode':'SHADOW','paid_authorized':False,'daily_budget_usd':'1'}))
        (self.root/'runtime/typesafe-api-key.txt').write_text('fixture-secret-only')
        self.calls=[]
        def transport(*args):self.calls.append(args);return response()
        self.model=Model(self.root,self.book,clock=lambda:1800000000,transport=transport)
        self.auth={'enabled':True,'operator_authorized':True,'approval_reference':'offline-fixture','binding':self.model.binding(),'daily_budget_usd':'1'}
        (self.book.folder/'paid-jev.json').write_text(encode(self.auth))
    def test_separate_binding_and_shared_caps(self):
        self.auth['binding']['account_id']='argon'
        (self.book.folder/'paid-jev.json').write_text(encode(self.auth))
        with self.assertRaises(PermissionError):self.model.ask('a',{})
        self.assertEqual(self.calls,[])
    def test_disabled_does_not_read_credential(self):
        (self.book.folder/'paid-jev.json').write_text('{"enabled":false}')
        with patch.object(self.model,'authorization',side_effect=PermissionError):
            with self.assertRaises(PermissionError):self.model.ask('a',{})
        self.assertEqual(self.calls,[])
    def test_shared_zero_ceiling_blocks(self):
        settings=json.loads((self.root/'runtime/jev-review/settings.json').read_text());settings['daily_budget_usd']='0'
        (self.root/'runtime/jev-review/settings.json').write_text(encode(settings))
        with self.assertRaises(PermissionError):self.model.ask('a',{})
        self.assertEqual(self.calls,[])
    def test_response_full_usage_attributed_and_secret_not_logged(self):
        r=self.model.ask('a',{'facts':'fixture'});self.assertEqual(r['response'],response());self.assertEqual(len(self.calls),1)
        audit=json.loads(self.book.db.execute('SELECT payload FROM api_requests').fetchone()[0])
        self.assertEqual(audit['account_id'],self.book.identity);self.assertIsNone(audit['usage']['billed_usd']);self.assertEqual(audit['reservation_usd'],'0.01')
        self.assertNotIn('fixture-secret-only',encode(audit))
        for path in (self.book.folder/'jev-budget.sqlite3',self.root/'runtime/jev-review/budget.sqlite3'):
            import sqlite3
            with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT account_id FROM advisory_request_accounts').fetchone()[0],self.book.identity)
        self.assertFalse((self.root/'rug_desk/runtime/ai-budget.sqlite3').exists())
        self.assertEqual(audit['budget_scope'],'JEV_ONLY')
    def test_no_request_replay(self):
        self.model.ask('a',{})
        with self.assertRaises(Exception):self.model.ask('a',{})
        self.assertEqual(len(self.calls),1)
    def test_budget_full_never_calls(self):
        with patch('sniper.nitrogen.model.DailyBudget.reserve',side_effect=ValueError):
            with self.assertRaises(BudgetBlocked):self.model.ask('a',{})
        self.assertEqual(self.calls,[])
    def test_malformed_response_and_timeout_keep_reservation(self):
        self.model.transport=lambda *a: {}
        with self.assertRaises(ValueError):self.model.ask('bad',{})
        audit=json.loads(self.book.db.execute('SELECT payload FROM api_requests').fetchone()[0]);self.assertEqual(audit['state'],'ERROR');self.assertEqual(audit['reservation_usd'],'0.01')
        self.assertFalse(self.model.gate()['enabled'])
    def test_provider_invoice_idempotent_and_bound(self):
        r=self.model.ask('a',{});invoice={'scope':'TYPESAFE_PROVIDER_INVOICE','account_id':self.book.identity,'request_id':r['request_id'],'provider_invoice_reference':'offline-invoice','billed_usd':'0.002'}
        self.assertEqual(reconcile(self.book,invoice),reconcile(self.book,invoice))
        self.assertEqual(str(self.book.model_costs()[0]),'0.002');self.assertEqual(str(self.book.model_costs()[1]),'0')
        with self.assertRaises(ValueError):reconcile(self.book,dict(invoice,billed_usd='0'))
        with self.assertRaises(ValueError):reconcile(self.book,dict(invoice,account_id='radon'))
