"""Optional comparison integration, entirely isolated with provider guards."""
from contextlib import ExitStack, contextmanager
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import explore as worker
from sniper.ledger import Ledger
from sniper.models import SafetyEvidence, WalletConfirmation
from sniper.paper_comparison import Comparison, initialize
from test_engine import NOW, quote
from test_paper_reentry import eligible

TOKEN = '0x' + '11' * 20
POOL = '0x' + '22' * 20
ASSET = 'eip155:4663/erc20:' + TOKEN


class ComparisonWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'exploration'; self.folder.mkdir()
        self.comparison_folder = self.root / 'paper-comparison'
        self.policy, self.rules, _ = worker.profile_settings('two-wallet-30k-reentry')
        ledger, store, _, self.trial = worker.open_trial(self.folder, self.policy, start=True,
                                                       now=NOW-60, rules=self.rules)
        ledger.close(); store.close()
        self.tick, self.price = [NOW], D(1)
        self.pending = [{'id': 'observed-source-candidate'}]
        self.routes, self.collections, self.notifications = [], [], []
        self.before_route = None

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self):
        return initialize(self.comparison_folder, self.folder / 'ledger.sqlite3',
                          self.policy, self.trial, now=self.tick[0])

    def open_comparison(self):
        return Comparison(self.comparison_folder, source_account_id=self.trial['account_id'],
                          source_id=self.trial['source_id'], policy_version=self.policy.version)

    def source_image(self):
        conn = sqlite3.connect((self.folder / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            return {name: conn.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
                    for name in ('metadata', 'positions', 'fills', 'closed_positions', 'decisions',
                                 'daily_equity', 'equity_history', 'events')}
        finally:
            conn.close()

    @contextmanager
    def offline(self):
        owner = self
        class Source:
            def __init__(self, *args): pass
            def collect(self, **kwargs):
                owner.collections.append(owner.tick[0])
                return {'sampled': len(owner.pending)}
            def pending(self): return owner.pending
            def finish(self, *args): pass
            def prove(self, swap, *, fallback_rpc=None):
                return {'token_address': TOKEN, 'pool': POOL, 'spent_usd': '100',
                        'wallet': 'wallet-a', 'tx_hash': 'observed-tx-a', 'observed_at': owner.tick[0]}
        class Market:
            def __init__(self, **kwargs): self.transport = None
        class Route:
            def __init__(self, *args): self.last_evidence = {}
            def quote(self, token, pool, **kwargs):
                if owner.before_route: owner.before_route(token, pool, kwargs)
                owner.routes.append({'token': token, 'pool': pool, **kwargs})
                amount = D(kwargs['max_notional'])
                holding = kwargs.get('sell_quantity', D(0))
                qty = holding if kwargs.get('exit_only') else amount / owner.price
                self.last_evidence = {'scope': 'isolated mocked provider', 'buy_input_usd': str(amount)}
                return quote(asset='eip155:4663/erc20:' + token, now=owner.tick[0], price=owner.price,
                    pool_id=pool, route_max_notional_usd=qty * owner.price,
                    route_max_sell_quantity=qty, route_impact_bps=D(1),
                    route_side='sell' if kwargs.get('exit_only') else 'both')
        class Book:
            def __init__(self, *args): pass
            def watched_pools(self, *args): return []
            def record(self, *args): pass
            def recent(self, *args, **kwargs): return ['a', 'b']
            def confirm(self, *args):
                return [WalletConfirmation('wallet-'+x, 'unreviewed-'+x, 'observed-tx-'+x,
                        owner.tick[0], D(100), True, False) for x in ('a', 'b')]
        def safety(_):
            return SafetyEvidence(ASSET, owner.tick[0], {k: True for k in owner.policy.required_safety_checks},
                'isolated-provider-fixture', {'rug_screen': {'status': 'PASS'}, 'report_pool_id': POOL})
        def notified(*args):
            owner.notifications.append(owner.tick[0]); return {'sent': 0, 'fixture': True}
        with ExitStack() as stack:
            patches = [(worker.time, 'time', lambda: owner.tick[0]),
                       (worker, 'ExploratorySource', Source), (worker, 'DexScreenerClient', Market),
                       (worker, 'V3RouteClient', Route), (worker, 'ConfirmationBook', Book),
                       (worker, 'signal_id', lambda swap: swap['id']),
                       (worker.GoPlusClient, 'screen', Mock(side_effect=safety)),
                       (worker, 'pool_adjusted_screen', lambda s, *a: s),
                       (worker, 'observed_pool_screen', lambda s, *a: s),
                       (worker, 'observe_creator_balance', lambda s, *a: s),
                       (worker, 'supplement_from_runtime', lambda s, *a: s),
                       (worker, 'enhance_rug_screen', lambda s: s),
                       (worker, 'notify_fills', notified), (worker, 'notify_market', notified)]
            for obj, name, value in patches: stack.enter_context(patch.object(obj, name, value))
            network = stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('No provider calls')))
            yield
            network.assert_not_called()

    def run_cycle(self):
        return worker.run_cycle(self.folder, self.root, profile='two-wallet-30k-reentry')

    def test_unconfigured_run_does_not_initialize_or_open_comparison(self):
        with self.offline(), patch.object(worker, 'Comparison', side_effect=AssertionError('Must remain disabled')) as factory:
            result = self.run_cycle()
        factory.assert_not_called()
        self.assertEqual(len(result['fills']), 1)
        self.assertNotIn('comparison', result)
        self.assertFalse(self.comparison_folder.exists())

    def test_source_candidates_and_watches_publish_separate_complete_results(self):
        manifest = self.initialize()
        before = self.source_image()
        with self.offline(): result = self.run_cycle()
        self.assertEqual(result['health'], 'OK', result['errors'])
        self.assertEqual(result['comparison']['state'], 'OBSERVING')
        self.assertTrue(result['comparison']['excluded_from_strategy'])
        self.assertEqual(len(result['fills']), 1)
        source = self.source_image()
        self.assertEqual(len(source['fills']), len(before['fills']) + 1)
        self.assertEqual(dict(source['metadata'])['account_id'], self.trial['account_id'])
        self.assertNotIn('paper_comparison', dict(source['metadata']))
        comparison = self.open_comparison()
        try:
            rows = comparison.conn.execute('SELECT seq,kind,observed_at,payload FROM observations ORDER BY seq').fetchall()
            self.assertEqual([r['seq'] for r in rows], list(range(1, len(rows)+1)))
            self.assertEqual(sum(r['kind'] == 'candidate' for r in rows), 1)
            self.assertGreaterEqual(sum(r['kind'] == 'watch' for r in rows), 3)
            candidate = json.loads(next(r['payload'] for r in rows if r['kind'] == 'candidate'))
            self.assertEqual(candidate['candidate']['event_id'], 'observed-source-candidate')
            self.assertLess(D(candidate['quote']['route_max_notional_usd']), 5)
            for name, (ledger, _) in comparison.portfolios().items():
                self.assertEqual(ledger._get('account_id'), manifest['accounts'][name])
                self.assertNotEqual(ledger._get('account_id'), self.trial['account_id'])
                self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)
                self.assertEqual(int(ledger._get('comparison_cursor')), len(rows))
            comparison.process(); comparison.publish(self.tick[0])
            self.assertEqual(self.source_image(), source)
        finally:
            comparison.close()
        report = json.loads((self.comparison_folder / 'report.json').read_text())
        self.assertEqual(report['entry_deadline'], self.trial['ends_at'])
        self.assertFalse(report['past_results_reconstructed'])
        self.assertFalse(report['profitability_claim'])
        self.assertTrue(all(v['backlog'] == 0 for v in report['variants'].values()))
        self.tick[0] += 120
        with self.offline(): restarted = self.run_cycle()
        self.assertEqual(len(restarted['fills']), 1)
        self.assertEqual(restarted['comparison']['state'], 'OBSERVING')

    def test_capture_failure_does_not_prevent_primary_entry_watch_or_publication(self):
        self.initialize()
        with self.offline(), patch.object(Comparison, 'capture', side_effect=OSError('fixture journal unavailable')):
            result = self.run_cycle()
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['snapshot']['unresolved_positions'], 0)
        self.assertEqual(result['comparison']['state'], 'ERROR')
        self.assertIn('Paper comparison observation gap: OSError', result['errors'])
        published = json.loads((self.folder / 'status.json').read_text())
        self.assertEqual(published['finished_at'], self.tick[0])
        self.assertEqual(len(published['fills']), 1)
        self.assertTrue((self.folder / 'report.html').is_file())

    def test_processing_failure_occurs_after_original_stop_and_primary_publication(self):
        with self.offline(): self.run_cycle()
        self.initialize()
        self.tick[0] += 120; self.price = D('.5'); self.pending = []
        def fail_processing(*args):
            already = json.loads((self.folder / 'status.json').read_text())
            self.assertEqual(already['finished_at'], self.tick[0])
            self.assertEqual(already['fills'][-1]['reason'], 'stop_loss')
            self.assertEqual(already['snapshot']['open_positions'], 0)
            self.assertEqual(self.notifications[-2:], [self.tick[0], self.tick[0]])
            raise RuntimeError('fixture interrupted comparison')
        with self.offline(), patch.object(Comparison, 'process', side_effect=fail_processing):
            result = self.run_cycle()
        self.assertEqual(result['comparison']['state'], 'ERROR')
        self.assertEqual(len(result['fills']), 2)
        self.assertEqual(result['fills'][-1]['reason'], 'stop_loss')
        self.assertEqual(result['snapshot']['closed_positions'], 1)

    def test_capture_gap_survives_restart_and_is_journaled_before_new_inputs(self):
        self.initialize()
        with self.offline(), patch.object(Comparison, 'capture', side_effect=OSError('temporary lock')):
            failed = self.run_cycle()
        self.assertEqual(len(failed['fills']), 1)
        marker = self.comparison_folder / 'pending-gap.json'
        self.assertTrue(marker.is_file())
        gap = json.loads(marker.read_text())
        self.assertEqual(gap['reason'], 'comparison_capture_failure')
        self.assertEqual(gap['from'], NOW)
        self.tick[0] += 120
        with self.offline(): recovered = self.run_cycle()
        self.assertEqual(recovered['comparison']['state'], 'OBSERVING', recovered['errors'])
        self.assertEqual(len(recovered['fills']), 1)
        self.assertFalse(marker.exists())
        comparison = self.open_comparison()
        try:
            rows = comparison.conn.execute('SELECT * FROM observations ORDER BY seq').fetchall()
            self.assertEqual(rows[0]['kind'], 'gap')
            recovered_gap = json.loads(rows[0]['payload'])
            self.assertEqual(recovered_gap['reason'], 'comparison_capture_failure')
            self.assertEqual(recovered_gap['recovered_at'], self.tick[0])
            self.assertFalse(any(r['kind'] == 'candidate' for r in rows))
            for ledger, _ in comparison.portfolios().values():
                self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
                self.assertTrue(ledger.conn.execute("SELECT 1 FROM events WHERE kind='comparison_gap'").fetchone())
        finally:
            comparison.close()

    def test_invalid_comparison_binding_does_not_block_primary_worker(self):
        manifest = self.initialize()
        manifest['source_account_id'] = 'different-account'
        (self.comparison_folder / 'manifest.json').write_text(json.dumps(manifest))
        with self.offline(): result = self.run_cycle()
        self.assertEqual(len(result['fills']), 1)
        self.assertIn('Paper comparison unavailable: ValueError', result['errors'])
        self.assertEqual(result['finished_at'], self.tick[0])
        self.assertTrue((self.folder / 'report.html').exists())

    def test_shared_original_deadline_stops_entries_and_allows_original_exits(self):
        self.initialize()
        with self.offline(): first = self.run_cycle()
        original_collections = len(self.collections)
        self.tick[0] = self.trial['ends_at'] + 1
        self.price = D('.5')
        with self.offline(): result = self.run_cycle()
        self.assertEqual(len(self.collections), original_collections)
        self.assertEqual(result['trial']['ends_at'], first['trial']['ends_at'])
        self.assertEqual(result['trial_state'], 'COMPLETE')
        self.assertEqual(len(result['fills']), 2)
        self.assertEqual(result['fills'][-1]['reason'], 'stop_loss')
        comparison = self.open_comparison()
        try:
            self.assertEqual(comparison.manifest['entry_deadline'], self.trial['ends_at'])
            for ledger, _ in comparison.portfolios().values():
                self.assertEqual(ledger.snapshot()['open_positions'], 0)
                self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 2)
        finally:
            comparison.close()

    def test_secondary_quotes_are_limited_rotated_and_after_primary_publication(self):
        self.initialize()
        comparison = self.open_comparison()
        try:
            for i in range(3):
                asset = 'eip155:4663/erc20:0x' + f'{i+50:040x}'
                now = NOW + i + 1
                c = eligible('experimental-'+str(i), asset, now)
                q = quote(asset, now, route_max_notional_usd=D(5),
                          route_max_sell_quantity=D(5), route_impact_bps=D(1))
                comparison.capture('experimental-'+str(i), 'candidate', now,
                                   {'candidate': c.as_dict(), 'quote': q.as_dict(), 'cycle': now})
        finally:
            comparison.close()
        self.tick[0] += 60; self.pending = []
        def primary_finished(token, pool, kwargs):
            primary = json.loads((self.folder / 'status.json').read_text())
            self.assertEqual(primary['finished_at'], self.tick[0])
            self.assertEqual(primary['fills'], [])
            self.assertTrue(kwargs['exit_only'])
            self.assertGreater(kwargs['sell_quantity'], 0)
            self.assertEqual(self.notifications[-2:], [self.tick[0], self.tick[0]])
        self.before_route = primary_finished
        with self.offline(): result = self.run_cycle()
        self.assertEqual(result['comparison']['state'], 'OBSERVING', result['errors'])
        self.assertEqual(len(self.routes), 2)
        first_tokens = {r['token'] for r in self.routes}
        self.tick[0] += 120; self.routes.clear()
        with self.offline(): second = self.run_cycle()
        self.assertEqual(second['comparison']['state'], 'OBSERVING', second['errors'])
        self.assertEqual(len(self.routes), 2)
        self.assertNotEqual({r['token'] for r in self.routes}, first_tokens)
        self.assertEqual(self.source_image()['fills'], [])


if __name__ == '__main__':
    unittest.main()
