"""Observation accuracy, isolation and HTTP boundary tests; entirely offline."""
from decimal import Decimal as D
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from sniper.console import ConsoleData, LedgerView, make_server, reader, queue
from sniper.engine import PaperEngine
from sniper.ledger import Ledger, json_text
from sniper.policy import RiskPolicy
from test_engine import candidate, quote, NOW


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'runtime/exploration'
        self.folder.mkdir(parents=True)
        self.ledger = Ledger(self.folder / 'ledger.sqlite3', D(1000))
        self.engine = PaperEngine(self.ledger, RiskPolicy())
        self.engine.consider(candidate(), quote(), NOW)
        self.engine.watch({'eip155:4663:token1': quote(now=NOW+1, price='1.5')}, NOW+1)
        self.identity = self.ledger._get('account_id')
        (self.folder / 'trial.json').write_text(json.dumps({'account_id': self.identity, 'started_at': NOW, 'ends_at': NOW+604800, 'policy_version': 'test'}))
        (self.folder / 'status.json').write_text(json.dumps({'finished_at': NOW+1,'started_at':NOW,'errors':[], 'snapshot': {'fill_model':'fixture'}}))
        self.data = ConsoleData(self.root)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_matches_engine_costs_and_exit_thresholds_without_writes(self):
        before = self.ledger.conn.total_changes
        expected = self.ledger.snapshot()
        actual = self.data.robinhood()
        for key in ('cash','equity','market_value','position_basis','realized_pnl','unrealized_pnl','fees_paid'):
            self.assertEqual(actual['snapshot'][key], expected[key])
        p = actual['snapshot']['positions'][0]
        self.assertEqual(p['stop_price'], p['entry_price'] * D('.65'))
        self.assertEqual(p['take_price'], p['entry_price'] * 2)
        self.assertEqual(before, self.ledger.conn.total_changes)

    def test_unresolved_position_is_not_a_fictional_exit(self):
        self.engine.watch({}, NOW+2)
        actual = self.data.robinhood()
        self.assertEqual(actual['snapshot']['open_positions'], 1)
        self.assertEqual(actual['snapshot']['unresolved_positions'], 1)
        self.assertEqual(actual['snapshot']['market_value'], 0)
        self.assertEqual(len(actual['fills']), 1)
        self.assertEqual(actual['snapshot']['positions'][0]['mark_status'], 'missing_quote')

    def test_read_connection_cannot_modify_or_initialize_accounts(self):
        with reader(self.folder / 'ledger.sqlite3') as c:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("UPDATE metadata SET value='0' WHERE key='cash'")
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            with reader(missing):
                pass
        self.assertFalse(missing.exists())

    def test_snapshot_persists_across_independent_connections(self):
        with reader(self.folder / 'ledger.sqlite3') as a:
            prior = LedgerView(a).snapshot()
            self.engine.watch({'eip155:4663:token1': quote(now=NOW+2,price='1.6')},NOW+2)
            self.assertEqual(LedgerView(a).snapshot(), prior)
        with reader(self.folder / 'ledger.sqlite3') as b:
            self.assertEqual(LedgerView(b).snapshot(), self.ledger.snapshot())
            self.assertNotEqual(LedgerView(b).snapshot()['equity'], prior['equity'])

    def test_isolation_unknown_bot_and_identity_fail_closed(self):
        with self.assertRaises(ValueError): self.data.state('../../acceptance')
        (self.folder / 'trial.json').write_text(json.dumps({'account_id':'other'}))
        with self.assertRaises(ValueError): self.data.robinhood()

    def test_chart_uses_quote_time_and_pinned_pool_and_excludes_raw_evidence(self):
        q = quote(now=NOW+3,price='2')
        self.ledger.record_event('quote-other-recorded-at','market_mark',q.as_dict(),NOW+8)
        self.ledger.record_event('other-pool','market_mark',quote(now=NOW+4,pool_id='elsewhere').as_dict(),NOW+4)
        actual = self.data.chart('robinhood','eip155:4663:token1','pool1')
        self.assertIn(NOW+3,[p['at'] for p in actual['points']])
        self.assertNotIn(NOW+8,[p['at'] for p in actual['points']])
        self.assertNotIn(NOW+4,[p['at'] for p in actual['points']])
        self.assertEqual(len(actual['fills']),1)
        self.assertEqual(self.data.chart('robinhood','eip155:4663:token1','elsewhere')['fills'],[])
        self.assertNotIn('source', actual['points'][0])

    def test_telegram_acknowledgements_do_not_expose_recipient_or_payload(self):
        path=self.root/'outbox.sqlite3'
        c=sqlite3.connect(path)
        c.execute('CREATE TABLE outbox(state,created_at,message_id,payload)')
        c.execute('INSERT INTO outbox VALUES (?,?,?,?)',('sent',100,24,'SECRET_RECIPIENT'))
        c.commit();c.close()
        q=queue(path)
        self.assertEqual(q['last_ack']['message_id'],24)
        self.assertNotIn('SECRET_RECIPIENT',json.dumps(q))
        self.assertFalse(queue(self.root/'missing.sqlite3')['available'])

    def test_http_boundary_and_errors_have_no_secrets(self):
        server=make_server(self.root,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(path,headers=None,method='GET'):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                c.request(method,path,headers=headers or {});r=c.getresponse();return r.status,r.read(),dict(r.getheaders())
            finally:c.close()
        try:
            for path in ['/runtime/rpc-url.txt','/../runtime/telegram-bot-token.txt','/%2e%2e/config.py']:
                self.assertEqual(get(path)[0],404)
            self.assertEqual(get('/api/health',{'Host':'evil.example'})[0],403)
            self.assertEqual(get('/api/health',{'Origin':'https://evil.example'})[0],403)
            self.assertEqual(get('/api/health',{'Sec-Fetch-Site':'cross-site'})[0],403)
            self.assertEqual(get('/api/state',method='POST')[0],501)
            code,body,headers=get('/api/health');self.assertEqual(code,200)
            self.assertNotIn('Access-Control-Allow-Origin',headers)
            self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
            with patch.object(ConsoleData,'jobs',return_value={'available':False,'items':[]}):
                self.assertEqual(get('/api/state')[0],200)
            self.assertEqual(get('/api/state?bot=acceptance')[0],409)
        finally:
            server.shutdown();server.server_close();thread.join(5)

    def test_public_bind_is_refused(self):
        with self.assertRaises(ValueError):make_server(self.root,host='0.0.0.0')
