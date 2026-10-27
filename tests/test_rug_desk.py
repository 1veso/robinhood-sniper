"""Offline acceptance of the article desk; no paid calls or production fills."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from rug_desk import config
from rug_desk.book import Book, day
from rug_desk.check import check
from rug_desk.grok import Judge, validate
from rug_desk.market import check_limits, parse
from rug_desk.report import notify, summary, start_trial
from rug_desk.rugcheck import normalize
from rug_desk.scan import scan
from rug_desk.watch import apply_quote, watch

NOW = 1793100000


def key(n):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    value, out = int.from_bytes(bytes([n]) * 32, 'big'), ''
    while value:
        value, digit = divmod(value, 58)
        out = alphabet[digit] + out
    return out


MINT, POOL = key(1), key(2)


def quote(mint=MINT, *, price='1', liquidity='50000', now=NOW):
    return {'mint': mint, 'pool': POOL, 'price': price, 'liquidity': liquidity,
            'observed_at': now, 'created_at_ms': (NOW - 3600) * 1000, 'h1': {'buys': 30, 'sells': 10}}


def raw_report():
    return {'mint': MINT, 'rugged': False, 'token': {'mintAuthority': None, 'freezeAuthority': None, 'supply': 1000000000, 'decimals': 6},
        'score_normalised': 10, 'risks': [], 'markets': [{'pubkey': POOL, 'lp': {'baseUSD': 25000, 'quoteUSD': 25000, 'lpLockedPct': 99}}],
        'knownAccounts': {}, 'topHolders': [{'address': key(n), 'owner': key(n + 30), 'amount': 10000000, 'pct': 1, 'insider': False} for n in range(10, 22)],
        'totalHolders': 100, 'creatorBalance': 0, 'graphInsidersDetected': 0}


def response(verdict='BUY', confidence=.8):
    url = 'https://x.com/example/status/123'
    return {'status': 'completed', 'output': [{'type': 'custom_tool_call', 'name': 'x_keyword_search', 'status': 'completed'},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'verdict': verdict, 'confidence': confidence,
          'why': 'Evidence reviewed', 'x_read': 'Retrieved post ' + url, 'main_risk': 'Unverified execution costs'}),
          'annotations': [{'type': 'url_citation', 'url': url}]}]}],
        'usage': {'input_tokens': 100, 'output_tokens': 100, 'total_tokens': 200, 'cost_in_usd_ticks': 100000,
                  'server_side_tool_usage_details': {'x_posts_fetched': 1, 'x_users_fetched': 0}}}


BUY = validate(response())


class DeskCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'desk'
        self.book = Book(self.root, initialize=True)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def open(self, mint=MINT):
        return self.book.open_paper(quote(mint), BUY, NOW)

    def test_missing_safety_is_not_zero_or_revoked(self):
        raw = raw_report()
        self.assertIsNone(normalize(MINT, raw, NOW)['failure'])
        for field in ('creatorBalance', 'graphInsidersDetected'):
            bad = copy.deepcopy(raw)
            del bad[field]
            self.assertIsNotNone(normalize(MINT, bad, NOW)['failure'])
        del raw['token']['mintAuthority']
        self.assertIn('authority_revoked', normalize(MINT, raw, NOW)['failure'])

    def test_first_failure_precedes_malformed_later_evidence(self):
        raw = {'mint': MINT, 'rugged': True, 'score_normalised': 'NaN'}
        self.assertEqual(normalize(MINT, raw, NOW)['failure'], 'not_rugged: rejected')

    def test_trial_cannot_start_without_funded_x_verification(self):
        with self.assertRaises(ValueError):
            start_trial(self.book, NOW)
        self.assertFalse((self.book.runtime / 'trial.json').exists())

    def test_holder_units_duplicates_order_and_creator_ambiguity(self):
        for change in ('amount', 'owner', 'pct', 'creator'):
            raw = raw_report()
            if change == 'amount':
                raw['topHolders'][0]['amount'] = 10
            elif change == 'owner':
                raw['topHolders'][1]['owner'] = raw['topHolders'][0]['owner']
            elif change == 'pct':
                raw['topHolders'][1]['pct'] = 2
            else:
                raw['creatorBalance'], raw['creator'] = 12, key(60)
            self.assertIsNotNone(normalize(MINT, raw, NOW)['failure'])

    def test_first_failure_and_no_network_on_full_book(self):
        for n in range(1, 5):
            self.open(key(n))
        provider = Mock()
        result = check(self.book, key(5), market=provider, rugcheck=provider, judge=provider, clock=lambda: NOW)
        self.assertEqual(result['stage'], 'limits')
        self.assertEqual(provider.mock_calls, [])
        result = check(self.book, key(5), clock=lambda: NOW + 1)
        self.assertEqual(result['stage'], 'seen_once')

    def test_market_invalid_numbers_missing_transactions_pool_identity(self):
        for value in ('NaN', 'Infinity', '0', None, True):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.book.open_paper(quote(price=value), BUY, NOW)
        q = quote()
        q['h1'] = {'buys': 30}
        self.assertIn('missing', check_limits(q, NOW))
        with self.assertRaises(ValueError):
            parse(MINT, POOL, [{'chainId': 'solana', 'pairAddress': key(3), 'baseToken': {'address': MINT}}], NOW)

    def test_refresh_after_judgment_and_recheck(self):
        moment = [NOW]
        market, rug, judge = Mock(), Mock(), Mock()
        rug.report.return_value = normalize(MINT, raw_report(), NOW)
        market.quote.side_effect = lambda mint, pool: quote(price='1' if moment[0] == NOW else '2', now=moment[0])
        def decide(*args):
            moment[0] += 90
            return BUY
        judge.ask.side_effect = decide
        result = check(self.book, MINT, market=market, rugcheck=rug, judge=judge, clock=lambda: moment[0])
        self.assertTrue(result['entry'])
        self.assertEqual(self.book.positions()[0]['entry_price'], '2')
        self.assertEqual(market.quote.call_count, 2)

    def test_refresh_rejects_liquidity_drop(self):
        market, rug, judge = Mock(), Mock(), Mock()
        rug.report.return_value = normalize(MINT, raw_report(), NOW)
        judge.ask.return_value = BUY
        market.quote.side_effect = [quote(), quote(liquidity='29999')]
        out = check(self.book, MINT, market=market, rugcheck=rug, judge=judge, clock=lambda: NOW)
        self.assertFalse(out['entry'])
        self.assertEqual(out['verdict'], 'BUY')
        self.assertEqual(self.book.fills(), [])

    def test_concurrent_last_slot(self):
        for n in range(1, 4):
            self.open(key(n))
        def attempt(n):
            book = Book(self.root)
            try:
                book.open_paper(quote(key(n)), BUY, NOW)
                return True
            except ValueError:
                return False
            finally:
                book.close()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, [4, 5]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.book.positions()), 4)

    def test_duplicate_and_stale_quote_blocked(self):
        self.open()
        with self.assertRaises(ValueError):
            self.open()
        with self.assertRaises(ValueError):
            self.book.open_paper(quote(key(3)), BUY, NOW + 31)

    def test_ladder_one_step_per_poll_then_trail(self):
        p = self.open()
        self.assertEqual(apply_quote(self.book, p['id'], quote(price='4'), NOW), 'take_profit_1')
        after = self.book.positions()[0]
        self.assertEqual(after['steps'], 1)
        self.assertEqual(Decimal(after['remaining']), Decimal('12.5'))
        self.assertEqual(apply_quote(self.book, p['id'], quote(price='4', now=NOW + 600), NOW + 600), 'take_profit_2')
        self.assertEqual(Decimal(self.book.positions()[0]['remaining']), Decimal('6.25'))
        self.assertEqual(apply_quote(self.book, p['id'], quote(price='2.7', now=NOW + 1200), NOW + 1200), 'trailing_exit')
        self.assertEqual(self.book.positions(), [])

    def test_liquidity_priority_stop_and_time_exit(self):
        p = self.open()
        self.assertEqual(apply_quote(self.book, p['id'], quote(price='4', liquidity='29999'), NOW), 'liquidity_drop')
        p = self.open(key(3))
        self.assertEqual(apply_quote(self.book, p['id'], quote(key(3), price='.65'), NOW), 'stop')
        p = self.open(key(4))
        self.assertEqual(apply_quote(self.book, p['id'], quote(key(4), now=NOW + 21600), NOW + 21600), 'time_exit')

    def test_missing_pool_does_not_sell(self):
        p = self.open()
        market = Mock()
        market.quote.side_effect = TimeoutError()
        for _ in range(2):
            watch(self.book, market=market, clock=lambda: NOW)
        self.assertEqual(self.book.positions()[0]['remaining'], p['remaining'])
        self.assertEqual(len(self.book.fills()), 1)
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM events WHERE id LIKE 'feed:%'").fetchone()[0], 1)

    def test_manual_alert_then_confirmed_partial_idempotent_fill(self):
        manual = Book(self.root / 'manual', initialize=True, mode='manual')
        try:
            p = manual.confirmed_fill('entry', 'buy', now=NOW, quantity=25, price=1, quote=quote())
            for _ in range(2):
                apply_quote(manual, p['id'], quote(price='4'), NOW)
            untouched = manual.positions()[0]
            self.assertEqual((untouched['remaining'], untouched['steps'], untouched['realized_pnl']), ('25', 0, '0'))
            self.assertEqual(len(manual.fills()), 1)
            args = dict(now=NOW + 1, quantity='6.25', price='4', position_id=p['id'])
            manual.confirmed_fill('partial1', 'sell', **args)
            manual.confirmed_fill('partial1', 'sell', **args)
            self.assertEqual(manual.positions()[0]['steps'], 0)
            manual.confirmed_fill('partial2', 'sell', **{**args, 'now': NOW + 2})
            self.assertEqual(manual.positions()[0]['steps'], 1)
            self.assertEqual(manual.positions()[0]['remaining'], '12.50')
            with self.assertRaises(ValueError):
                manual.confirmed_fill('partial1', 'sell', **{**args, 'quantity': 1})
        finally:
            manual.close()

    def test_missing_ledger_cannot_silently_reinitialize(self):
        self.open()
        self.book.export()
        (self.root / config.POSITIONS).unlink()
        self.book.export()
        self.assertEqual(len(json.loads((self.root / config.POSITIONS).read_text())['positions']), 1)
        self.book.close()
        (self.root / 'runtime' / 'book.sqlite3').unlink()
        with self.assertRaises(RuntimeError):
            Book(self.root, initialize=True)

    def test_realized_halt_excludes_unrealized_and_uses_berlin_day(self):
        for n in range(1, 5):
            p = self.open(key(n))
            apply_quote(self.book, p['id'], quote(key(n), price='.4'), NOW)
        self.assertEqual(self.book.limits(key(5), NOW), 'daily realized loss halt')
        self.assertIsNone(self.book.limits(key(5), NOW + 86400))
        stamp = int(datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(day(stamp), '2026-10-28')
        # DST transitions still share one Berlin civil date.
        for hour in (0, 1, 2):
            self.assertEqual(day(int(datetime(2026, 10, 25, hour, 30, tzinfo=timezone.utc).timestamp())), '2026-10-25')

    def test_seen_once_scanner_five_and_restart(self):
        market, rug = Mock(), Mock()
        market.profiles.return_value = [key(n) for n in range(1, 9)]
        rug.report.side_effect = TimeoutError()
        self.assertEqual(len(scan(self.book, market=market, rugcheck=rug, clock=lambda: NOW)), 5)
        self.book.close()
        self.book = Book(self.root)
        self.assertEqual(len(scan(self.book, market=market, rugcheck=rug, clock=lambda: NOW)), 3)

    def test_paid_disabled_never_reads_credential_or_calls_network(self):
        transport = Mock(side_effect=AssertionError('Paid request'))
        result = Judge(self.root, transport=transport).ask(MINT, {}, {})
        self.assertEqual(result['verdict'], 'SKIP')
        self.assertEqual(transport.call_count, 0)
        self.assertFalse((self.root / 'runtime' / 'ai-budget.sqlite3').exists())

    def test_opt_in_request_schema_and_billed_usage_are_journaled(self):
        credentials = self.root.parent / 'runtime'
        credentials.mkdir()
        (credentials / 'xai-api-key.txt').write_text('synthetic-offline-key')
        transport = Mock(return_value=response())
        result = Judge(self.root, enabled=True, transport=transport, clock=lambda: NOW).ask(MINT, {}, quote())
        self.assertTrue(result['validated'])
        method, url, payload, headers, timeout = transport.call_args.args
        self.assertEqual((method, url, payload['model'], payload['max_turns']), ('POST', 'https://api.x.ai/v1/responses', 'grok-4.7', 2))
        self.assertEqual(payload['tools'][0]['type'], 'x_search')
        self.assertTrue(payload['text']['format']['strict'])
        out = summary(self.book, NOW)
        self.assertEqual(out['api_usage'][0]['billed_usd'], '0.00001')
        # A rejected judgment still consumes and records its actual provider bill.
        transport.return_value = response(confidence=2)
        with self.assertRaises(ValueError):
            Judge(self.root, enabled=True, transport=transport, clock=lambda: NOW).ask(MINT, {}, quote())
        self.assertEqual(len(summary(self.book, NOW)['api_usage']), 2)

    def test_grok_malformed_confidence_search_and_sources(self):
        for confidence in (2, float('nan'), -1, True, '0.8'):
            with self.subTest(confidence=confidence), self.assertRaises((ValueError, TypeError)):
                validate(response(confidence=confidence))
        for change in ('prose', 'search', 'source', 'usage', 'status'):
            r = response()
            if change == 'prose':
                r['output'][1]['content'][0]['text'] = 'Here: ' + r['output'][1]['content'][0]['text']
            elif change == 'search':
                r['output'][0]['name'] = 'not_search'
            elif change == 'source':
                r['output'][1]['content'][0]['annotations'] = []
            elif change == 'usage':
                del r['usage']['cost_in_usd_ticks']
            else:
                r['status'] = 'incomplete'
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(r)

    def test_full_isolated_cycle_and_durable_delivery(self):
        market, rug, judge = Mock(), Mock(), Mock()
        rug.report.return_value = normalize(MINT, raw_report(), NOW)
        judge.ask.return_value = validate(response())
        market.quote.return_value = quote()
        result = check(self.book, MINT, market=market, rugcheck=rug, judge=judge, clock=lambda: NOW)
        self.assertTrue(result['entry'])
        p = self.book.positions()[0]
        apply_quote(self.book, p['id'], quote(price='2', now=NOW + 600), NOW + 600)
        apply_quote(self.book, p['id'], quote(price='1.3', now=NOW + 1200), NOW + 1200)
        report = summary(self.book, NOW + 1200)
        self.assertEqual((report['paper_entries_today'], report['closed_positions_today']), (1, 1))
        self.assertEqual(report['profitable_exit_events_today'], 2)
        self.assertEqual(report['profitable_closed_positions_today'], 1)
        runtime = self.root / 'credentials'
        runtime.mkdir()
        (runtime / 'telegram-bot-token.txt').write_text('test-token')
        (runtime / 'telegram-recipient.json').write_text(json.dumps({'bot_id': 1, 'chat_id': 2}))
        client = Mock()
        client.call.return_value = {'id': 1}
        client.send.side_effect = [101, 102, 103]
        delivery = notify(self.book, runtime=runtime, client_factory=lambda _: client)
        self.assertEqual(delivery['outbox'], {'sent': 3})
        self.assertEqual(notify(self.book, runtime=runtime, client_factory=lambda _: client)['sent'], 0)
        self.assertEqual(client.send.call_count, 3)


if __name__ == '__main__':
    unittest.main()
