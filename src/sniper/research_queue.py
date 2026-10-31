"""Bounded wallet work with immutable per-assessment historical anchors."""
from copy import deepcopy
import json

from .adapters.evm import address

DAY = 86400


class HistoryQueue:
    def __init__(self, folder, candidates, legacy_snapshot, now, write):
        self.folder, self.candidates, self.now, self.write = folder, candidates, now, write
        self.legacy = legacy_snapshot
        self.path = folder / 'history-work.json'
        cohort = folder / f"cohort-{legacy_snapshot['block']}"
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get('version') != 1 or self.state.get('legacy_snapshot') != legacy_snapshot:
                raise ValueError('History work identity mismatch')
        else:
            def read(name):
                path = cohort / name
                return json.loads(path.read_text()) if path.exists() else {}
            self.state = {'version': 1, 'legacy_snapshot': legacy_snapshot,
                          'jobs': {}, 'attempts': read('attempts.json'), 'progress': read('progress.json')}
        self.raw_reports = {}
        for path in sorted(cohort.glob('wallet-0x*.json')):
            report = json.loads(path.read_text())
            self.raw_reports[address(report['wallet'])] = {**report, 'history_snapshot': legacy_snapshot}
        for wallet, snapshot in self.state['jobs'].items():
            path = self.report_path(wallet, snapshot)
            if path.exists():
                report = json.loads(path.read_text())
                if report['wallet'] != wallet or report['cutoff'] != snapshot['timestamp']:
                    raise ValueError('History report identity mismatch')
                self.raw_reports[wallet] = {**report, 'history_snapshot': snapshot}

    def report_path(self, wallet, snapshot):
        return self.folder / f"cohort-{snapshot['block']}" / f'wallet-{address(wallet)}.json'

    def unfinished(self, wallet):
        snapshot = self.state['jobs'].get(wallet)
        return snapshot is not None and not self.report_path(wallet, snapshot).exists()

    def refresh_due(self, wallet):
        report = self.raw_reports.get(wallet)
        candidate = self.candidates[wallet]
        return bool(report and self.now - report['cutoff'] >= DAY
                    and candidate.get('last_seen_at', 0) > report['cutoff'])

    def eligible(self, wallet):
        return self.unfinished(wallet) or wallet not in self.raw_reports or self.refresh_due(wallet)

    def next_wallets(self, limit=5):
        # Rank the entire queue BEFORE bounding the work. The old first-100
        # prefix could permanently exclude thousands of later candidates.
        progress = self.state['progress']
        active = progress.get('active_wallet') if progress.get('consecutive_batches', 0) < 3 else None
        attempts = self.state['attempts']
        queue = [w for w in self.candidates if self.eligible(w)]
        return sorted(queue, key=lambda w: (w != active, attempts.get(w, self.candidates[w]['first_seen_at']),
                      not self.candidates[w].get('observed_recipient', False),
                      self.candidates[w]['first_seen_at'], w))[:limit]

    def begin(self, wallet, current_snapshot):
        wallet = address(wallet)
        jobs, attempts, progress = (self.state[k] for k in ('jobs', 'attempts', 'progress'))
        if self.unfinished(wallet):
            snapshot = jobs[wallet]
        elif wallet not in self.raw_reports and (wallet in attempts or progress.get('active_wallet') == wallet):
            # Resume pre-upgrade work under its original snapshot/checkpoints.
            snapshot = self.legacy
        else:
            snapshot = current_snapshot()
        jobs[wallet] = snapshot
        attempts[wallet] = self.now
        self.state['progress'] = {'active_wallet': wallet, 'last_attempt_at': self.now,
            'consecutive_batches': progress.get('consecutive_batches', 0) + 1
                if progress.get('active_wallet') == wallet else 1}
        # Persist the anchor BEFORE any history request. Interruptions cannot
        # restart an unfinished wallet at a newer cutoff.
        self.write(self.path, self.state)
        return snapshot

    def complete(self, wallet, snapshot, report):
        self.raw_reports[wallet] = {**report, 'history_snapshot': snapshot}
        self.state['progress'] = {'active_wallet': None, 'completed_wallet': wallet,
                                  'completed_at': self.now}
        self.write(self.path, self.state)

    def reports(self):
        reports = []
        for wallet, original in sorted(self.raw_reports.items()):
            report = deepcopy(original)
            pending = wallet in self.candidates and (self.unfinished(wallet) or self.refresh_due(wallet))
            report['history_refresh_pending'] = pending
            if pending:
                report['performance_qualified'] = False
                report['qualified'] = False
                report['qualification_blockers'] = sorted(set(report['qualification_blockers'] + ['history_refresh_pending']))
            reports.append(report)
        return reports

    def summary(self):
        return {'pending_wallets': sum(self.eligible(w) for w in self.candidates),
                'unassessed_wallets': sum(w not in self.raw_reports for w in self.candidates),
                'refresh_due_wallets': sum(self.refresh_due(w) for w in self.candidates),
                'bound_unfinished_wallets': sum(self.unfinished(w) for w in self.candidates),
                'batch_limit': 5, 'rotation_batches': 3, 'new_snapshot_interval_seconds': DAY,
                'progress': self.state['progress']}
