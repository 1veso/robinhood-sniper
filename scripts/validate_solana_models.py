"""One authorized funded contract check. Never creates a candidate, order or fill."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bot',choices=('xenon','neon','krypton','helium'))
    parser.add_argument('--allow-paid',action='store_true')
    args=parser.parse_args()
    if not args.allow_paid:parser.error('--allow-paid is required; no request sent')
    if args.bot in ('xenon','neon'):
        from rug_desk.model_gate import binding,status
        from rug_desk.grok import Judge
        from rug_desk.book import atomic,encode
        folder=ROOT/('rug_desk' if args.bot=='xenon' else 'neon_bot')
        current=binding(folder)
        auth=json.loads((folder/'runtime/paid-ai.json').read_text())
        if (auth.get('enabled') is not True or auth.get('operator_authorized') is not True
                or auth.get('binding')!=current or not auth.get('approval_reference')):
            raise ValueError('Current explicit paid authorization required')
        if status(folder)['enabled']:
            print(args.bot+': existing current proof; no paid call needed');return
        result=Judge(folder,enabled=True,budget_root=ROOT/'rug_desk',billing_account_id=current['account_id']).ask(
            'So11111111111111111111111111111111111111112',
            {'test_only':True,'screens_passed':False},{'test_only':True})
        proof={**result,'tested_at':int(time.time()),'test_only':True,
               'scope':'Structured inference and completed X retrieval from the same original funded response; no trade approval'}
        for name in ('inference-test.json','x-search-test.json'):
            atomic(folder/'runtime'/name,encode(proof))
        print(args.bot+': funded structured inference and X Search verified; '+result['request_id'])
    else:
        from sniper.krypton.config import load
        if args.bot=='krypton':
            from sniper.krypton.paper import Paper
            from sniper.krypton.grok import Grok
        else:
            from sniper.helium.paper import Paper
            from sniper.helium.analyst import Grok
        folder=ROOT/(args.bot+'_bot')
        with closing(Paper(folder/'runtime',load(folder))) as book:
            model=Grok(ROOT,book)
            if model.gate()['enabled']:
                print(args.bot+': existing current proof; no paid call needed');return
            proof=model.test()
            print(args.bot+': funded contract verified; '+proof['request_id'])


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Validation failed ('+type(exc).__name__+'); inspect the local audit. No automatic retry.')
        raise SystemExit(1) from None
