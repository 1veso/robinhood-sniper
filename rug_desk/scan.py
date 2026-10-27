"""Latest profiles, five fresh mints per scan. Every attempted mint is seen once."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import Book, encode, process_lock
from rug_desk.check import check
from rug_desk.market import Market


def scan(book, *, market=None, **dependencies):
    market = market or Market()
    results = []
    for mint in market.profiles():
        if book.checked(mint):
            continue
        results.append(check(book, mint, market=market, **dependencies))
        if len(results) >= config.MAX_SCANS:
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    with process_lock(args.root / 'runtime' / 'scan.lock'):
        book = Book(args.root)
        try:
            with book.run('scan') as run:
                run['checks'] = scan(book)
                if args.send:
                    from rug_desk.report import notify
                    run['notifications'] = notify(book)
            print(encode(run))
        finally:
            book.close()


if __name__ == '__main__':
    main()
