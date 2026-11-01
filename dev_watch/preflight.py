"""Windows-friendly equivalent of step 03's two API checks; never prints secrets.

Default: free Solana getSlot. --xai also makes the article's small paid OK call.
This helper is separate from the eleven unchanged article files.
"""
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
import requests
from config import MODEL, PAPER, RPC_URL, env
from verify_article import verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xai', action='store_true', help='Also make the paid xAI OK check')
    args = parser.parse_args()
    verify()
    print(f'PAPER={PAPER}; model={MODEL}')
    print('XAI_API_KEY: ' + ('present' if env('XAI_API_KEY') else 'missing'))
    ok = True
    try:
        r = requests.post(RPC_URL, json={'jsonrpc': '2.0', 'id': 1, 'method': 'getSlot'}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data.get('result'), int):
            raise ValueError('RPC returned no slot')
        print('RPC PASS: slot ' + str(data['result']))
    except Exception as exc:
        # URLs can contain credentials. Do not print exception messages or bodies.
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        print(f'RPC FAIL: {type(exc).__name__}; HTTP={status}')
        ok = False
    if args.xai:
        key = env('XAI_API_KEY')
        if not key:
            print('xAI BLOCKED: run setup_env.ps1 locally; never paste the key in chat')
            return 1
        try:
            r = requests.post('https://api.x.ai/v1/responses',
                              headers={'Authorization': f'Bearer {key}'},
                              json={'model': MODEL, 'input': 'reply with OK'}, timeout=180)
            r.raise_for_status()
            usage = r.json().get('usage') or {}
            ticks = usage.get('cost_in_usd_ticks')
            if not isinstance(ticks, (int, float)):
                raise ValueError('No cost_in_usd_ticks returned')
            print('xAI PASS: ' + json.dumps({'cost_in_usd_ticks': ticks}))
            print('This setup call is outside costs.csv, as in the article curl check.')
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            print(f'xAI FAIL: {type(exc).__name__}; HTTP={status}')
            ok = False
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
