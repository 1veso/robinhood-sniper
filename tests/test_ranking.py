import unittest

from sniper.ranking import rank_traders

NOW = 1793026000


def trader(wallet="a", share="0.2"):
    return {"wallet": wallet, "verified_history": True, "source": "reconciled-ledger", "observed_at": NOW - 1,
            "closed_trades": 60, "win_rate": "0.60", "top_coin_share": share,
            "pnl_7d_usd": "100", "pnl_30d_usd": "200", "avg_hold_minutes": "120", "followers": 50}


class RankingTests(unittest.TestCase):
    def test_deterministic_ranking(self):
        result = rank_traders([trader("a", "0.3"), trader("b", "0.1")], decision_at=NOW)
        self.assertEqual([r["wallet"] for r in result["leaders"]], ["b", "a"])
        self.assertEqual(result, rank_traders([trader("a", "0.3"), trader("b", "0.1")], decision_at=NOW))

    def test_unverified_and_future_history_excluded(self):
        a, b = trader("a"), trader("b")
        a["verified_history"] = False
        b["observed_at"] = NOW + 1
        result = rank_traders([a, b], decision_at=NOW)
        self.assertFalse(result["leaders"])
        self.assertEqual(len(result["rejected"]), 2)

    def test_nan_and_boolean_rejected(self):
        for value in ("NaN", float("inf"), True):
            raw = trader()
            raw["win_rate"] = value
            self.assertFalse(rank_traders([raw], decision_at=NOW)["leaders"])

    def test_concentration_and_short_holds_rejected(self):
        a, b = trader("a", "0.9"), trader("b")
        b["avg_hold_minutes"] = 1
        result = rank_traders([a, b], decision_at=NOW)
        self.assertFalse(result["leaders"])

    def test_duplicate_wallet_is_not_an_extra_leader(self):
        result = rank_traders([trader(), trader()], decision_at=NOW)
        self.assertEqual(len(result["leaders"]), 1)
        self.assertEqual(len(result["rejected"]), 1)


if __name__ == "__main__":
    unittest.main()
