import copy
import tempfile
from pathlib import Path
import unittest

from sniper.adapters.wallet_history import ResearchCache, ResearchPending, reconstruct_wallet
from sniper.adapters.evm import USDG, WETH, FACTORY
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
