"""One seen-once candidate: limits -> facts -> pool -> X -> refresh -> entry."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import Book, encode, number
from rug_desk.grok import Judge
from rug_desk.market import Market, check_limits
from rug_desk.rugcheck import Rugcheck
from sniper.adapters.solana import validate_address


def check(book, mint, *, market=None, rugcheck=None, judge=None, clock=time.time,
          entry_guard=None, entry_hook=None, signal_context=None):
    mint = validate_address(mint)
    now = int(clock())
    if not book.claim(mint, now):
        return {'mint': mint, 'stage': 'seen_once', 'verdict': 'SKIP', 'reason': 'Mint was already checked', 'entry': False}
    market, rugcheck, judge = market or Market(costs=bool(book.cost_policy())), rugcheck or Rugcheck(), judge or Judge(book.root)
    result = {'mint': mint, 'stage': 'limits', 'verdict': 'SKIP', 'entry': False}
    try:
        reason = book.limits(mint, now)
        if reason:
            result['reason'] = reason
            return result
        result['stage'] = 'facts'
        facts = rugcheck.report(mint)
        result['facts'] = facts
        if facts['failure']:
            result['reason'] = facts['failure']
            return result
        result['stage'] = 'pool'
        quote = market.quote(mint, facts['pool'])
        result['pool'] = quote
        reason = check_limits(quote, int(clock()))
        if reason:
            result['reason'] = reason
            return result
        result['stage'] = 'judge'
        judgment = judge.ask(mint, facts, quote)
        if signal_context is not None:
            judgment = {**judgment, 'leader_signal': signal_context}
        result['judgment'] = judgment
        if judgment['verdict'] != 'BUY' or not number(config.MIN_CONFIDENCE) <= number(judgment['confidence']) <= 1 or judgment.get('validated') is not True:
            result['reason'] = judgment.get('why', 'Judgment did not qualify')
            return result
        result['verdict'] = 'BUY'  # A signal is not yet an entry.
        result['stage'] = 'refresh'
        fresh = market.quote(mint, facts['pool'])
        if fresh['pool'] != quote['pool'] or fresh['mint'] != mint:
            raise ValueError('Pool changed during judgment')
        result['refreshed_pool'] = fresh
        reason = check_limits(fresh, int(clock()))
        if reason:
            result['reason'] = reason
            return result
        result['stage'] = 'entry'
        position = book.open_paper(fresh, judgment, int(clock()),
                                  entry_guard=entry_guard, entry_hook=entry_hook)
        result.update(entry=True, position_id=position['id'], reason='Paper entry recorded at refreshed indicative price')
        return result
    except Exception as exc:
        # Stable local reasons only; provider exception bodies may contain URLs.
        result['reason'] = f'{type(exc).__name__}: stage failed; no entry recorded'
        return result
    finally:
        book.finish(mint, int(clock()), result['stage'], result['verdict'], result.get('reason', 'Interrupted'), result)
        book.export()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mint')
    parser.add_argument('--root', type=Path, default=config.ROOT)
    args = parser.parse_args()
    book = Book(args.root)
    try:
        with book.run('check') as run:
            result = check(book, args.mint)
            run.update(result)
        print(encode(result))
    finally:
        book.close()


if __name__ == '__main__':
    main()
