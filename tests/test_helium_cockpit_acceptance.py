"""Worker-to-cockpit acceptance with real accounting and offline dependencies."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from verify_helium_cockpit import (prepare, run_stage, verify, STAGES,
                                   ReplayProviders)
from sniper.helium.config import encode, load
from sniper.helium.demo import FictionalReviewer
from sniper.helium.paper import Paper
from sniper.helium.service import scan, watch
from sniper.helium.view import snapshot, view
from sniper.krypton.demo import FictionalGrok, address, offline

NOW = 1793361600


class HeliumCockpitAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'replay'
        self.initial = prepare(self.root, now=NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, now=NOW):
        return Paper(self.root / 'helium_bot/runtime', load(self.root / 'helium_bot'),
                     demo=True, clock=lambda: now)

    def test_full_cycle_survives_restarts_and_maps_only_committed_trades(self):
        entry_history = None
        for name in STAGES:
            row = run_stage(self.root, name)
            self.assertEqual(row['account_id'], self.initial['account_id'])
            self.assertTrue(row['excluded_from_strategy'])
            state = json.loads((self.root / ('state-' + name + '.json')).read_text())
            self.assertEqual(state['helium_workspace']['data']['state'], 'replay')
            if name == 'entry':
                entry_history = state['equity_history']
                self.assertEqual(len(state['positions']), 1)
                self.assertEqual(state['positions'][0]['mark']['quote']['price'], '1')
            if name == 'mark':
                self.assertEqual(state['positions'][0]['mark']['quote']['price'], '1.10')
            if name == 'missing_quote':
                self.assertEqual(len(state['fills']), 1)
                self.assertEqual(state['summary']['unresolved_positions'], 1)
            if name == 'recover':
                self.assertEqual(state['summary']['unresolved_positions'], 0)
                self.assertEqual(state['positions'][0]['mark']['quote']['price'], '1.08')
                guard = next(row for row in state['helium_workspace']['pipeline'] if row['id'] == 'guard')
                self.assertNotIn('unavailable', guard['reason'].lower())
            if name == 'close':
                guard = next(row for row in state['helium_workspace']['pipeline'] if row['id'] == 'guard')
                self.assertEqual(guard['reason'], 'take_profit')
        result = verify(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['network_calls'], 0)
        self.assertEqual(len(result['fills']), 2)
        final = json.loads((self.root / 'state-close.json').read_text())
        by_id = {row['id']: row for row in final['equity_history']}
        for old in entry_history:
            self.assertEqual(by_id[old['id']], old, 'Historical account observations must not be recomputed from today’s prices')
        repeated = run_stage(self.root, 'entry')
        self.assertTrue(repeated['already_completed'])
        self.assertEqual(verify(self.root)['fills'], result['fills'])

    def test_read_only_refreshes_cannot_replay_entries_or_alter_account(self):
        from sniper.helium.cockpit import journal
        for name in STAGES[:2]:
            run_stage(self.root, name)
        book = self.book(NOW + 2)
        try:
            before = list(book.db.execute('SELECT id,at,mint,side,payload FROM fills'))
            state = snapshot(book.db, NOW + 2)
            cursor = state['helium_workspace']['cursor']
            for _ in range(3):
                current = view(self.root, now=NOW + 2)
                self.assertEqual(current['account_id'], self.initial['account_id'])
                self.assertEqual(current['summary']['fill_count'], 1)
                page = journal(self.root, after=cursor)
                self.assertEqual(page.get('events', page.get('items')), [])
            self.assertEqual([tuple(r) for r in before], [tuple(r) for r in book.db.execute('SELECT id,at,mint,side,payload FROM fills')])
        finally:
            book.close()

    def test_disabled_models_block_new_entries_while_exit_worker_keeps_running(self):
        for name in STAGES[:2]:
            run_stage(self.root, name)
        now = NOW + 10
        book = self.book(now)
        class Disabled(FictionalGrok):
            def gate(self):
                return {'enabled': False, 'blockers': ['Paid Grok access disabled']}
            def ask(self, evidence):
                raise AssertionError('Disabled model cannot be called')
        try:
            with offline():
                providers = ReplayProviders(book, lambda: now, 'entry')
                providers.profiles = lambda: [address(5)]
                model = Disabled(book, lambda: now)
                reviewer = FictionalReviewer(book, lambda: now)
                self.assertEqual(scan(self.root, book, providers, model, reviewer, lambda: now), [])
                blocked = snapshot(book.db, now)
                self.assertFalse(blocked['status']['team_gate']['enabled'])
                self.assertIn('Paid Grok access disabled', blocked['status']['team_gate']['blockers'])
                self.assertEqual(blocked['summary']['fill_count'], 1)
                self.assertEqual(blocked['decision_counts']['BLOCKED'], 1)
                lanes = {row['id']: row for row in blocked['helium_workspace']['pipeline']}
                self.assertEqual(lanes['filter']['state'], 'passed')
                self.assertEqual(lanes['analyst']['state'], 'blocked')
                self.assertEqual(model.calls, 0)
                self.assertEqual(reviewer.calls, 0)
                providers = ReplayProviders(book, lambda: now + 1, 'close')
                self.assertEqual(watch(self.root, book, providers, lambda: now + 1), [])
                closed = snapshot(book.db, now + 1)
                self.assertFalse(closed['positions'])
                self.assertEqual(closed['summary']['fill_count'], 2)
                self.assertFalse(closed['status']['team_gate']['enabled'])
        finally:
            book.close()

    def test_unknown_model_invoice_stays_unknown_and_allowance_reduces_equity(self):
        run_stage(self.root, 'reject')
        book = self.book(NOW + 2)
        try:
            before = snapshot(book.db, NOW + 2)
            with book.transaction():
                book.db.execute('INSERT INTO api_requests VALUES(?,?)',
                    ('helium:grok:unbilled-fixture', encode({'state': 'RECEIVED', 'provider': 'grok',
                        'usage': {'billed_usd': None, 'estimated_usd': '0.25'}})))
            run_id = book.run_start('watch', NOW + 2)
            book.run_finish(run_id, NOW + 2, [])
            state = snapshot(book.db, NOW + 2)
            self.assertIsNone(state['summary']['net_pnl'])
            self.assertEqual(state['summary']['estimated_model_costs'], '0.25')
            self.assertEqual(float(state['summary']['equity']), float(before['summary']['equity']) - 0.25)
            self.assertEqual(state['summary']['paper_estimated_net_pnl'], '-0.25')
            self.assertIn('not provider invoices', state['summary']['model_cost_estimate_scope'])
            observed = state['equity_history'][-1]
            self.assertIsNone(observed['net_pnl'])
            self.assertEqual(observed['estimated_net_pnl'], '-0.25')
            self.assertEqual(observed['chart_equity'], '999.75')
        finally:
            book.close()

    def test_failure_before_commit_cannot_publish_a_phantom_fill(self):
        from sniper.helium.cockpit import observe
        run_stage(self.root, 'reject')
        now = NOW + 2
        book = self.book(now)

        def fail_after_fill(current, at, reason):
            if current.db.execute('SELECT count(*) FROM fills').fetchone()[0]:
                raise ValueError('Injected pre-commit observation failure')
            return observe(current, at, reason)

        try:
            with offline(), patch('sniper.helium.cockpit.observe', side_effect=fail_after_fill):
                errors = scan(self.root, book, ReplayProviders(book, lambda: now, 'entry'),
                              FictionalGrok(book, lambda: now), FictionalReviewer(book, lambda: now),
                              lambda: now)
            self.assertTrue(errors)
            state = snapshot(book.db, now)
            self.assertEqual(state['summary']['fill_count'], 0)
            self.assertEqual(state['summary']['cash'], '1000')
            self.assertFalse(state['positions'])
            self.assertFalse(any(event['kind'] == 'fill' for event in state['helium_workspace']['events']))
            self.assertEqual(state['decision_counts']['ERROR'], 1)
        finally:
            book.close()

    def test_replay_refuses_existing_root_wrong_identity_and_out_of_order_steps(self):
        with self.assertRaises(ValueError):
            prepare(self.root, now=NOW)
        with self.assertRaises(ValueError):
            run_stage(self.root, 'close')
        marker = json.loads((self.root / 'REPLAY.json').read_text())
        marker['account_id'] = 'different-account'
        (self.root / 'REPLAY.json').write_text(json.dumps(marker))
        with self.assertRaises(ValueError):
            run_stage(self.root, 'reject')

    def test_configuration_is_exact_inherited_baseline(self):
        source = Path(__file__).resolve().parents[1]
        self.assertEqual((source / 'helium_bot/config.json').read_bytes(),
                         (source / 'krypton_bot/config.json').read_bytes())
        self.assertEqual(hashlib.sha256((self.root / 'helium_bot/config.json').read_bytes()).digest(),
                         hashlib.sha256((source / 'helium_bot/config.json').read_bytes()).digest())


if __name__ == '__main__':
    unittest.main()
