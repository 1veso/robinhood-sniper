"""Explicit Radeon paper exit revision, without running a trade cycle."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'src'))
from supervise import atomic_json, instance_lock
from sniper.ledger import Ledger
from sniper.policy import RiskPolicy
from sniper.radeon_exits import RUNNER, activate, revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT/'runtime')
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--account')
    parser.add_argument('--source')
    parser.add_argument('--entry-revision')
    parser.add_argument('--review', type=Path)
    args = parser.parse_args()
    if not args.activate:
        print(json.dumps({'paper_only': True, 'policy': RUNNER, 'revision': revision(RUNNER),
                          'active_state_changed': False}, indent=2))
        return 0
    if not all((args.account,args.source,args.entry_revision,args.review)):
        parser.error('Activation requires account, source, entry revision and review evidence')
    evidence_hash = hashlib.sha256(args.review.read_bytes()).hexdigest()
    policy = RiskPolicy.from_file(ROOT/'config/exploration-v3.toml')
    folder = args.runtime/'exploration'
    with instance_lock(args.runtime/'exploration-controller.lock'):
        with instance_lock(folder/'worker.lock'):
            selector = json.loads((args.runtime/'active-exploration.json').read_text(encoding='utf-8'))
            if selector['profile'] != 'parallel-whale-v1' or selector['account_id'] != args.account:
                raise ValueError('Expected active Radeon account/profile is required')
            with sqlite3.connect((folder/'events.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as source:
                actual_source = source.execute("SELECT value FROM evm_metadata WHERE key='identity'").fetchone()
                if actual_source is None or actual_source[0] != args.source:
                    raise ValueError('Canonical source identity mismatch')
            ledger = Ledger(folder/'ledger.sqlite3',policy.starting_cash_usd,require_existing=True)
            try:
                disk = json.loads((folder/'trial.json').read_text(encoding='utf-8'))
                bound = json.loads(ledger._get('trial_manifest'))
                # Only the previous publication of this exact revision may be recovered.
                old = ledger.conn.execute("SELECT payload FROM events WHERE event_id=?",
                                          ('exit-policy-revision:'+revision(RUNNER),)).fetchone()
                prior = json.loads(old[0])['previous_trial'] if old else None
                if disk != bound and disk != prior:
                    raise ValueError('Unrecorded trial manifest change')
                updated = activate(ledger,policy,RUNNER,now=int(time.time()),expected_account=args.account,
                    expected_source=args.source,expected_entry_revision=args.entry_revision,
                    review_reference=str(args.review.resolve())+'#sha256='+evidence_hash)
                atomic_json(folder/'trial.json',updated)
            finally:
                ledger.close()
    print(json.dumps({'activated_exit_revision':updated['exit_revision'],'account_id':updated['account_id'],
                      'ends_at':updated['ends_at'],'policy':RUNNER,'trade_cycle_run':False},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
