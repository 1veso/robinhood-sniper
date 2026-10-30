"""Helium's cockpit exposes its recorded paper account through read-only HTTP."""
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sniper.console import ConsoleData, make_server
from sniper.helium.config import load
from sniper.helium.demo import FictionalGrok, FictionalProviders, FictionalReviewer, address
from sniper.helium.paper import Paper
from sniper.helium.service import scan


ROOT = Path(__file__).resolve().parents[1]


class HeliumCockpitConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = int(time.time())
        self.clock = lambda: self.now
        self.cfg = load(ROOT / 'helium_bot')

    def book(self, demo=True):
        book = Paper(self.root / 'helium_bot/runtime', self.cfg,
                     initialize=True, demo=demo, clock=self.clock)
        self.addCleanup(book.close)
        return book

    def server(self):
        server = make_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(close)

        def request(path, method='GET', headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request(method, path, headers=headers or {})
                response = connection.getresponse()
                body = response.read()
                if response.getheader('Content-Type', '').startswith('application/json'):
                    body = json.loads(body)
                return response.status, body
            finally:
                connection.close()

        return request

    def test_http_observations_match_committed_account_and_cannot_mutate_it(self):
        book = self.book()
        self.assertEqual(scan(self.root, book, FictionalProviders(self.clock),
                              FictionalGrok(book, self.clock), FictionalReviewer(book, self.clock),
                              self.clock), [])
        before = book.db.total_changes
        request = self.server()
        with patch.object(ConsoleData, 'jobs', return_value={'available': False, 'items': []}):
            code, state = request('/api/state?bot=helium')
        self.assertEqual(code, 200)
        self.assertEqual(state['account_id'], book.identity)
        self.assertTrue(state['read_only'])
        self.assertFalse(state['real_money_enabled'])
        self.assertIn('helium_workspace', state)
        code, page = request('/api/helium-journal?limit=500')
        self.assertEqual(code, 200)
        self.assertEqual(page['account_id'], book.identity)
        self.assertEqual(page['event_count'], len(page['events']))
        self.assertTrue(page['events'])
        code, detail = request('/api/helium-candidate?mint=' + address(1))
        self.assertEqual(code, 200)
        self.assertEqual(detail['account_id'], book.identity)
        self.assertEqual(detail['mint'], address(1))
        self.assertEqual(request('/api/helium-journal?after=' + page['cursor'])[1]['events'], [])
        self.assertEqual(request('/api/helium-journal?limit=501')[0], 409)
        self.assertEqual(request('/api/helium-journal?stage=madeup')[0], 409)
        self.assertEqual(request('/api/helium-journal?after=another-account:0')[0], 409)
        self.assertEqual(request('/api/helium-candidate?mint=../runtime')[0], 409)
        self.assertEqual(request('/api/helium-journal', 'POST')[0], 501)
        self.assertEqual(request('/api/helium-journal', headers={'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(request('/helium_bot/runtime/book.sqlite3')[0], 404)
        self.assertEqual(request('/helium_bot/runtime/paid-ai.json')[0], 404)
        self.assertEqual(before, book.db.total_changes)

    def test_missing_account_and_assets_do_not_initialize_trading(self):
        request = self.server()
        with patch.object(ConsoleData, 'jobs', return_value={'available': False, 'items': []}):
            code, state = request('/api/state?bot=helium')
        self.assertEqual(code, 200)
        self.assertFalse(state['initialized'])
        self.assertEqual(request('/helium.js')[0], 200)
        code, style = request('/helium.css')
        self.assertEqual(code, 200)
        self.assertIn(b'helium', style)
        self.assertFalse((self.root / 'helium_bot').exists())

    def test_next_runs_require_fresh_schedule_bound_to_this_root(self):
        self.book(demo=False)
        future = datetime.fromtimestamp(self.now + 45, timezone.utc).isoformat()
        jobs = {'available': True, 'checked_at': self.now, 'items': [
            {'name': 'HELIUM Paper Scan', 'next_run': future, 'account_root_bound': True},
            {'name': 'HELIUM Paper Watch', 'next_run': future, 'account_root_bound': False},
            {'name': 'NEON Paper Watch', 'next_run': future, 'account_root_bound': True},
        ]}
        with patch.object(ConsoleData, 'jobs', return_value=jobs):
            state = ConsoleData(self.root).state('helium')
        worker = state['helium_workspace']['worker']
        self.assertEqual(worker['next_scan_at'], self.now + 45)
        self.assertIsNone(worker.get('next_watch_at'))
        jobs['checked_at'] = self.now - 91
        with patch.object(ConsoleData, 'jobs', return_value=jobs):
            stale = ConsoleData(self.root).state('helium')['helium_workspace']['worker']
        self.assertIsNone(stale.get('next_scan_at'))
        self.assertIsNone(stale.get('next_watch_at'))


if __name__ == '__main__':
    unittest.main()
