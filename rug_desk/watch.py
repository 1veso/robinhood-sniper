"""Independent ten-minute exit watcher. Alerts never stand in for actual fills."""
from __future__ import annotations
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import Book, encode, number, process_lock
from rug_desk.market import Market, validate_quote


def exit_decision(p, quote, now):
    price, entry, peak = number(quote['price'], positive=True), number(p['entry_price'], positive=True), number(p['peak'], positive=True)
    gain = (price / entry - 1) * 100
    remaining, original = number(p['remaining']), number(p['original'])
    if number(quote['liquidity']) < number(p['entry_liquidity']) * (1 - Decimal(config.EXIT['liq_drop_pct']) / 100):
        return 'liquidity_drop', remaining
    if gain <= config.EXIT['stop_pct']:
        return 'stop', remaining
    step = p['steps']
    if step < len(config.EXIT['ladder']) and gain >= config.EXIT['ladder'][step][0]:
        return f'take_profit_{step + 1}', min(remaining, original * Decimal(str(config.EXIT['ladder'][step][1])))
    if step > 0 and price <= peak * (1 - Decimal(config.EXIT['trail_pct']) / 100):
        return 'trailing_exit', remaining
    if now - p['opened_at'] >= config.EXIT['time_stop_hours'] * 3600 and gain < config.EXIT['time_stop_min_pct']:
        return 'time_exit', remaining
    return None


def apply_quote(book, position_id, quote, now):
    validate_quote(quote, now)
    with book.transaction():
        row = book.db.execute('SELECT payload FROM positions WHERE id=?', (position_id,)).fetchone()
        if not row:
            raise ValueError('Position missing')
        p = json.loads(row[0])
        if number(p['remaining']) == 0:
            return 'already_closed'
        if (p['mint'], p['pool']) != (quote['mint'], quote['pool']):
            raise ValueError('Watcher pool mismatch')
        if now < p['opened_at']:
            raise ValueError('Watcher predates entry')
        p['peak'] = str(max(number(p['peak']), number(quote['price'])))
        p['last_observed_at'] = now
        p['feed_error'] = False
        decision = exit_decision(p, quote, now)
        if not decision:
            book._save(p)
            return 'hold'
        reason, quantity = decision
        if book.identity['mode'] == 'manual':
            previous = p.get('pending')
            if not previous or (previous['reason'], previous['step']) != (reason, p['steps']):
                pending = {'id': uuid.uuid4().hex, 'reason': reason, 'step': p['steps'],
                           'quantity': str(quantity), 'confirmed': '0', 'created_at': now}
                p['pending'] = pending
                book._event('exit:' + pending['id'], now,
                    f"RUG DESK MANUAL — EXIT REQUEST\nMint: {p['mint']}\nPool: {p['pool']}\n"
                    f"Reason: {reason}\nRequested quantity: {quantity}\nIndicative price: ${quote['price']}\n"
                    'No sale recorded. Execute in your wallet only if you choose, then record the actual fill.')
            book._save(p)
            return 'pending_manual_exit'
        if reason.startswith('take_profit_'):
            p['steps'] += 1  # Exactly one ladder step in this poll.
        book._sell(p, quantity, number(quote['price'], positive=True), Decimal(0), now, reason, 'paper:' + uuid.uuid4().hex)
        return reason


def watch(book, *, market=None, clock=time.time):
    market = market or Market()
    results = []
    for p in book.positions():
        try:
            quote = market.quote(p['mint'], p['pool'])  # Network stays outside the ledger lock.
            reason = apply_quote(book, p['id'], quote, int(clock()))
        except Exception as exc:
            now = int(clock())
            with book.transaction():
                row = book.db.execute('SELECT payload FROM positions WHERE id=?', (p['id'],)).fetchone()
                current = json.loads(row[0])
                if not current.get('feed_error') and number(current['remaining']) > 0:
                    book._event('feed:' + uuid.uuid4().hex, now,
                        f"RUG DESK {book.identity['mode'].upper()} — PRICE UNAVAILABLE\nMint: {p['mint']}\nPool: {p['pool']}\n"
                        'Holdings retained; no zero-price sale. Exit conditions cannot currently be evaluated.')
                current['feed_error'] = True
                current['last_error_at'] = now
                book._save(current)
            reason = 'feed_unavailable:' + type(exc).__name__
        results.append({'position_id': p['id'], 'result': reason})
    book.export()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    parser.add_argument('--manual-records', action='store_true')
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    with process_lock(args.root / 'runtime' / 'watch.lock'):
        book = Book(args.root, mode='manual' if args.manual_records else 'paper')
        try:
            with book.run('watch') as run:
                run['positions'] = watch(book)
                if args.send:
                    from rug_desk.report import notify
                    run['notifications'] = notify(book)
            print(encode(run))
        finally:
            book.close()


if __name__ == '__main__':
    main()
