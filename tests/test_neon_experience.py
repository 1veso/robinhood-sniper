"""The UI replay is isolated and exercises the unchanged paper engine."""
import json
from pathlib import Path
import tempfile
import unittest

from verify_neon_experience import prepare, run_stage, verify, STAGES
from sniper.solana_console import view, events

NOW = 1793229000


class NeonExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'replay'

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_replay_is_costed_durable_and_has_no_phantom_entry_or_exit(self):
        initial = prepare(self.root, now=NOW)
        start = view(self.root, 'neon', NOW)['event_stream']['cursor']
        for index, name in enumerate(STAGES):
            out = run_stage(self.root, name, clock=lambda i=index: NOW + i + 1)
            self.assertEqual(out['account_id'], initial['account_id'])
            self.assertTrue(out['excluded_from_strategy'])
            if name == 'failed_refresh':
                self.assertEqual(out['result']['verdict'], 'BUY')
                self.assertFalse(out['result']['entry'])
                self.assertEqual(view(self.root, 'neon', NOW + index + 1)['fills'], [])
            if name == 'missing_quote':
                state = view(self.root, 'neon', NOW + index + 1)
                self.assertEqual(state['snapshot']['open_positions'], 1)
                self.assertEqual(state['snapshot']['unresolved_positions'], 1)
                self.assertEqual(len(state['fills']), 1)
        result = verify(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['network_calls'], 0)
        self.assertEqual(len(result['fills']), 3)
        page = events(self.root, 'neon', start, 500)
        ids = [e['id'] for e in page['events']]
        self.assertEqual(len(ids), len(set(ids)))
        repeated = run_stage(self.root, 'entry', clock=lambda: NOW + 30)
        self.assertTrue(repeated['already_completed'])
        self.assertEqual(events(self.root, 'neon', page['cursor'])['events'], [])
        state = view(self.root, 'neon', NOW + 30)
        self.assertEqual(state['snapshot']['open_positions'], 0)
        self.assertEqual(state['fill_count'], 3)
        self.assertFalse(state['paid_ai_enabled'])

    def test_refuses_existing_accounts_bad_identity_and_out_of_order(self):
        prepare(self.root, now=NOW)
        with self.assertRaises(ValueError):
            prepare(self.root, now=NOW)
        with self.assertRaises(ValueError):
            run_stage(self.root, 'partial', clock=lambda: NOW + 1)
        marker = json.loads((self.root / 'REPLAY.json').read_text())
        marker['account_id'] = 'another-account'
        (self.root / 'REPLAY.json').write_text(json.dumps(marker))
        with self.assertRaises(ValueError):
            run_stage(self.root, 'reject', clock=lambda: NOW + 1)


if __name__ == '__main__':
    unittest.main()
