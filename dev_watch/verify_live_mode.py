"""Offline verification of the article's manual live entry. No network or wallet.

Fixtures are confined to a temporary directory; real ledgers are not touched.
This verifies pending-entry behavior, not live exit bookkeeping correctness.
"""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
import config
import enter
from verify_article import verify


def main():
    verify()
    # Live behavior remains an isolated regression test; the installed desk is paper.
    assert config.PAPER is True and enter.PAPER is True
    scratch = ROOT / 'runtime' / 'verification'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as folder:
        pending = str(Path(folder) / 'pending.json')
        checks = str(Path(folder) / 'checks.csv')
        verdict = {'verdict': 'BUY', 'stage': 'grok', 'symbol': 'OFFLINE_TEST',
                   'confidence': 0.9, 'price': 0.01, 'liq_usd': 40000,
                   'snap': {'creator': 'TEST_OWNER', 'dev0': 100, 'dev0_pct': 1,
                            'holders0': {'TEST_ACCOUNT': 500}}}
        with patch.object(enter, 'PAPER', False), patch.object(enter, 'PENDING', pending), patch.object(enter, 'CHECKS', checks):
            with patch.object(enter, 'check', return_value=verdict), \
                 patch.object(enter.book, 'open_position') as open_position:
                out = enter.run('OFFLINE_TEST_MINT')
                open_position.assert_not_called()
                assert out['action'].startswith('APPROVAL NEEDED: buy $25')
                assert 'PAPER BUY' not in out['action']
                assert 'snap' not in out
                saved = json.loads(Path(pending).read_text())
                assert saved['OFFLINE_TEST_MINT']['snap']['dev0'] == 100
            before = Path(pending).read_bytes()
            with patch.object(enter, 'check', return_value={
                    'verdict': 'SKIP', 'stage': 'rpc', 'reason': 'snapshot failed: offline fixture'}), \
                 patch.object(enter.book, 'open_position') as open_position:
                out = enter.run('OFFLINE_SKIP_MINT')
                open_position.assert_not_called()
                assert 'action' not in out
                assert Path(pending).read_bytes() == before
    print('PASS: live BUY creates pending approval, never opens a position; SKIP creates no order')
    print('No network requests, real-ledger changes, or wallet transactions made')


if __name__ == '__main__':
    main()
