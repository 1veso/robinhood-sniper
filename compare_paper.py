"""Initialize or inspect isolated Radeon baseline/A/B paper comparisons.

Normal scheduled explore.py runs capture and process observations afterwards.
This command never calls a provider, sends a notification or changes a source
account. It cannot start or extend the actual seven-day trial.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from explore import active_profile, profile_settings
from supervise import instance_lock
from sniper.paper_comparison import Comparison, initialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    folder = runtime / 'paper-comparison'
    with instance_lock(runtime / 'exploration-controller.lock'):
        profile = active_profile(runtime)
        if profile != 'two-wallet-30k-reentry':
            raise ValueError('Comparison requires the authorized re-entry baseline')
        policy, _, name = profile_settings(profile)
        trial = json.loads((runtime / name / 'trial.json').read_text(encoding='utf-8'))
        if args.initialize:
            initialize(folder, runtime / name / 'ledger.sqlite3', policy, trial)
        comparison = Comparison(folder, source_account_id=trial['account_id'], source_id=trial['source_id'], policy_version=policy.version)
        try:
            completed = comparison.process()
            comparison.publish(int(time.time()))
            print(json.dumps({'mode': 'ISOLATED_PAPER_COMPARISON', 'comparison_id': comparison.manifest['comparison_id'],
                              'processed': completed, 'entry_deadline': trial['ends_at'], 'report': str(folder / 'report.json')}))
        finally:
            comparison.close()


if __name__ == '__main__':
    main()
