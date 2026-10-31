import copy
import hashlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sniper.adapters.wallet_history import ResearchCache, ResearchPending, reconstruct_wallet
from sniper.adapters.evm import USDG, WETH, FACTORY, RpcError
from sniper.wallet_analysis import analyse_wallet
from test_evm import TOKEN, POOL, WALLET, TX_SWAP, NOW, word, bh, receipt

TX_SELL = "0x" + "99" * 32


class FakeHistory:
    def account_history(self, wallet, action, start, end):
        if action != "txlist":
            return []
        return [{"hash": TX_SWAP}, {"hash": TX_SELL}]

    def get(self, url):
        return {"prices": [[(NOW - 3600)*1000, "1"]]}

    def call(self, method, params, *, anchor):
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getTransactionCount":
            return "0x0" if params[1] == hex(99) else "0x2"
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return {"number": hex(block), "hash": bh(block), "timestamp": hex(NOW - 1000 + (block - 102)*100)}
        if method == "eth_getTransactionByHash":
            block = 102 if params[0] == TX_SWAP else 103
            return {"hash": params[0], "from": WALLET, "to": POOL, "value": "0x0", "blockHash": bh(block)}
        if method == "eth_getTransactionReceipt":
            r = receipt()
            r.update(gasUsed="0x5208", effectiveGasPrice="0x1", transactionIndex="0x0")
            if params[0] == TX_SELL:
                r.update(transactionHash=TX_SELL, blockNumber=hex(103), blockHash=bh(103))
                for log in r["logs"]:
                    log.update(transactionHash=TX_SELL, blockNumber=hex(103), blockHash=bh(103))
                r["logs"][0]["topics"][1:] = ["0x" + word(POOL), "0x" + word(WALLET)]
                r["logs"][0]["data"] = "0x" + word(14_000_000)
                r["logs"][1]["topics"][1:] = ["0x" + word(WALLET), "0x" + word(POOL)]
                r["logs"][2]["data"] = "0x" + word(2_000_000_000) + word(-14_000_000) + word(2**96) + word(123456) + word(-1)
            return r
        if method == "eth_call":
            to, data = params[0]["to"], params[0]["data"]
            if data == "0x0dfe1681": return "0x" + word(TOKEN)
            if data == "0xd21220a7": return "0x" + word(USDG)
            if data == "0xddca3f43": return "0x" + word(3000)
            if data.startswith("0x1698ee82") and to == FACTORY: return "0x" + word(POOL)
            if data == "0x313ce567": return "0x" + word(6 if to == USDG else 9)
            if data.startswith("0x70a08231"):
                return "0x" + word(0 if to == TOKEN else (100_000_000 if params[1] == hex(99) else 104_000_000))
        raise AssertionError((method, params))


