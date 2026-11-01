"""Offline regression tests. No real positions, paid API calls, or wallets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import book
import watch

ROOT = Path(__file__).resolve().parent


class ExitConfirmationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'runtime' / 'verification'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.positions = str(self.folder / 'positions.json')
        self.trades = str(self.folder / 'trades.csv')
        for obj, name, value in [(book, 'POSITIONS', self.positions),
                                 (book, 'TRADES', self.trades), (watch, 'PAPER', False)]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.pool = patch.object(watch.facts, 'pool', return_value={'price': 1, 'liq_usd': 40000}).start()
        self.dev = patch.object(watch.rpc, 'wallet_balance', return_value=100).start()
        self.holders = patch.object(watch.rpc, 'balances', return_value={'h1': 50, 'h2': 50}).start()
        self.addCleanup(patch.stopall)
        self.fresh()

    def fresh(self, **fields):
        if Path(self.trades).exists():
            Path(self.trades).unlink()
        book.save({})
        book.open_position('TEST', 'TEST', 1, 40000,
                           {'creator': 'dev', 'dev0': 100, 'dev0_pct': 1,
                            'holders0': {'h1': 50, 'h2': 50}}, 25)
        if fields:
            book.update('TEST', **fields)
        self.pool.return_value = {'price': 1, 'liq_usd': 40000}
        self.dev.return_value = 100
        self.holders.return_value = {'h1': 50, 'h2': 50}

    def pending(self):
        return book.load()['TEST']['pending_exit']

    def test_all_live_exit_paths_only_alert_and_repeat_without_selling(self):
        cases = ['pool_gone', 'dev_sold', 'holders_exit', 'liquidity_pulled',
                 'x_dead', 'x_fading', 'stop', 'take_profit', 'trailing_stop', 'time_stop']
        for reason in cases:
            with self.subTest(reason=reason):
                self.fresh()
                if reason == 'pool_gone': self.pool.return_value = None
                if reason == 'dev_sold': self.dev.return_value = 75
                if reason == 'holders_exit': self.holders.return_value = {'h1': 20, 'h2': 50}
                if reason == 'liquidity_pulled': self.pool.return_value['liq_usd'] = 27000
                if reason == 'x_dead': book.update('TEST', pulse='dead')
                if reason == 'x_fading': book.update('TEST', pulse='fading')
                if reason == 'stop': self.pool.return_value['price'] = 0.65
                if reason == 'take_profit': self.pool.return_value['price'] = 1.9
                if reason == 'trailing_stop': book.update('TEST', took=True, peak=2)
                if reason == 'time_stop': book.update('TEST', opened=time.time()-7*3600)
                before = book.load()['TEST']
                rows = book.trades()
                events = watch.tick()
                after = book.load()['TEST']
                self.assertEqual(after['pending_exit']['reason'], reason)
                self.assertTrue(any('book.py confirm TEST' in e for e in events))
                for field in ['qty', 'qty0', 'took', 'trimmed_on_fade']:
                    self.assertEqual(after[field], before[field])
                self.assertEqual(book.trades(), rows)
                self.assertEqual(book.today_pnl(), 0)
                watch.tick()
                self.assertEqual(self.pending()['id'], after['pending_exit']['id'])
                self.assertEqual(book.trades(), rows)

    def test_zero_price_pool_does_not_write_off_live_holding(self):
        self.pool.return_value['price'] = 0
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'pool_gone')
        self.assertEqual(book.load()['TEST']['qty'], 25)
        self.assertEqual(len(book.trades()), 1)

    def test_confirm_full_sale_uses_actual_price_and_only_then_closes(self):
        self.dev.return_value = 70
        watch.tick()
        pnl = book.confirm_exit('TEST', self.pending()['id'], 25, 0.8)
        self.assertAlmostEqual(pnl, -5)
        self.assertNotIn('TEST', book.load())
        row = book.trades()[-1]
        self.assertEqual(row['reason'], 'dev_sold')
        self.assertEqual(float(row['usd']), 20)
        self.assertEqual(float(row['pnl']), -5)
        self.assertEqual(row['action'], 'close')
        self.assertEqual(book.guard('TEST'), 'cooldown')

    def test_partial_take_profit_fills_wait_for_total_and_reject_replay(self):
        self.pool.return_value['price'] = 1.9
        watch.tick()
        first = self.pending()['id']
        self.assertEqual(self.pending()['qty'], 12.5)
        book.confirm_exit('TEST', first, 5, 1.8)
        p = book.load()['TEST']
        self.assertEqual(p['qty'], 20)
        self.assertFalse(p['took'])
        self.assertEqual(p['pending_exit']['qty'], 7.5)
        self.assertNotEqual(p['pending_exit']['id'], first)
        rows = book.trades()
        with self.assertRaisesRegex(ValueError, 'stale|already confirmed'):
            book.confirm_exit('TEST', first, 5, 1.8)
        self.assertEqual(book.trades(), rows)
        watch.tick()
        self.assertEqual(self.pending()['qty'], 7.5)
        second = self.pending()['id']
        book.confirm_exit('TEST', second, 7.5, 1.7)
        p = book.load()['TEST']
        self.assertEqual(p['qty'], 12.5)
        self.assertTrue(p['took'])
        self.assertNotIn('pending_exit', p)
        self.assertAlmostEqual(book.today_pnl(), 9.25)
        self.pool.return_value['price'] = 1.4
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'trailing_stop')
        self.assertEqual(self.pending()['qty'], 12.5)

    def test_fading_flag_changes_only_after_confirmed_target_fill(self):
        book.update('TEST', pulse='fading')
        watch.tick()
        book.confirm_exit('TEST', self.pending()['id'], 12.5, 0.95)
        p = book.load()['TEST']
        self.assertTrue(p['trimmed_on_fade'])
        self.assertFalse(p['took'])
        self.assertEqual(p['qty'], 12.5)
        self.assertEqual(watch.tick(), [])

    def test_interrupted_partial_target_does_not_reset_after_another_alert(self):
        self.pool.return_value['price'] = 1.9
        watch.tick()
        book.confirm_exit('TEST', self.pending()['id'], 5, 1.8)
        self.dev.return_value = 70
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'dev_sold')
        self.assertEqual(self.pending()['qty'], 20)
        self.dev.return_value = 100
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'take_profit')
        self.assertEqual(self.pending()['qty'], 7.5)

    def test_full_exit_can_fill_in_parts_and_watch_keeps_the_remainder(self):
        self.dev.return_value = 70
        watch.tick()
        book.confirm_exit('TEST', self.pending()['id'], 10, 0.9)
        watch.tick()
        self.assertEqual(book.load()['TEST']['qty'], 15)
        self.assertEqual(self.pending()['qty'], 15)
        book.confirm_exit('TEST', self.pending()['id'], 15, 0.8)
        self.assertEqual(book.load(), {})
        self.assertAlmostEqual(book.today_pnl(), -4)

    def test_invalid_or_stale_confirmation_has_no_effect(self):
        pending = book.request_exit('TEST', 1, 'stop')
        before = Path(self.positions).read_bytes(), Path(self.trades).read_bytes()
        for qty, price in [(0, 1), (-1, 1), (26, 1), (float('nan'), 1),
                           (1, float('inf')), (1, 0), (1, -1)]:
            with self.subTest(qty=qty, price=price), self.assertRaises(ValueError):
                book.confirm_exit('TEST', pending['id'], qty, price)
            self.assertEqual((Path(self.positions).read_bytes(), Path(self.trades).read_bytes()), before)
        with self.assertRaises(ValueError):
            book.confirm_exit('TEST', 'WRONG_ID', 1, 1)
        self.assertEqual((Path(self.positions).read_bytes(), Path(self.trades).read_bytes()), before)

    def test_higher_priority_exit_keeps_original_rule_order(self):
        book.update('TEST', pulse='fading')
        self.pool.return_value['price'] = 1.9
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'x_fading')
        old = self.pending()['id']
        self.dev.return_value = 70
        watch.tick()
        self.assertEqual(self.pending()['reason'], 'dev_sold')
        self.assertEqual(self.pending()['qty'], 25)
        with self.assertRaises(ValueError):
            book.confirm_exit('TEST', old, 5, 1.8)

    def test_paper_exits_keep_immediate_fill_behavior(self):
        with patch.object(watch, 'PAPER', True):
            self.pool.return_value['price'] = 1.9
            watch.tick()
            self.assertTrue(book.load()['TEST']['took'])
            self.assertEqual(book.load()['TEST']['qty'], 12.5)
            self.assertAlmostEqual(book.today_pnl(), 11.25)
            self.assertNotIn('pending_exit', book.load()['TEST'])
            self.fresh(pulse='fading')
            watch.tick()
            self.assertTrue(book.load()['TEST']['trimmed_on_fade'])
            self.fresh()
            self.pool.return_value = None
            watch.tick()
            self.assertEqual(book.load(), {})
            self.assertEqual(book.today_pnl(), -25)

    def test_concurrent_confirmation_and_watch_update_do_not_restore_sold_tokens(self):
        pending = book.request_exit('TEST', 1, 'stop')
        setup = f"import book; book.POSITIONS={self.positions!r}; book.TRADES={self.trades!r}; "
        update = setup + "[book.update('TEST', peak=2) for _ in range(30)]"
        confirm = setup + f"book.confirm_exit('TEST', {pending['id']!r}, 10, 0.9)"
        children = [subprocess.Popen([sys.executable, '-c', code], cwd=ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for code in [update, confirm]]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(book.load()['TEST']['qty'], 15)
        self.assertEqual(book.load()['TEST']['peak'], 2)
        self.assertAlmostEqual(book.today_pnl(), -1)

    def test_cli_confirmation_is_explicit_and_duplicate_fails(self):
        # Run the actual CLI against a fully isolated working directory.
        pending = book.request_exit('TEST', 1, 'stop')
        args = [sys.executable, str(ROOT/'book.py'), 'confirm', 'TEST', pending['id'], '25', '0.9']
        first = subprocess.run(args, cwd=self.folder, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('Confirmed fill recorded', first.stdout)
        second = subprocess.run(args, cwd=self.folder, capture_output=True, text=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(len(book.trades()), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
