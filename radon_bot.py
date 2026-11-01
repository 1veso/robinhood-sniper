"""Radon — slot-eight Solana copier desk. Internal paper execution only."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT/'src')]
from rug_desk.book import atomic, process_lock
from sniper.radon.config import encode, load
from sniper.radon.book import Book, bound_config, folder_for


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='status', choices=['init','status','import','research','entries','watch','report','replay','run','scheduled','model-status','model-binding','model-test'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--history', action='store_true', help='Import the research export rather than the event feed')
    parser.add_argument('--job', choices=['research','entries','watch','report'])
    parser.add_argument('--mode', default='paper')
    parser.add_argument('--demo', action='store_true', help='Run a synthetic acceptance scenario in a separate runtime')
    parser.add_argument('--duration-seconds', type=int, default=0, help='Bound run-loop lifetime for desktop workers')
    parser.add_argument('--allow-paid', action='store_true', help='Explicitly permit one funded validation, subject to existing authorizations and budgets')
    parser.add_argument('--test-kind', choices=['inference','x-search'])
    parser.add_argument('--mint')
    args = parser.parse_args(argv)
    if args.mode != 'paper':
        parser.error('Live execution is not implemented; no wallet or signing path exists')
    root = args.root.resolve()
    if args.demo:
        from sniper.radon.demo import demo
        print(encode(demo(root))); return 0
    if args.command == 'status':
        from sniper.radon.view import view
        print(encode(view(root))); return 0
    folder = folder_for(root)
    if args.command == 'init':
        book = Book(root, load(root/'radon_bot'), initialize=True)
        try:
            paid = folder/'paid-ai.json'
            if not paid.exists():
                atomic(paid, encode({'enabled': False, 'operator_authorized': False,
                    'reason': 'Explicit Radon account-bound authorization and funded inference/X proofs required'}))
            book.publish(); print(encode({'bot': 'radon', 'slot': 8, 'account_id': book.identity, 'mode': 'paper'}))
        finally:
            book.close()
        return 0
    if args.command == 'replay':
        if not args.source:
            parser.error('--source is required for replay')
        from sniper.radon.source import read
        from sniper.radon.replay import research
        result = research(read(args.source, demo=True), load(root/'radon_bot'))
        path = folder/'replays'/f'{int(time.time())}.json'
        atomic(path, encode(result)); print(encode({'replay_file': str(path), 'result': result})); return 0
    cfg = bound_config(root)
    if args.command == 'import':
        if not args.source:
            parser.error('--source is required')
        from sniper.radon.source import read
        bundle = read(args.source)
        with process_lock(folder/'import.lock'):
            # Validation happens before changing the installed export. No remote login.
            atomic(folder/('history.json' if args.history else 'source.json'), encode(bundle))
        print(encode({'imported': 'history' if args.history else 'source', 'coverage': bundle['coverage']})); return 0
    if args.command in ('model-status','model-binding','model-test'):
        from sniper.radon.model import Model
        book = Book(root, cfg)
        try:
            model = Model(root, book)
            if args.command == 'model-status':
                result = model.gate()
            elif args.command == 'model-binding':
                result = model.binding()  # Credential digest only, never the key or a paid request.
            else:
                if not args.allow_paid or not args.test_kind:
                    parser.error('One funded test requires --allow-paid and --test-kind; it does not enable recurring authorization')
                result = model.test(args.test_kind, args.mint)
            print(encode(result))
        finally:
            book.close()
        return 0
    from sniper.radon import service

    def once(job, scheduled=False):
        with process_lock(folder/(job+'.lock')):
            # Always use the original account policy for exits and reports.
            book = Book(root, bound_config(root))
            try:
                if scheduled and job in ('research','report') and not service.daily_due(book, job, int(time.time())):
                    return 0
                result = getattr(service, job)(book)
                ok = isinstance(result, str) or not result
                if ok and scheduled and job in ('research','report'):
                    book.set('schedule:'+job, {'day': datetime.now(timezone.utc).date().isoformat(), 'completed_at': int(time.time())})
                print(encode({'worker': job, 'result': result}))
                return 0 if ok else 2
            finally:
                book.close()
    if args.command == 'scheduled':
        if not args.job:
            parser.error('--job is required')
        return once(args.job, True)
    if args.command == 'run':
        if args.job not in ('entries', 'watch'):
            parser.error('Use a separate run --job entries or run --job watch process so research cannot delay exits')
        started = time.monotonic()
        while True:
            try:
                once(args.job)
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                print(encode({'worker': args.job, 'state': 'ERROR', 'reason': str(exc)[:300]}))
            if args.duration_seconds and time.monotonic()-started+cfg['poll_seconds'] >= args.duration_seconds:
                return 0
            time.sleep(cfg['poll_seconds'])
    return once(args.command)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print('Radon stopped safely: '+(str(exc)[:300] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__), file=sys.stderr)
        sys.exit(2)
