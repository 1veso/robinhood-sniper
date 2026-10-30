"""Krypton standalone paper CLI; the shared console is read-only."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
from rug_desk.book import process_lock
from sniper.krypton.config import load,encode
from sniper.krypton.paper import Paper


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs='?',choices=['init','scan','watch','status','run'],default='run')
    parser.add_argument('--mode',default='paper')
    parser.add_argument('--demo',action='store_true')
    args=parser.parse_args(argv)
    if args.mode!='paper':parser.error('Live mode is not implemented; only --mode paper is accepted')
    from sniper.krypton.providers import settings
    settings(ROOT) # MODE validation occurs before account or network operations.
    if args.demo:
        from sniper.krypton.demo import demo
        print(encode(demo(ROOT)));return 0
    if args.command=='status':
        from sniper.krypton.view import view
        print(encode(view(ROOT)));return 0
    cfg=load(ROOT/'krypton_bot');folder=ROOT/'krypton_bot/runtime'
    if args.command=='init':
        with process_lock(folder/'worker.lock'):
            book=Paper(folder,cfg,initialize=True)
            try:
                from rug_desk.book import atomic
                if not (folder/'paid-ai.json').exists():atomic(folder/'paid-ai.json',encode({'enabled':False,'operator_authorized':False,'reason':'Explicit Krypton paid authorization and funded proofs required'}))
                book.publish();print(encode({'account_id':book.identity,'mode':'paper','paid_enabled':False}))
            finally:book.close()
        return 0
    from sniper.krypton.service import scan,watch
    def once(command):
        with process_lock(folder/(command+'.lock')):
            book=Paper(folder,cfg)
            try:return (scan if command=='scan' else watch)(ROOT,book)
            finally:book.close()
    if args.command=='run':
        while True:
            started=time.monotonic();once('watch');once('scan');time.sleep(max(1,cfg['poll_seconds']-(time.monotonic()-started)))
    return 2 if once(args.command) else 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError) as exc:
        print('Krypton stopped safely: '+(str(exc)[:200] if isinstance(exc,ValueError) else type(exc).__name__),file=sys.stderr);sys.exit(2)
