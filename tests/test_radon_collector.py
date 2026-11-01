import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from datetime import datetime, timezone

from sniper.radon.book import Book
from sniper.radon.collector import Collector, GENESIS
from sniper.radon.config import load, encode, policy_hash, digest
from sniper.radon.demo import address

ROOT = Path(__file__).resolve().parents[1]


def signature(n):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    return '1'*84+alphabet[n//58**3 % 58]+alphabet[n//58**2 % 58]+alphabet[n//58 % 58]+alphabet[n % 58]


class RadonCollectorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root, self.now, self.wallet = Path(temp.name), 1800000000, address(9)
        (self.root/'runtime').mkdir()
        (self.root/'runtime/solana-rpc-url.txt').write_text('https://example.invalid/private-credential')
        self.book = Book(self.root, load(ROOT/'radon_bot'), initialize=True, clock=lambda: self.now)
        self.addCleanup(self.book.close)
        self.settings = {'enabled': True, 'authorization_reference': 'Explicit fixture authorization',
            'binding': {'bot_id': 'radon', 'account_id': self.book.identity, 'policy_hash': policy_hash(self.book.cfg)}}
        (self.book.folder/'collector.json').write_text(encode(self.settings))
        self.calls = []

    def rows(self, numbers):
        return [{'signature': signature(n), 'slot': n, 'blockTime': self.now-1000+n,
            'err': None, 'confirmationStatus': 'finalized'} for n in numbers]

    def transport(self, method, url, body, headers, timeout):
        self.calls.append(body['method'] if body else 'leaderboard')
        if not body:
            return {'board': 'solana', 'window': '30d', 'capturedAt': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                'data': [{'address': self.wallet}]}
        if body['method'] == 'getGenesisHash':
            return {'result': GENESIS}
        if body['method'] == 'getSignaturesForAddress':
            return {'result': [] if body['params'][1].get('before') else self.rows([10])}
        self.assertEqual(body['method'], 'getTransaction')
        self.assertEqual(body['params'][1]['maxSupportedTransactionVersion'], 1)
        return {'result': {'version': 1, 'slot': 10, 'blockTime': self.now-990,
            'transaction': {'signatures': [signature(10)]}, 'meta': {'err': None}}}

    def make(self, transport=None):
        collector = Collector(self.book, clock=lambda: self.now, transport=transport or self.transport)
        self.addCleanup(collector.close)
        return collector

    def test_restarts_deduplicate_and_never_change_account_or_create_feed(self):
        before = {t: self.book.db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
            for t in ('fills', 'positions', 'inventory', 'events', 'shortlists')}
        first = self.make(); out = first.run(); first.close()
        second = self.make(); again = second.run()
        self.assertEqual(out['signature_count'], 1)
        self.assertEqual(again['signature_count'], 1)
        self.assertEqual(again['receipt_states'], {'UNSUPPORTED': 1})
        self.assertEqual(self.calls.count('getTransaction'), 1)
        self.assertFalse(again['archive_complete']); self.assertFalse(again['entry_ready'])
        self.assertLessEqual(out['requests_this_run'], 8)
        for table, count in before.items():
            self.assertEqual(self.book.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], count)
        self.assertFalse((self.book.folder/'source.json').exists())
        self.assertFalse((self.root/'neon_bot').exists())
        self.assertNotIn('private-credential', encode(again))

    def test_wrong_account_or_foreign_archive_is_rejected_before_mutation(self):
        collector = self.make(); collector.close()
        original = (self.book.folder/'chain.sqlite3').read_bytes()
        self.settings['binding']['account_id'] = 'different-account'
        (self.book.folder/'collector.json').write_text(encode(self.settings))
        with self.assertRaisesRegex(ValueError, 'authorization'):
            self.make()
        self.assertEqual((self.book.folder/'chain.sqlite3').read_bytes(), original)
        self.settings['binding']['account_id'] = self.book.identity
        (self.book.folder/'collector.json').write_text(encode(self.settings))
        collector = self.make()
        collector.set('binding', {'account_id': 'foreign'}); collector.db.commit(); collector.close()
        with self.assertRaisesRegex(ValueError, 'another account'):
            self.make()

    def test_failed_receipt_is_missing_not_complete_and_retries_are_bounded(self):
        def missing(method, url, **kw):
            if kw['body'] and kw['body']['method'] == 'getTransaction':
                return {'result': None}
            return self.transport(method, url, **kw)
        collector = self.make(missing)
        out = collector.run()
        self.assertEqual(out['receipt_states'], {'MISSING': 1})
        self.assertFalse(out['archive_complete'])
        self.assertEqual(collector.db.execute('SELECT attempts FROM signatures').fetchone()[0], 1)
        collector.run()
        self.assertEqual(collector.db.execute('SELECT attempts FROM signatures').fetchone()[0], 1)

    def test_head_pagination_keeps_gap_cursor_across_runs(self):
        collector = self.make()
        collector.db.execute('INSERT INTO candidates(wallet,payload,head,boundary_reached) VALUES(?,?,?,1)',
            (self.wallet, '{}', signature(1)))
        collector.db.commit()
        def rpc(method, params):
            self.assertEqual(method, 'getSignaturesForAddress')
            return self.rows(range(120, 20, -1)) if not params[1].get('before') else self.rows(range(20, 0, -1))
        collector.rpc = rpc
        collector.poll()
        state = collector.db.execute('SELECT * FROM candidates').fetchone()
        self.assertEqual(state['head'], signature(1)); self.assertEqual(state['head_before'], signature(21))
        collector.poll()
        state = collector.db.execute('SELECT * FROM candidates').fetchone()
        self.assertEqual(state['head'], signature(120)); self.assertIsNone(state['head_before'])
        self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM signatures').fetchone()[0], 120)

    def test_only_read_methods_and_mainnet_and_redacted_errors(self):
        collector = self.make()
        with self.assertRaisesRegex(ValueError, 'read-only'):
            collector.rpc('sendTransaction', [])
        self.assertEqual(self.calls, [])
        collector.transport = lambda *a, **k: {'result': 'wrong-chain'}
        self.assertEqual(collector.run()['state'], 'ERROR')
        self.assertEqual(collector.summary('ERROR')['signature_count'], 0)
        self.now += 61
        def broken(*a, **k):
            raise OSError('https://example.invalid/private-credential')
        collector.transport = broken
        out = collector.run()
        self.assertEqual(out['state'], 'ERROR'); self.assertNotIn('private-credential', encode(out))

    def test_discovery_pauses_to_drain_receipts_without_dropping_cursors(self):
        collector = self.make()
        collector.set('verified_endpoint', digest(collector.rpc_url))
        collector.set('candidates_at', self.now)
        collector.db.execute('INSERT INTO candidates(wallet,payload,head_before) VALUES(?,?,?)',
            (self.wallet, '{}', signature(1001)))
        collector.db.executemany('INSERT INTO signatures(wallet,signature,slot,at,payload) VALUES(?,?,?,?,?)',
            [(self.wallet, signature(n), n, self.now-1, '{}') for n in range(1000)])
        collector.db.commit()
        collector.poll = Mock(side_effect=AssertionError('Discovery must pause at the pending bound'))
        collector.receipts = Mock()
        out = collector.run()
        collector.poll.assert_not_called(); collector.receipts.assert_called_once()
        self.assertEqual(out['state'], 'COLLECTING_HISTORY')
        self.assertEqual(collector.db.execute('SELECT head_before FROM candidates').fetchone()[0], signature(1001))


if __name__ == '__main__':
    unittest.main()
