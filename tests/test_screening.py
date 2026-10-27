from pathlib import Path
import unittest
from unittest.mock import Mock

from sniper.screening import ScreenRules, screen_solana
from test_solana import MINT, NOW, report, pairs


class ScreeningTests(unittest.TestCase):
    def screen(self, raw_report=None, raw_pairs=None, **kwargs):
        self.rugcheck = Mock()
        self.rugcheck.raw_report.return_value = report() if raw_report is None else raw_report
        self.market = Mock()
        data = pairs() if raw_pairs is None else raw_pairs
        if raw_pairs is None:
            for pair in data:
                pair.update(pairCreatedAt=(NOW - 1800) * 1000, txns={"h1": {"buys": 20, "sells": 1}})
        self.market.pairs.return_value = data
        return screen_solana(MINT, rugcheck=self.rugcheck, market=self.market, clock=lambda: NOW, **kwargs)

    def test_matching_safety_pool_is_used_even_if_another_pool_is_deeper(self):
        result = self.screen()
        self.assertEqual(result["quote"]["pool_id"], "pool-one")
        self.assertTrue(result["deterministic_checks_passed"])
        self.assertEqual(len(result["checks"]), 14)
        self.assertFalse(result["entry_eligible"])
        self.assertFalse(result["quote"]["executable"])
        self.assertEqual(result["verdict"], "SKIP")
        self.assertEqual(result["checks"][-1]["status"], "UNKNOWN")

    def test_first_failed_fact_prevents_market_and_paid_research(self):
        raw = report()
        raw["rugged"] = True
        result = self.screen(raw)
        self.assertEqual(result["reason"], "not_rugged:failed")
        self.market.pairs.assert_not_called()
        self.assertTrue(all(x["status"] == "NOT_RUN" for x in result["checks"][1:]))

    def test_caller_cannot_switch_away_from_safety_pool(self):
        result = self.screen(pool_id="pool-two")
        self.assertIn("pool_mismatch", result["reason"])
        self.market.pairs.assert_not_called()

    def test_age_and_missing_activity_are_never_filled_with_zero(self):
        for age, txns, expected in [(1799, {"h1": {"buys": 20, "sells": 1}}, "pool_age"),
                                    (1800, {"h1": {"buys": 20}}, "hourly_sell_activity"),
                                    (1800, {"h1": {"buys": True, "sells": 0}}, "hourly_sell_activity"),
                                    (1800, {"h1": {"buys": 20, "sells": 0}}, "hourly_sell_activity"),
                                    (-1, {"h1": {"buys": 20, "sells": 1}}, "pool_age")]:
            with self.subTest(age=age, txns=txns):
                data = pairs()
                data[0].update(pairCreatedAt=(NOW-age)*1000, txns=txns)
                result = self.screen(raw_pairs=data)
                self.assertTrue(result["reason"].startswith(expected))
                self.assertFalse(result["deterministic_checks_passed"])

    def test_missing_matching_pool_never_substitutes_another(self):
        result = self.screen(raw_pairs=pairs()[1:])
        self.assertNotIn("quote", result)
        self.assertFalse(result["deterministic_checks_passed"])

    def test_research_config_is_separate_and_validated(self):
        path = Path(__file__).resolve().parents[1] / "config/solana-screen.toml"
        self.assertEqual(ScreenRules.from_file(path), ScreenRules())
        for kwargs in ({"reference_ticket_usd": "NaN"}, {"max_ticket_share": True}, {"min_age_seconds": False}):
            with self.assertRaises(ValueError):
                ScreenRules(**kwargs)
