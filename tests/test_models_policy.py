import dataclasses
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sniper.models import MarketQuote, SafetyEvidence, loads_strict
from sniper.policy import RiskPolicy


class DomainTests(unittest.TestCase):
    def test_no_nonfinite_money(self):
        for bad in ("NaN", "Infinity", "-Infinity", True):
            with self.assertRaises(ValueError):
                MarketQuote("demo:a", "pool", 1, bad, "50000")

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            loads_strict('{"paper_only":true,"paper_only":false}')

    def test_nonfinite_json_rejected(self):
        with self.assertRaises(ValueError):
            loads_strict('{"confidence":NaN}')

    def test_unknown_is_not_false_or_true(self):
        proof = SafetyEvidence("demo:a", 1, {"controls": None}, "fixture")
        self.assertIsNone(proof.checks["controls"])
        with self.assertRaises(ValueError):
            SafetyEvidence("demo:a", 1, {"controls": "true"}, "fixture")

    def test_live_policy_refused(self):
        with self.assertRaises(ValueError):
            RiskPolicy(paper_only=False)

    def test_policy_roundtrip_default_and_version(self):
        policy = RiskPolicy.from_file(Path(__file__).resolve().parents[1] / "config" / "paper.toml")
        self.assertEqual(policy.version, RiskPolicy().version)
        self.assertEqual(policy.min_liquidity_usd, Decimal("50000"))
        self.assertEqual(policy.min_independent_wallets, 5)
        self.assertEqual(policy.max_account_risk_pct, Decimal("2"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.paper_only = False


if __name__ == "__main__":
    unittest.main()
