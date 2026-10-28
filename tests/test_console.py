"""Observation accuracy, isolation and HTTP boundary tests; entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
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

    def test_slow_view_does_not_block_other_views_and_same_view_loads_once(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Fixture was not released')
            return {'chart': 'complete'}
        with patch.object(self.data, '_inspect_jobs', return_value={'available': False, 'items': []}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                with patch.object(self.data, 'chart', side_effect=slow) as loader:
                    first = pool.submit(self.data.cached, 'slow-chart', 5, self.data.chart)
                    self.assertTrue(entered.wait(2))
                    second = pool.submit(self.data.cached, 'slow-chart', 5, self.data.chart)
                    try:
                        other = pool.submit(self.data.state, 'robinhood')
                        self.assertEqual(other.result(timeout=2)['account_id'], self.identity)
                        self.assertFalse(first.done())
                        self.assertFalse(second.done())
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=2), second.result(timeout=2))
                    loader.assert_called_once()

    def test_failed_cached_load_is_not_published_and_can_recover(self):
        with self.assertRaises(ValueError):
            self.data.cached('broken', 5, lambda: (_ for _ in ()).throw(ValueError('unavailable')))
        self.assertNotIn('broken', self.data._cache)
        self.assertEqual(self.data.cached('broken', 5, lambda: 'recovered'), 'recovered')

    def test_slow_task_inspection_is_single_flight_and_does_not_block_account_or_chart(self):
        entered, release = threading.Event(), threading.Event()
        def inspect():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Fixture was not released')
            return {'available': True, 'checked_at': NOW, 'items': [{'name': 'fixture-task'}], 'error': None}
        with patch.object(self.data, '_inspect_jobs', side_effect=inspect) as lookup:
            try:
                initial = self.data.jobs()
                self.assertFalse(initial['available'])
                self.assertTrue(initial['refreshing'])
                self.assertIsNone(initial['checked_at'])
                self.assertTrue(entered.wait(2))
                # Multiple HTTP handlers can ask for different caches while the
                # same task inspection is still blocked in its provider.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    state = pool.submit(self.data.state, 'robinhood')
                    chart = pool.submit(self.data.chart, 'robinhood', 'eip155:4663:token1', 'pool1')
                    repeated = [pool.submit(self.data.jobs) for _ in range(12)]
                    self.assertTrue(state.result(timeout=2)['jobs']['refreshing'])
                    self.assertEqual(len(chart.result(timeout=2)['fills']), 1)
                    self.assertTrue(all(item.result(timeout=2)['refreshing'] for item in repeated))
                lookup.assert_called_once()
            finally:
                release.set()
                self.data._jobs_thread.join(3)
            completed = self.data.jobs()
            self.assertTrue(completed['available'])
            self.assertFalse(completed['refreshing'])
            self.assertEqual(completed['checked_at'], NOW)
            self.assertIsNone(completed['error'])

    def test_failed_task_refresh_retains_last_success_and_backs_off(self):
        saved = {'available': True, 'checked_at': NOW, 'items': [{'name': 'last-confirmed-task'}], 'error': None}
        self.data._cache['jobs'] = (time.monotonic() - 31, saved)
        entered, release = threading.Event(), threading.Event()
        def inspect():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Fixture was not released')
            raise subprocess.TimeoutExpired('PRIVATE_COMMAND_SENTINEL', 12)
        with patch.object(self.data, '_inspect_jobs', side_effect=inspect) as lookup:
            try:
                refreshing = self.data.jobs()
                self.assertTrue(entered.wait(2))
                self.assertTrue(refreshing['refreshing'])
                self.assertEqual(refreshing['checked_at'], NOW)
                self.assertEqual(refreshing['items'], saved['items'])
            finally:
                release.set()
                self.data._jobs_thread.join(3)
            failed = self.data.jobs()
            self.assertTrue(failed['available'])
            self.assertFalse(failed['refreshing'])
            self.assertEqual(failed['checked_at'], NOW)
            self.assertEqual(failed['items'], saved['items'])
            self.assertEqual(failed['error'], 'task_inspection_timeout')
            self.assertIsInstance(failed['last_attempt_at'], int)
            self.assertNotIn('PRIVATE_', json.dumps(failed))
            for _ in range(12):
                self.data.jobs()
            lookup.assert_called_once()

    def test_task_refresh_recovery_replaces_old_results_and_clears_error(self):
        self.data._cache['jobs'] = (time.monotonic() - 31, {
            'available': True, 'checked_at': NOW, 'items': [{'name': 'old'}],
            'error': 'task_inspection_timeout', 'last_attempt_at': NOW + 1})
        fresh = {'available': True, 'checked_at': NOW + 30, 'items': [{'name': 'new'}], 'error': None}
        with patch.object(self.data, '_inspect_jobs', return_value=fresh) as lookup:
            self.data.jobs()
            self.data._jobs_thread.join(3)
            recovered = self.data.jobs()
        lookup.assert_called_once()
        self.assertFalse(recovered['refreshing'])
        self.assertIsNone(recovered['error'])
        self.assertEqual(recovered['checked_at'], NOW + 30)
        self.assertEqual(recovered['items'], [{'name': 'new'}])

    def wallet_exports(self):
        folder = self.root / 'runtime/research'
        (folder / 'addresses').mkdir(parents=True)
        wallet = '0x' + '1' * 40
        summary = {'wallet':wallet,'research_candidate':True,'evidence_count':203,
            'current_source_checked_observations':201,'capture_only_observations':1,
            'invalidated_observations':1,'roles':{'funded_recipient':202,'transaction_sender':1},
            'evidence_status':'indexed_observations','qualified':False,'history_complete':False,
            'performance_qualified':False,'first_observed_at':NOW,'last_observed_at':NOW+2,
            'json_path':'PRIVATE_FILE_SENTINEL','private_key':'PRIVATE_KEY_SENTINEL'}
        index = {'schema_version':1,'mode':'RESEARCH_ONLY','generated_at':NOW+3,
            'address_count':1,'candidate_count':1,'observation_count':203,
            'source_checked_observations':201,'capture_only_observations':1,
            'invalidated_observations':1,'qualified_count':0,'addresses':[summary],
            'csv_file':'PRIVATE_FILE_SENTINEL','rpc_url':'PRIVATE_ENDPOINT_SENTINEL'}
        observation = {'wallet':wallet,'role':'funded_recipient','tx_hash':'0x'+'2'*64,
            'block_number':123,'observed_at':NOW,'pool':'0x'+'3'*40,'token':'0x'+'4'*40,
            'canonical':True,'canonical_scope':'source_journal_current','raw_evidence_available':True,
            'canonical_checked_at':NOW+1,'quote_address':'0x'+'5'*40,'amount_raw':'1000000',
            'spent_raw':'25000000','spent_usd':'25','transaction_sender':'0x'+'6'*40,
            'base_payers':['0x'+'7'*40],
            'source_proof_scope':'funded pool buy and net recipient acquisition; funding ownership unestablished',
            'source_spend_basis':'pool base input, not proven personal wallet expenditure',
            'proof_digest':'a'*64,'receipt':{'private':'PRIVATE_RAW_SENTINEL'}}
        detail = {'schema_version':1,'mode':'RESEARCH_ONLY','generated_at':NOW+3,**summary,
            'history':{'history_complete':False,'qualified':False,'assessment':'NOT_ASSESSED',
                'net_pnl_after_follower_stress_usd':None,'qualification_blockers':['history_incomplete'],
                'controller_review_current':False,'qualification_checked_at':NOW+3,
                'history_path':'PRIVATE_HISTORY_SENTINEL'},
            'observations':[observation]*202+[dict(observation,spent_usd=None,spent_raw=None,amount_raw=None)],
            'raw_proof_location':'PRIVATE_RAW_LOCATION'}
        (folder / 'wallet-evidence-index.json').write_text(json.dumps(index),encoding='utf-8')
        (folder / 'addresses' / (wallet+'.json')).write_text(json.dumps(detail),encoding='utf-8')
        (folder / 'wallet-evidence.csv').write_text('wallet,role\n'+wallet+',funded_recipient\n',encoding='utf-8')
        return wallet, folder

    def test_discovery_index_exposes_only_public_evidence_and_saved_qualification(self):
        self.wallet_exports()
        before = self.ledger.conn.total_changes
        result = self.data.wallets()
        self.assertTrue(result['available'])
        self.assertEqual(result['candidate_count'],1)
        self.assertEqual(result['source_checked_observations'],201)
        self.assertEqual(result['capture_only_observations'],1)
        self.assertFalse(result['addresses'][0]['qualified'])
        self.assertFalse(result['addresses'][0]['history_complete'])
        self.assertEqual(result['addresses'][0]['roles']['funded_recipient'],202)
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertEqual(before,self.ledger.conn.total_changes)

    def test_address_detail_is_bounded_and_does_not_expose_raw_proofs_or_paths(self):
        wallet,_ = self.wallet_exports()
        result = self.data.wallet_evidence(wallet)
        self.assertEqual(result['wallet'],wallet)
        self.assertEqual(result['evidence_count'],203)
        self.assertEqual(result['shown_observations'],200)
        self.assertEqual(len(result['observations']),200)
        self.assertEqual(result['observations'][0]['token'],'0x'+'4'*40)
        self.assertEqual(result['observations'][0]['canonical_scope'],'source_journal_current')
        self.assertEqual(result['observations'][0]['canonical_checked_at'],NOW+1)
        self.assertIsNone(result['observations'][0]['spent_usd'])
        self.assertIsNone(result['observations'][0]['spent_raw'])
        self.assertIsNone(result['observations'][0]['amount_raw'])
        proof=result['observations'][1]
        self.assertEqual(proof['quote_address'],'0x'+'5'*40)
        self.assertEqual(proof['amount_raw'],'1000000')
        self.assertEqual(proof['spent_raw'],'25000000')
        self.assertEqual(proof['spent_usd'],'25')
        self.assertEqual(proof['transaction_sender'],'0x'+'6'*40)
        self.assertEqual(proof['base_payers'],['0x'+'7'*40])
        self.assertIn('ownership unestablished',proof['source_proof_scope'])
        self.assertEqual(proof['source_spend_basis'],'pool base input, not proven personal wallet expenditure')
        self.assertIsNone(result['history']['net_pnl_after_follower_stress_usd'])
        self.assertNotIn('PRIVATE_',json.dumps(result))

    def test_missing_export_is_unavailable_and_address_identity_fails_closed(self):
        self.assertFalse(self.data.wallets()['available'])
        self.assertFalse((self.root/'runtime/research').exists())
        wallet,folder = self.wallet_exports()
        for invalid in ('../rpc-url','0x1234',wallet+'.json','0x'+'g'*40):
            with self.assertRaises(ValueError):self.data.wallet_evidence(invalid)
        path = folder/'addresses'/(wallet+'.json')
        item=json.loads(path.read_text());item['wallet']='0x'+'2'*40
        path.write_text(json.dumps(item))
        with self.assertRaises(ValueError):self.data.wallet_evidence(wallet)

    def test_wallet_http_routes_allow_only_fixed_public_exports(self):
        wallet,_ = self.wallet_exports()
        private=self.root/'runtime/rpc-url.txt';private.write_text('PRIVATE_ENDPOINT_SENTINEL')
        server=make_server(self.root,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(path,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                c.request('GET',path,headers=headers or {});r=c.getresponse();return r.status,r.read(),dict(r.getheaders())
            finally:c.close()
        try:
            code,body,headers=get('/api/wallets')
            self.assertEqual(code,200);self.assertNotIn(b'PRIVATE_',body)
            code,body,headers=get('/api/wallet-evidence?wallet='+wallet+'&download=1')
            self.assertEqual(code,200);self.assertNotIn(b'PRIVATE_',body)
            self.assertEqual(headers['Content-Disposition'],'attachment; filename="'+wallet+'.json"')
            code,body,headers=get('/api/wallet-export.csv?path=runtime/rpc-url.txt')
            self.assertEqual(code,200);self.assertIn(wallet.encode(),body)
            self.assertNotIn(b'PRIVATE_',body)
            self.assertEqual(headers['Content-Disposition'],'attachment; filename="wallet-evidence.csv"')
            self.assertEqual(get('/api/wallet-evidence?wallet=..%2Frpc-url')[0],409)
            self.assertEqual(get('/api/wallets',{'Origin':'https://foreign.example'})[0],403)
            self.assertEqual(get('/runtime/research/wallet-evidence-index.json')[0],404)
            self.assertEqual(get('/api/wallet-evidence?wallet='+wallet,{'Sec-Fetch-Site':'cross-site'})[0],403)
        finally:
            server.shutdown();server.server_close();thread.join(5)
