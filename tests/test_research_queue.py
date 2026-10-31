import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sniper.research_queue import HistoryQueue, DAY

OLD = {'block': 100, 'timestamp': 1000, 'hash': '0x' + '11' * 32, 'start_block': 1}
NEW = {'block': 200, 'timestamp': 1000 + 2 * DAY, 'hash': '0x' + '22' * 32, 'start_block': 2}


def wallet(i):
    return '0x' + f'{i:040x}'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class HistoryQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.candidates = {wallet(i): {'first_seen_at': i, 'last_seen_at': NEW['timestamp'] - 1,
                                     'observed_recipient': True} for i in range(1, 105)}

    def queue(self, now=NEW['timestamp']):
        return HistoryQueue(self.folder, self.candidates, OLD, now, write)

    def report(self, w, snapshot=OLD):
        report = {'wallet': w, 'cutoff': snapshot['timestamp'], 'history_complete': True,
                  'performance_qualified': True, 'qualified': False, 'qualification_blockers': []}
        write(self.folder / f"cohort-{snapshot['block']}" / f'wallet-{w}.json', report)
        return report

    def test_rotation_includes_candidates_beyond_legacy_first_100(self):
        write(self.folder / 'cohort-100/attempts.json', {wallet(i): 500 for i in range(1, 101)})
        self.assertEqual(self.queue().next_wallets()[0], wallet(101))
        self.assertLessEqual(len(self.queue().next_wallets()), 5)

    def test_resume_old_job_outside_first_100_without_new_snapshot(self):
        w = wallet(104)
        write(self.folder / 'cohort-100/progress.json', {'active_wallet': w, 'consecutive_batches': 2})
        q = self.queue(); fresh = Mock(side_effect=AssertionError('must preserve old work'))
        self.assertEqual(q.next_wallets()[0], w)
        self.assertEqual(q.begin(w, fresh), OLD)
        self.assertNotEqual(self.queue().next_wallets()[0], w)
        self.assertEqual(self.queue().begin(w, fresh), OLD)

    def test_new_job_anchor_survives_restart_and_day_change(self):
        q = self.queue(); w = wallet(1)
        self.assertEqual(q.begin(w, lambda: NEW), NEW)
        self.assertEqual(self.queue(now=NEW['timestamp'] + 2 * DAY).begin(
            w, Mock(side_effect=AssertionError('unfinished work cannot reset'))), NEW)

    def test_unfinished_jobs_cannot_starve_the_rest_of_a_fixed_queue(self):
        q = self.queue(); attempted = set()
        # All histories remain pending; rotation must still reach every wallet.
        q.write = lambda *args: None
        for tick in range(3 * len(self.candidates)):
            q.now = NEW['timestamp'] + tick
            w = q.next_wallets()[0]; attempted.add(w); q.begin(w, lambda: NEW)
        self.assertEqual(attempted, set(self.candidates))
        self.assertEqual(q.reports(), [])

    def test_completed_old_assessment_refreshes_without_overwrite_or_qualification(self):
        w = wallet(1); self.report(w)
        old_path = self.folder / 'cohort-100' / f'wallet-{w}.json'; original = old_path.read_bytes()
        q = self.queue(); self.assertTrue(q.refresh_due(w))
        self.assertFalse(q.reports()[0]['performance_qualified'])
        self.assertIn('history_refresh_pending', q.reports()[0]['qualification_blockers'])
        self.assertEqual(q.begin(w, lambda: NEW), NEW)
        report = self.report(w, NEW); q.complete(w, NEW, report)
        self.assertFalse(q.refresh_due(w))
        self.assertFalse(q.reports()[0]['history_refresh_pending'])
        self.assertEqual(self.queue().reports()[0]['history_snapshot'], NEW)
        self.assertEqual(old_path.read_bytes(), original)

    def test_no_refresh_without_new_activity_or_before_daily_interval(self):
        w = wallet(1); self.report(w)
        self.candidates[w]['last_seen_at'] = OLD['timestamp']
        self.assertFalse(self.queue().refresh_due(w))
        self.candidates[w]['last_seen_at'] += 1
        self.assertFalse(self.queue(now=OLD['timestamp'] + DAY - 1).refresh_due(w))

    def test_report_commit_before_progress_commit_recovers_without_replay(self):
        w = wallet(1); q = self.queue(); q.begin(w, lambda: NEW); self.report(w, NEW)
        resumed = self.queue()
        self.assertFalse(resumed.eligible(w))
        self.assertEqual(resumed.reports()[0]['cutoff'], NEW['timestamp'])

    def test_changed_legacy_identity_fails_closed(self):
        self.queue().begin(wallet(1), lambda: NEW)
        with self.assertRaisesRegex(ValueError, 'identity'):
            HistoryQueue(self.folder, self.candidates, NEW, NEW['timestamp'], write)

    def test_new_arrivals_do_not_jump_a_previously_attempted_older_job(self):
        self.candidates = {wallet(1): {'first_seen_at': 10}, wallet(2): {'first_seen_at': 1000}}
        write(self.folder / 'cohort-100/attempts.json', {wallet(1): 500})
        self.assertEqual(self.queue().next_wallets()[0], wallet(1))
