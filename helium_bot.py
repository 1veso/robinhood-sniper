"""Helium standalone trading team CLI. All execution is paper-only."""
import argparse
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from rug_desk.book import atomic, process_lock
from sniper.helium.config import encode, load
from sniper.helium.paper import Paper, bound_config


def _message(exc):
    return str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['init', 'scan', 'watch', 'status', 'run'], default='run')
    parser.add_argument('--root', type=Path, default=ROOT, help='Application root containing helium_bot/config.json')
    parser.add_argument('--mode', default='paper')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args(argv)
    if args.mode != 'paper':
        parser.error('Live mode is not implemented; only --mode paper is accepted')
    root = args.root.resolve()
    if args.demo:
        from sniper.helium.demo import demo
        print(encode(demo(root)))
        return 0
    if args.command == 'status':
        from sniper.helium.view import view
        print(encode(view(root)))
        return 0

    folder = root / 'helium_bot/runtime'
    cfg = bound_config(folder) if args.command in ('watch', 'run') else load(root / 'helium_bot')
    if args.command == 'init':
        with process_lock(folder / 'worker.lock'):
            book = Paper(folder, cfg, initialize=True)
            try:
                created = []
                for filename, provider in [('paid-ai.json', 'Grok'), ('paid-jev.json', 'Jev')]:
                    path = folder / filename
                    if not path.exists():
                        atomic(path, encode({'enabled': False, 'operator_authorized': False,
                               'reason': 'Explicit Helium account-bound ' + provider + ' authorization and validation required'}))
                        created.append(filename)
                book.publish()
                print(encode({'account_id': book.identity, 'mode': 'paper',
                              'paid_settings_created': created, 'live_execution_enabled': False}))
            finally:
                book.close()
        return 0

    from sniper.helium.service import scan, watch

    def once(command):
        with process_lock(folder / (command + '.lock')):
            policy = bound_config(folder) if command == 'watch' else load(root / 'helium_bot')
            book = Paper(folder, policy)
            try:
                return (scan if command == 'scan' else watch)(root, book)
            finally:
                book.close()

    if args.command == 'run':
        # Independent scheduled scan/watch jobs are preferred. This convenience
        # loop also retains its watcher when a scan/config/model step fails.
        while True:
            started = time.monotonic()
            for command in ('watch', 'scan'):
                try:
                    once(command)
                except (ValueError, OSError, sqlite3.Error) as exc:
                    print(encode({'worker': command, 'state': 'ERROR', 'reason': _message(exc)}), file=sys.stderr)
            time.sleep(max(1, cfg['poll_seconds'] - (time.monotonic() - started)))
    return 2 if once(args.command) else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error) as exc:
        print('Helium stopped safely: ' + _message(exc), file=sys.stderr)
        sys.exit(2)
