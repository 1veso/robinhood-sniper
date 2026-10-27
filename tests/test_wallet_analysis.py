import copy
from decimal import Decimal as D
import unittest

from sniper.wallet_analysis import analyse_wallet, cluster_controllers, apply_controller_reviews
from sniper.adapters.wallet_history import historical_price

NOW = 1800000000


def history(records=None):
    return {"wallet": "0x" + "12" * 20, "cutoff": NOW, "complete": True, "gaps": [],
            "records": records or [], "funders": ["0x" + "34" * 20],
            "is_eoa": True, "controller_coverage_complete": True}


def record(n, side, quantity="10", value="100", token="asset-a", gas="1"):
    return {"id": str(n), "kind": side, "timestamp": NOW - 100000 + n * 3600,
            "block_number": n, "transaction_index": 0, "gas_usd": gas,
            "token": token, "quantity": quantity, "value_usd": value, "verified": True}


class WalletAnalysisTests(unittest.TestCase):
    def test_partial_sales_are_one_position_and_gas_is_charged_once(self):
        h = history([record(1, "BUY"), record(2, "SELL", "5", "70"), record(3, "SELL", "5", "70")])
        r = analyse_wallet(h)
        self.assertEqual(r["closed_positions"], 1)
        self.assertEqual(D(r["realized_pnl_usd"]), D(37))
        self.assertEqual(D(r["last_week_realized_less_open_basis_usd"]), D(37))
        self.assertEqual(D(r["gas_usd"]), D(3))
        self.assertEqual(D(r["net_pnl_after_follower_stress_usd"]), D("34.6"))

    def test_open_loser_cannot_be_hidden_by_realized_winners(self):
        h = history([record(1, "BUY"), record(2, "SELL", value="150"), record(3, "BUY", value="200", token="loser")])
        r = analyse_wallet(h)
        self.assertEqual(D(r["open_cost_basis_usd"]), D(201))
        self.assertEqual(D(r["net_pnl_with_open_inventory_stress_usd"]), D(-153))
        self.assertFalse(r["performance_qualified"])

    def test_airdrop_sale_is_not_profit_with_zero_basis(self):
        transfer = record(1, "TRANSFER", gas="0")
        transfer["tokens"] = ["asset-a"]
        r = analyse_wallet(history([transfer, record(2, "SELL", value="10000")]))
        self.assertEqual(r["closed_positions"], 0)
        self.assertLess(D(r["realized_pnl_usd"]), 0)
        self.assertIn("sale_without_reconciled_basis:asset-a", r["qualification_blockers"])

    def test_duplicate_and_future_history_is_rejected(self):
        with self.assertRaises(ValueError):
            analyse_wallet(history([record(1, "BUY"), record(1, "BUY")]))
        future = record(1, "BUY")
        future["timestamp"] = NOW + 1
        with self.assertRaises(ValueError):
            analyse_wallet(history([future]))

    def test_complete_winning_history_passes_performance_but_not_identity(self):
        records = []
        for i in range(20):
            buy = record(2*i+1, "BUY", token=f"asset-{i%10}")
            sell = record(2*i+2, "SELL", value="130", token=f"asset-{i%10}")
            buy["timestamp"] = NOW - 200000 + (2*i+1)*3600
            sell["timestamp"] = NOW - 200000 + (2*i+2)*3600
            records += [buy, sell]
        r = analyse_wallet(history(records))
        self.assertTrue(r["performance_qualified"], r["qualification_blockers"])
        self.assertFalse(r["qualified"])
        h = history(records)
        h["complete"] = False
        self.assertFalse(analyse_wallet(h)["performance_qualified"])

    def test_shared_funding_is_clustered_transitively(self):
        a = analyse_wallet(history())
        b, c = copy.deepcopy(a), copy.deepcopy(a)
        b["wallet"], c["wallet"] = "wallet-b", "wallet-c"
        c["funders"] = [b["wallet"]]
        result = cluster_controllers([a, b, c])
        self.assertEqual(len({r["cluster_id"] for r in result}), 1)
        self.assertTrue(all(r["controller_status"] == "LINKED" for r in result))
        self.assertTrue(all(not r["independence_verified"] for r in result))

    def test_absent_funding_does_not_prove_independence(self):
        h = history()
        h["funders"] = []
        r = cluster_controllers([analyse_wallet(h)])[0]
        self.assertEqual(r["controller_status"], "UNKNOWN")

    def test_historical_price_never_uses_future_or_stale_quote(self):
        prices = [[100000, "1.1"], [200000, "999"]]
        self.assertEqual(historical_price(prices, 150), D("1.1"))
        with self.assertRaises(ValueError):
            historical_price(prices, 50)
        with self.assertRaises(ValueError):
            historical_price(prices, 10000)

    def test_controller_review_must_be_fresh_and_match_history_cutoff(self):
        r = cluster_controllers([analyse_wallet(history())])[0]
        r["performance_qualified"] = True
        review = {"chain_id": 4663, "history_cutoff": NOW, "reviewed_at": NOW - 100,
                  "expires_at": NOW + 100, "independence_reviewed": True,
                  "scope": "test identity review", "cluster_id": "controller-a",
                  "evidence_urls": ["https://example.org/review"]}
        self.assertEqual(len(apply_controller_reviews([r], {r["wallet"]: review}, now=NOW)), 1)
        review["expires_at"] = NOW - 1
        self.assertEqual(apply_controller_reviews([r], {r["wallet"]: review}, now=NOW), {})
        self.assertFalse(r["qualified"])
        review["expires_at"] = NOW + 100
        review["history_cutoff"] = NOW - 1
        self.assertEqual(apply_controller_reviews([r], {r["wallet"]: review}, now=NOW), {})

    def test_reviews_cannot_split_funding_linked_wallets(self):
        a, b = analyse_wallet(history()), analyse_wallet(history())
        b["wallet"] = "wallet-b"
        reports = cluster_controllers([a, b])
        reviews = {}
        for i, r in enumerate(reports):
            r["performance_qualified"] = True
            reviews[r["wallet"]] = {"chain_id": 4663, "history_cutoff": NOW,
                "reviewed_at": NOW, "expires_at": NOW + 10, "independence_reviewed": True,
                "scope": "test", "cluster_id": str(i), "evidence_urls": ["https://example.org"]}
        self.assertEqual(apply_controller_reviews(reports, reviews, now=NOW), {})
        self.assertTrue(all(r["controller_status"] == "REVIEW_CONFLICTS_WITH_FUNDING_LINKS" for r in reports))
