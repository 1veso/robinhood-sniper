"""Cost arithmetic and receipt safety use isolated fixtures, never real orders."""
import base64
import copy
from decimal import Decimal as D
import json
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import Mock

from tests.test_rug_desk import MINT, POOL, NOW, BUY, key, quote
from rug_desk.book import Book
from rug_desk.watch import apply_quote, watch
from rug_desk.report import report_event_id
from sniper.execution_costs import DEFAULT_POLICY, simulate
from sniper.live_readiness import evaluate
from sniper.adapters.solana_execution import USDC, SolanaExecutionReader, order_evidence, reconcile_receipt, signature_bytes
from prepare_live import local_reader, record_reconciled, verify_preparation_screen

WALLET, SIGNATURE = key(3), '5' * 88


def cost_quote(**kwargs):
    out = quote(**kwargs)
    return {**out, 'sol_usd': '125', 'sol_observed_at': out['observed_at']}


def order():
    return {'inputMint': USDC, 'outputMint': MINT, 'swapMode': 'ExactIn', 'taker': WALLET,
        'inAmount': '25000000', 'outAmount': '24000000', 'otherAmountThreshold': '23760000', 'slippageBps': 100,
        'routePlan': [{'swapInfo': {'ammKey': POOL, 'inputMint': USDC, 'outputMint': MINT,
            'inAmount': '25000000', 'outAmount': '24000000'}, 'percent': 100}],
        'signatureFeeLamports': 5000, 'prioritizationFeeLamports': 100000, 'rentFeeLamports': 2039280,
        'signatureFeePayer': WALLET, 'prioritizationFeePayer': WALLET, 'rentFeePayer': WALLET,
        'gasless': False, 'feeBps': 10, 'feeMint': USDC, 'platformFee': {'amount': '25000', 'feeBps': 10},
        'priceImpact': '0.1', 'transaction': base64.b64encode(b'\1' + b'\0' * 64 + b'unsigned-message-fixture').decode(), 'transactionVersion': 0, 'requestId': 'test'}


def intent():
    return {**order_evidence(order(), mint=MINT, pool=POOL, side='buy', amount_raw='25000000', wallet=WALLET, now=NOW), 'screened_liquidity_usd': '50000'}


def receipt(*, failed=False):
    fee, rent = 105000, 2039280
    def bal(index, mint, amount):
        return {'accountIndex': index, 'mint': mint, 'owner': WALLET, 'uiTokenAmount': {'amount': str(amount), 'decimals': 6}}
    return {'slot': 123, 'blockTime': NOW, '_wire_transaction': base64.b64encode(b'\1' + signature_bytes(SIGNATURE) + b'unsigned-message-fixture').decode(), 'transaction': {'signatures': [SIGNATURE], 'message': {'accountKeys': [
        {'pubkey': WALLET, 'signer': True}, {'pubkey': POOL, 'signer': False},
        {'pubkey': key(4), 'signer': False}, {'pubkey': key(5), 'signer': False}]}},
        'meta': {'err': {'InstructionError': [0, 'Custom']} if failed else None, 'fee': fee,
        'preBalances': [1000000000, 100, 2039280, 0],
        'postBalances': [1000000000 - fee - (0 if failed else rent), 100, 2039280, 0 if failed else rent],
        'preTokenBalances': [bal(2, USDC, 100000000)],
        'postTokenBalances': [bal(2, USDC, 75000000), bal(3, MINT, 24000000)]}}


class CostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book = Book(Path(self.temp.name) / 'paper', initialize=True)
        self.book.enable_costs(NOW)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_model_lowers_output_and_charges_both_network_fees_once(self):
        p = self.book.open_paper(cost_quote(), BUY, NOW)
        self.assertLess(D(p['original']), 25)
        self.assertEqual(D(p['realized_pnl']), D('-.013125'))
        apply_quote(self.book, p['id'], cost_quote(now=NOW + 21600), NOW + 21600)
        fills = self.book.fills()
        received = D(fills[1]['quantity']) * D(fills[1]['price'])
        expected = received - 25 - D('.02625')
        self.assertLess(abs(sum(D(f['pnl']) for f in fills) - expected), D('1e-20'))
        self.assertEqual(D(fills[0]['execution_costs']['rent_capital_reserved_usd']), D('.254910'))
        self.assertLess(expected, 0)

    def test_partial_and_final_exit_keep_net_marks_and_costs(self):
        p = self.book.open_paper(cost_quote(), BUY, NOW)
        apply_quote(self.book, p['id'], cost_quote(price='4', now=NOW + 600), NOW + 600)
        current = self.book.positions()[0]
        self.assertEqual(current['steps'], 1)
        mark = simulate(DEFAULT_POLICY, 'sell', cost_quote(price='4', now=NOW + 600), current['remaining'], NOW + 600)
        self.assertEqual(D(current['net_liquidation_usd']), D(current['remaining']) * D(mark['fill_price']) - D(mark['explicit_fee_usd']))
        apply_quote(self.book, p['id'], cost_quote(price='2.7', now=NOW + 1200), NOW + 1200)
        self.assertEqual(self.book.positions(), [])
        self.assertEqual(len(self.book.fills()), 3)

    def test_stale_or_missing_native_price_blocks_fill_not_zero_cost(self):
        for q in (quote(), {**cost_quote(), 'sol_observed_at': NOW - 31}, {**cost_quote(), 'sol_usd': 'NaN'}):
            with self.assertRaises(ValueError):
                self.book.open_paper(q, BUY, NOW)
        self.assertEqual(self.book.fills(), [])

    def test_missing_exit_costs_retains_holdings_and_ladder(self):
        p = self.book.open_paper(cost_quote(), BUY, NOW)
        market = Mock()
        market.quote.return_value = quote(price='4')
        watch(self.book, market=market, clock=lambda: NOW)
        current = self.book.positions()[0]
        self.assertEqual((current['remaining'], current['steps']), (p['remaining'], 0))

    def test_cost_activation_does_not_rewrite_old_account(self):
        other = Book(Path(self.temp.name) / 'legacy', initialize=True)
        try:
            other.open_paper(quote(), BUY, NOW)
            with self.assertRaises(ValueError):
                other.enable_costs(NOW)
        finally:
            other.close()

    def test_positive_pnl_cannot_auto_enable_live(self):
        p = self.book.open_paper(cost_quote(), BUY, NOW)
        apply_quote(self.book, p['id'], cost_quote(price='4'), NOW)
        result = evaluate(self.book, NOW)
        self.assertFalse(result['real_money_enabled'])
        self.assertEqual(result['execution_target'], 'automatic')
        self.assertFalse(result['per_trade_user_approval_required'])
        self.assertFalse(result['automatic_live_executor_available'])
        self.assertFalse(result['live_execution_ready'])
        self.assertTrue(result['execution_blockers'])
        self.assertFalse(result['performance_ready_for_human_review'])
        self.assertFalse(result['checks']['seven_days_of_cost_aware_trial'])

    def test_api_costs_unknown_or_unmarked_open_positions_block_profit_claim(self):
        self.book.open_paper(cost_quote(), BUY, NOW)
        result = evaluate(self.book, NOW, [{'billed_usd': None}])
        self.assertIsNone(result['net_including_open_positions_usd'])
        self.assertFalse(result['checks']['all_api_costs_reconciled'])

    def test_preview_does_not_consume_the_daily_report_notification(self):
        before = int(datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc).timestamp())
        due = int(datetime(2026, 10, 27, 20, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(report_event_id(before), 'preview:2026-10-27')
        self.assertEqual(report_event_id(due), 'daily:2026-10-27:21:30')


class ReceiptTests(unittest.TestCase):
    def test_server_key_precedence_and_invalid_server_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'jupiter-api-key.txt').write_text('legacy-fixture')
            self.assertEqual(local_reader(root).api_key, 'legacy-fixture')
            (root / 'jupiter-server-api-key.txt').write_text('server-fixture\n')
            self.assertEqual(local_reader(root).api_key, 'server-fixture')
            for value in ('', 'first\nsecond'):
                (root / 'jupiter-server-api-key.txt').write_text(value)
                with self.assertRaises(ValueError):
                    local_reader(root)

    def test_safety_failure_blocks_entry_but_does_not_strand_exit_preparation(self):
        safety = {'failure': 'not_rugged: rejected', 'pool': POOL}
        with self.assertRaises(ValueError):
            verify_preparation_screen('buy', safety, POOL, cost_quote(), NOW)
        verify_preparation_screen('sell', safety, POOL, cost_quote(), NOW)

    def test_quote_pins_pool_amount_and_fee_semantics(self):
        evidence = intent()
        self.assertFalse(evidence['real_money_enabled'])
        self.assertFalse(evidence['transaction_instruction_reviewed'])
        for field, value in [('inAmount', '999'), ('slippageBps', 101), ('gasless', True), ('prioritizationFeeLamports', 100001)]:
            raw = order(); raw[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                order_evidence(raw, mint=MINT, pool=POOL, side='buy', amount_raw='25000000', wallet=WALLET, now=NOW)
        raw = order(); raw['routePlan'][0]['swapInfo']['ammKey'] = key(9)
        with self.assertRaises(ValueError):
            order_evidence(raw, mint=MINT, pool=POOL, side='buy', amount_raw='25000000', wallet=WALLET, now=NOW)

    def test_reader_cannot_broadcast_or_sign(self):
        transport = Mock()
        reader = SolanaExecutionReader(transport=transport)
        with self.assertRaises(ValueError):
            reader.rpc('sendTransaction', ['anything'])
        self.assertEqual(transport.call_count, 0)

    def test_full_genesis_not_shortened_chain_identifier(self):
        reader = SolanaExecutionReader(transport=Mock(return_value={'id': 1, 'result': '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'}))
        reader.verify_chain()
        reader.transport.return_value['result'] = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'
        with self.assertRaises(ValueError):
            reader.verify_chain()

    def test_unfinalized_receipt_cannot_become_a_fill(self):
        reader = SolanaExecutionReader()
        reader.verify_chain = Mock()
        reader.rpc = Mock(return_value={'value': [{'confirmationStatus': 'confirmed', 'slot': 123}]})
        with self.assertRaises(ValueError):
            reader.finalized_receipt(SIGNATURE)
        self.assertEqual(reader.rpc.call_count, 1)

    def test_simulation_is_not_execution_and_rejects_changed_bytes(self):
        raw, evidence = order(), intent()
        reader = SolanaExecutionReader(transport=Mock())
        reader.verify_chain = Mock()
        reader.rpc = Mock(return_value={'context': {'slot': 123}, 'value': {'err': None, 'unitsConsumed': 100}})
        result = reader.simulate(raw, evidence, NOW)
        self.assertTrue(result['simulation_succeeded'])
        self.assertFalse(result['broadcast'])
        raw['transaction'] = base64.b64encode(b'changed').decode()
        with self.assertRaises(ValueError):
            reader.simulate(raw, evidence, NOW)

    def test_actual_net_token_amounts_and_native_rent_are_separate(self):
        result = reconcile_receipt(receipt(), intent(), SIGNATURE)
        self.assertEqual(result['token_quantity_raw'], '24000000')
        self.assertEqual(result['usdc_change_raw'], '-25000000')
        self.assertEqual(result['network_fee_lamports'], 105000)
        self.assertEqual(result['rent_capital_change_lamports'], 2039280)
        self.assertEqual(result['native_expense_lamports'], 105000)
        self.assertEqual(result['limit_violations'], [])

    def test_finalized_message_must_match_prepared_intent(self):
        raw = receipt()
        raw['_wire_transaction'] = base64.b64encode(b'\1' + signature_bytes(SIGNATURE) + b'different-message').decode()
        with self.assertRaises(ValueError):
            reconcile_receipt(raw, intent(), SIGNATURE)

    def test_failed_transaction_charges_fee_without_a_trade(self):
        result = reconcile_receipt(receipt(failed=True), intent(), SIGNATURE)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse(result['swap_recorded'])
        self.assertEqual(result['native_expense_lamports'], 105000)

    def test_unrelated_swap_or_missing_evidence_is_not_accepted(self):
        for change in ('wallet', 'pool', 'balances', 'signature', 'extra_outflow'):
            raw = receipt()
            if change == 'wallet': raw['transaction']['message']['accountKeys'][0]['pubkey'] = key(9)
            elif change == 'pool': raw['transaction']['message']['accountKeys'][1]['pubkey'] = key(9)
            elif change == 'balances': raw['meta']['preTokenBalances'] = None
            elif change == 'signature': raw['transaction']['signatures'] = ['wrong']
            else:
                raw['meta']['postBalances'][0] -= 1234
                result = reconcile_receipt(raw, intent(), SIGNATURE)
                self.assertIn('unclassified_native_outflow_requires_review', result['limit_violations'])
                continue
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                reconcile_receipt(raw, intent(), SIGNATURE)

    def test_actual_fill_recording_idempotency_and_time_matched_valuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Book(Path(tmp), initialize=True, mode='manual')
            try:
                result = reconcile_receipt(receipt(), intent(), SIGNATURE)
                valuation = {'sol_usd': '125', 'usdc_usd': '1', 'observed_at': NOW, 'source': 'isolated fixture'}
                record_reconciled(book, result, intent(), valuation)
                record_reconciled(book, result, intent(), valuation)
                self.assertEqual(len(book.fills()), 1)
                self.assertEqual(book.positions()[0]['remaining'], '24')
                self.assertEqual(D(book.positions()[0]['realized_pnl']), D('-.013125'))
                with self.assertRaises(ValueError):
                    record_reconciled(book, result, intent(), {**valuation, 'observed_at': NOW + 86400})
                failed = reconcile_receipt(receipt(failed=True), intent(), SIGNATURE)
                failed['signature'] = '6' * 88
                record_reconciled(book, failed, intent(), valuation)
                record_reconciled(book, failed, intent(), valuation)
                self.assertEqual(len(book.fills()), 2)
                self.assertEqual(len(book.positions()), 1)
            finally:
                book.close()

    def test_confirmed_full_sale_and_rent_refund_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Book(Path(tmp), initialize=True, mode='manual')
            try:
                valuation = {'sol_usd': '125', 'usdc_usd': '1', 'observed_at': NOW, 'source': 'isolated fixture'}
                record_reconciled(book, reconcile_receipt(receipt(), intent(), SIGNATURE), intent(), valuation)
                sell_intent = {**intent(), 'side': 'sell', 'amount_raw': '24000000', 'quoted_output_raw': '30000000', 'minimum_output_raw': '29700000'}
                raw = receipt(); raw['blockTime'] = NOW + 60
                signature = '6' * 88
                raw['transaction']['signatures'] = [signature]
                raw['_wire_transaction'] = base64.b64encode(b'\1' + signature_bytes(signature) + b'unsigned-message-fixture').decode()
                raw['meta']['preBalances'] = [997855720, 100, 2039280, 2039280]
                raw['meta']['postBalances'] = [999790000, 100, 2039280, 0]
                pre = raw['meta']['postTokenBalances']
                pre[0]['uiTokenAmount']['amount'] = '75000000'
                raw['meta']['preTokenBalances'] = pre
                raw['meta']['postTokenBalances'] = [copy.deepcopy(pre[0])]
                raw['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '105000000'
                result = reconcile_receipt(raw, sell_intent, signature)
                record_reconciled(book, result, sell_intent, {**valuation, 'observed_at': NOW + 60})
                record_reconciled(book, result, sell_intent, {**valuation, 'observed_at': NOW + 60})
                self.assertEqual(book.positions(), [])
                self.assertEqual(sum(D(f['pnl']) for f in book.fills()), D('4.97375'))
                self.assertEqual(book.positions(all_positions=True)[0]['rent_capital_reserved_usd'], '0')
            finally:
                book.close()


if __name__ == '__main__':
    unittest.main()