class WalletHistoryTests(unittest.TestCase):
    def test_transient_pool_or_valuation_or_balance_failure_retries_without_terminal_rejection(self):
        snapshot = {'block': 105, 'timestamp': NOW, 'hash': bh(105), 'start_block': 100}
        for selector in ('0x0dfe1681', '0x313ce567', '0x70a08231'):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as tmp:
                class Interrupted(FakeHistory):
                    root = Path(tmp)
                    def call(self, method, params, *, anchor):
                        if method == 'eth_call' and params[0]['data'].startswith(selector):
                            raise RpcError('fixture provider unavailable')
                        return super().call(method, params, anchor=anchor)
                with self.assertRaises(ResearchPending):
                    reconstruct_wallet(Interrupted(), WALLET, snapshot, eligibility_only=True)
                class Recovered(FakeHistory):
                    root = Path(tmp)
                h = reconstruct_wallet(Recovered(), WALLET, snapshot, eligibility_only=True)
                self.assertTrue(h['complete'], h['gaps'])
                self.assertEqual([r['kind'] for r in h['records']], ['BUY', 'SELL'])

    def test_legacy_provider_failure_checkpoint_is_rechecked(self):
        from sniper.adapters.history_progress import HistoryProgress
        snapshot = {'block': 105, 'timestamp': NOW, 'hash': bh(105), 'start_block': 100}
        with tempfile.TemporaryDirectory() as tmp:
            p = HistoryProgress(Path(tmp), WALLET, snapshot)
            p.save('tx:' + TX_SWAP, {'gaps': ['pool_verification_unavailable:' + TX_SWAP]})
            p.close()
            class Cached(FakeHistory):
                root = Path(tmp)
            self.assertTrue(reconstruct_wallet(Cached(), WALLET, snapshot, eligibility_only=True)['complete'])
    def test_socket_timeout_is_retryable_and_does_not_cache_a_false_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp), None, blockscout_key='fixture-key')
            valid = {'status': '1', 'result': []}
            with patch('urllib.request.urlopen', side_effect=[TimeoutError('read timed out'), io.BytesIO(json.dumps(valid).encode())]):
                with self.assertRaises(ResearchPending):
                    cache.get('https://api.blockscout.com/v2/api?action=txlist')
                self.assertEqual(cache.get('https://api.blockscout.com/v2/api?action=txlist'), valid)

    def test_unsupported_history_can_reject_eligibility_without_inventing_partial_pnl(self):
        class Unsupported(FakeHistory):
            def call(self, method, params, *, anchor):
                if method == 'eth_call' and params[0]['data'].startswith('0x1698ee82'):
                    return '0x' + word(0)
                return super().call(method, params, anchor=anchor)
        snapshot = {'block': 105, 'timestamp': NOW, 'hash': bh(105), 'start_block': 100}
        history = reconstruct_wallet(Unsupported(), WALLET, snapshot, eligibility_only=True)
        self.assertEqual(history['assessment'], 'REJECTED_UNSUPPORTED_HISTORY')
        self.assertFalse(history['complete'])
        self.assertEqual(history['records'], [])
        self.assertEqual(history['transactions_reconciled_before_rejection'], 1)
        self.assertFalse(analyse_wallet(history)['performance_qualified'])

    def test_checkpoint_resumes_without_replaying_completed_receipts(self):
        snapshot = {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100}
        first_hash = min(TX_SWAP, TX_SELL)
        second_hash = max(TX_SWAP, TX_SELL)
        with tempfile.TemporaryDirectory() as tmp:
            class Interrupted(FakeHistory):
                root = Path(tmp)
                def call(self, method, params, *, anchor):
                    if method == 'eth_getTransactionReceipt' and params[0] == second_hash:
                        raise ResearchPending('cycle ended')
                    return super().call(method, params, anchor=anchor)
            with self.assertRaises(ResearchPending):
                reconstruct_wallet(Interrupted(), WALLET, snapshot)
            class Resumed(FakeHistory):
                root = Path(tmp)
                def account_history(self, *args):
                    raise AssertionError('complete indexes must not replay')
                def call(self, method, params, *, anchor):
                    if method in ('eth_getTransactionByHash', 'eth_getTransactionReceipt') and params[0] == first_hash:
                        raise AssertionError('completed receipt must not replay')
                    return super().call(method, params, anchor=anchor)
            resumed = reconstruct_wallet(Resumed(), WALLET, snapshot)
            expected = reconstruct_wallet(FakeHistory(), WALLET, snapshot)
            self.assertEqual(resumed, expected)

    def test_checkpoint_is_bound_to_wallet_and_snapshot(self):
        from sniper.adapters.history_progress import HistoryProgress
        with tempfile.TemporaryDirectory() as tmp:
            original = HistoryProgress(Path(tmp), WALLET, {'block': 100, 'hash': bh(100)})
            original.save('tx:test', {'verified': True})
            original.close()
            for wallet, snap in ((WALLET, {'block': 100, 'hash': bh(101)}),
                                 ('0x' + '77' * 20, {'block': 100, 'hash': bh(100)})):
                changed = HistoryProgress(Path(tmp), wallet, snap)
                try:
                    self.assertIsNone(changed.get('tx:test'))
                finally:
                    changed.close()

    def test_internal_transaction_hash_field_matches_live_blockscout_schema(self):
        class InternalHistory(FakeHistory):
            def account_history(self, wallet, action, start, end):
                if action == "txlistinternal":
                    return [{"transactionHash": TX_SWAP, "blockNumber": "102", "isError": "0", "value": "0"}]
                return super().account_history(wallet, action, start, end)
        h = reconstruct_wallet(InternalHistory(), WALLET, {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100})
        self.assertTrue(h["complete"], h["gaps"])
        self.assertEqual(len(h["records"]), 2)
        self.assertFalse(h["controller_coverage_complete"])

    def test_receipt_reconstruction_proves_buy_and_sell_and_reconciles_balances(self):
        h = reconstruct_wallet(FakeHistory(), WALLET, {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100})
        self.assertTrue(h["complete"], h["gaps"])
        self.assertEqual([r["kind"] for r in h["records"]], ["BUY", "SELL"])
        self.assertEqual([r["value_usd"] for r in h["records"]], ["10.000000", "14.000000"])
        r = analyse_wallet(h)
        self.assertEqual(r["closed_positions"], 1)

    def test_balance_discrepancy_blocks_history_completeness(self):
        class BadBalance(FakeHistory):
            def call(self, method, params, *, anchor):
                if method == "eth_call" and params[0]["to"] == TOKEN and params[0]["data"].startswith("0x70a08231") and params[1] == hex(105):
                    return "0x" + word(99)
                return super().call(method, params, anchor=anchor)
        h = reconstruct_wallet(BadBalance(), WALLET, {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100})
        self.assertFalse(h["complete"])
        self.assertIn("token_balance_history_does_not_reconcile:" + TOKEN, h["gaps"])

    def test_wrong_factory_cannot_create_verified_trades(self):
        class WrongFactory(FakeHistory):
            def call(self, method, params, *, anchor):
                if method == "eth_call" and params[0]["data"].startswith("0x1698ee82"):
                    return "0x" + word(0)
                return super().call(method, params, anchor=anchor)
        h = reconstruct_wallet(WrongFactory(), WALLET, {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100})
        self.assertFalse(h["complete"])
        self.assertFalse(any(r["verified"] for r in h["records"]))

    def test_untouched_old_inventory_is_not_hidden_from_open_losses(self):
        old_token = "0x" + "88" * 20
        class OldInventory(FakeHistory):
            def account_history(self, wallet, action, start, end):
                if action == "tokentx":
                    return [{"blockNumber": "50", "contractAddress": old_token, "hash": "0x" + "77" * 32}]
                return super().account_history(wallet, action, start, end)
            def call(self, method, params, *, anchor):
                if method == "eth_call" and params[0]["to"] == old_token:
                    return "0x" + word(100)
                return super().call(method, params, anchor=anchor)
        h = reconstruct_wallet(OldInventory(), WALLET, {"block": 105, "timestamp": NOW, "hash": bh(105), "start_block": 100})
        self.assertFalse(h["complete"])
        self.assertIn("opening_token_inventory_has_unknown_basis:" + old_token, h["gaps"])

    def test_repeated_page_is_not_complete_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp), None, blockscout_key="test")
            cache.get = lambda *args, **kwargs: {"status": "1", "result": [{"blockNumber": "100"}] * 100}
            with self.assertRaisesRegex(ValueError, "Repeated history page"):
                cache.account_history(WALLET, "txlist", 90, 110)

    def test_provider_error_is_not_empty_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp), None, blockscout_key="test")
            cache.get = lambda *args, **kwargs: {"status": "0", "message": "rate limited", "result": []}
            with self.assertRaises(ResearchPending):
                cache.account_history(WALLET, "txlist", 90, 110)

    def test_empty_history_messages_match_the_requested_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp), None, blockscout_key="test")
            for action, message in (("txlist", "No transactions found"),
                                    ("txlistinternal", "No internal transactions found"),
                                    ("tokentx", "No token transfers found")):
                with self.subTest(action=action):
                    cache.get = lambda *args, **kwargs: {"status": "0", "message": message, "result": []}
                    self.assertEqual(cache.account_history(WALLET, action, 90, 110), [])
            with self.assertRaises(ResearchPending):
                cache.account_history(WALLET, "txlistinternal", 90, 110)

    def test_provider_error_is_retried_after_recovery_instead_of_cached_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp), None, blockscout_key="test")
            url = "https://api.blockscout.com/v2/api?action=txlist"
            error = {"status": "0", "message": "rate limited", "result": []}
            valid = {"status": "1", "message": "OK", "result": [{"blockNumber": "100"}]}
            response = lambda data: io.BytesIO(json.dumps(data).encode())
            key = ["https", url]
            path = Path(tmp) / (hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest() + ".json")
            path.write_text(json.dumps(error))  # Legacy cached provider failure.
            with patch("urllib.request.urlopen", side_effect=[response(error), response(valid)]) as http:
                with self.assertRaises(ResearchPending):
                    cache.get(url)
                self.assertFalse(path.exists())
                self.assertEqual(cache.get(url), valid)
                self.assertEqual(cache.get(url), valid)
            self.assertEqual(http.call_count, 2)
