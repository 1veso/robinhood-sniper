"""Read-only, fail-closed checks for this paper installation's Windows identity.

No account initialization, model calls, notification sends, signing or orders.
Optional network probes are one public HTTPS read and two eth_chainId calls.
"""
import argparse
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request


def inspect(root, network=False):
    root = Path(root).resolve()
    result = {'started_at': int(time.time()), 'pid': os.getpid(), 'root': str(root),
              'checks': {}, 'paid_calls': 0, 'orders': 0, 'signatures': 0, 'broadcasts': 0}
    if os.name == 'nt':
        session = ctypes.c_ulong()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise OSError('Cannot establish Windows session')
        result['windows_session_id'] = session.value
    def read(rel):
        return json.loads((root/rel).read_text(encoding='utf-8-sig'))
    def account(rel, key):
        with closing(sqlite3.connect((root/rel).as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
            return db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()[0]
    def check(name, fn):
        try:
            value = fn()
            result['checks'][name] = {'ok': value is not False, 'result': value}
        except Exception as exc:
            # Never serialize provider URLs, key material or exception messages.
            result['checks'][name] = {'ok': False, 'error_type': type(exc).__name__}
    def paper():
        identity = account('runtime/exploration/ledger.sqlite3', 'account_id')
        if (identity != read('runtime/exploration/trial.json')['account_id'] or
            identity != read('runtime/active-exploration.json')['account_id'] or
            account('runtime/exploration/ledger.sqlite3', 'execution_mode') != 'paper'):
            raise ValueError('Paper identity mismatch')
        return identity
    def solana(name):
        identity = json.loads(account(name+'/runtime/book.sqlite3', 'identity'))
        if identity != read(name+'/runtime/account.json') or identity['mode'] != 'paper':
            raise ValueError('Solana identity mismatch')
        return identity['account_id']
    check('paper_identity', paper)
    for name in ('rug_desk', 'neon_bot'):
        check(name+'_identity', lambda n=name: solana(n))
        check(name+'_paid_disabled', lambda n=name: read(n+'/runtime/paid-ai.json').get('enabled') is False)
    check('jev_paid_disabled', lambda: read('runtime/jev-review/settings.json').get('paid_authorized') is False)
    check('credential_files_readable', lambda: all(bool((root/'runtime'/n).read_text(encoding='utf-8-sig').strip())
        for n in ('telegram-bot-token.txt', 'quote-rpc-url.txt', 'observation-rpc-url.txt')))
    protected = ['rug_desk/config.py', 'rug_desk/runtime/account.json', 'neon_bot/runtime/account.json',
        'runtime/active-exploration.json', 'runtime/exploration/trial.json',
        'rug_desk/runtime/execution-cost-policy.json', 'neon_bot/runtime/execution-cost-policy.json',
        'rug_desk/runtime/paid-ai.json', 'neon_bot/runtime/paid-ai.json', 'runtime/jev-review/settings.json']
    result['protected_sha256'] = {p: hashlib.sha256((root/p).read_bytes()).hexdigest() for p in protected}
    def get(url, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
            headers={'Content-Type':'application/json', 'User-Agent':'SNIPER-recovery-check'})
        with urllib.request.urlopen(req, timeout=12) as response:
            obj = json.load(response)
        if body:
            return obj.get('result') == '0x1237' and 'error' not in obj  # chain 4663
        return isinstance(obj, list)
    # A failed local prerequisite must not even reach optional network probes.
    if network and all(x['ok'] for x in result['checks'].values()):
        check('public_https', lambda: get('https://api.dexscreener.com/token-pairs/v1/solana/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'))
        for name in ('quote', 'observation'):
            check(name+'_rpc', lambda n=name: get((root/'runtime'/f'{n}-rpc-url.txt').read_text(encoding='utf-8-sig').strip(),
                {'jsonrpc':'2.0','id':1,'method':'eth_chainId','params':[]}))
    result.update(finished_at=int(time.time()), network_requested=network,
                  ok=all(x['ok'] for x in result['checks'].values()))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--network', action='store_true')
    args = parser.parse_args()
    try:
        result = inspect(args.root, args.network)
    except Exception as exc:
        result = {'ok': False, 'error_type': type(exc).__name__, 'finished_at': int(time.time())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2), encoding='utf-8')
    temporary.replace(args.output)
    raise SystemExit(0 if result['ok'] else 1)
