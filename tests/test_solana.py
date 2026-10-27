import copy
import unittest
from decimal import Decimal

from sniper.adapters.solana import (DexScreenerClient, RugcheckClient, SourceError,
                                    normalize_quote, normalize_report, validate_address)

MINT = "So11111111111111111111111111111111111111112"
NOW = 1793026000


def report():
    return {"mint": MINT, "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1000000},
            "rugged": False, "score_normalised": 5, "risks": [], "creatorBalance": 0,
            "totalHolders": 100, "graphInsidersDetected": 0,
            "topHolders": [{"address": f"holder-{i}", "pct": 1, "insider": False} for i in range(10)],
            "markets": [{"pubkey": "pool-one", "lp": {"baseUSD": 50000, "quoteUSD": 50000, "lpLockedPct": 99}}]}


def pairs():
    return [{"chainId": "solana", "baseToken": {"address": MINT}, "pairAddress": "pool-one",
             "priceUsd": "0.10", "liquidity": {"usd": 100000}},
            {"chainId": "solana", "baseToken": {"address": MINT}, "pairAddress": "pool-two",
             "priceUsd": "0.11", "liquidity": {"usd": 200000}}]


class SolanaTests(unittest.TestCase):
    def test_authority_absence_is_not_explicit_null(self):
        clean = report()
        evidence = normalize_report(MINT, clean, now=NOW)
        self.assertIs(evidence.checks["authority_revoked"], True)
        del clean["token"]["mintAuthority"]
        missing = normalize_report(MINT, clean, now=NOW)
        self.assertIsNone(missing.checks["authority_revoked"])
        clean["token"]["mintAuthority"] = "owner"
        self.assertIs(normalize_report(MINT, clean, now=NOW).checks["authority_revoked"], False)

    def test_screen_does_not_imply_safety_or_exitability(self):
        evidence = normalize_report(MINT, report(), now=NOW)
        for key in ("contract_controls", "sellability", "deployer_history"):
            self.assertIsNone(evidence.checks[key])
        self.assertTrue(evidence.checks["liquidity_controls"])
        self.assertTrue(evidence.checks["holder_distribution"])

    def test_missing_creator_and_insider_not_zero(self):
        raw = report()
        del raw["creatorBalance"]
        del raw["graphInsidersDetected"]
        evidence = normalize_report(MINT, raw, now=NOW)
        self.assertIsNone(evidence.checks["creator_holding"])
        self.assertIsNone(evidence.checks["no_insiders"])

    def test_partial_holder_report_unknown(self):
        raw = report()
        raw["topHolders"] = raw["topHolders"][:3]
        self.assertIsNone(normalize_report(MINT, raw, now=NOW).checks["holder_distribution"])

    def test_largest_pool_not_any_locked_pool(self):
        raw = report()
        raw["markets"].append({"pubkey": "larger", "lp": {"baseUSD": 500000, "quoteUSD": 500000, "lpLockedPct": 0}})
        evidence = normalize_report(MINT, raw, now=NOW)
        self.assertFalse(evidence.checks["liquidity_controls"])
        self.assertEqual(evidence.details["report_pool_id"], "larger")

    def test_malformed_and_nonfinite_numbers_reject(self):
        for value in (float("nan"), float("inf"), "NaN", True, -1):
            raw = report()
            raw["score_normalised"] = value
            with self.subTest(value=value), self.assertRaises(SourceError):
                normalize_report(MINT, raw, now=NOW)

    def test_lack_of_danger_list_unknown(self):
        raw = report()
        del raw["risks"]
        self.assertIsNone(normalize_report(MINT, raw, now=NOW).checks["no_danger_flags"])

    def test_quote_pool_identity_is_stable(self):
        quote = normalize_quote(MINT, pairs(), now=NOW, pool_id="pool-one")
        self.assertEqual(quote.price_usd, Decimal("0.10"))
        self.assertFalse(quote.executable)
        self.assertEqual(quote.pool_id, "pool-one")
        with self.assertRaises(SourceError):
            normalize_quote(MINT, pairs(), now=NOW, pool_id="missing")

    def test_solana_pool_identity_is_case_sensitive(self):
        raw = pairs()[:1]
        raw[0]["pairAddress"] = "CaseSensitivePool"
        with self.assertRaises(SourceError):
            normalize_quote(MINT, raw, now=NOW, pool_id="casesensitivepool")

    def test_mismatched_mint_and_chain_reject(self):
        raw = report()
        raw["mint"] = "wrong"
        with self.assertRaises(SourceError):
            normalize_report(MINT, raw, now=NOW)
        raw_pairs = pairs()
        for p in raw_pairs:
            p["chainId"] = "wrong"
        with self.assertRaises(SourceError):
            normalize_quote(MINT, raw_pairs, now=NOW)

    def test_missing_quote_price_rejects(self):
        raw = pairs()[:1]
        del raw[0]["priceUsd"]
        with self.assertRaises(SourceError):
            normalize_quote(MINT, raw, now=NOW)

    def test_robinhood_quote_namespace(self):
        address = "0x" + "ab" * 20
        raw = [{"chainId": "robinhood", "baseToken": {"address": address}, "pairAddress": "0x" + "cd" * 20,
                "priceUsd": "1.25", "liquidity": {"usd": 200000}}]
        calls = []
        def transport(*args):
            calls.append(args)
            return raw
        client = DexScreenerClient(transport=transport, chain_id="robinhoodchain")
        self.assertEqual(client.quote(address, now=NOW).asset_id, f"robinhood:{address}")
        self.assertIn("/tokens/v1/robinhood/", calls[0][1])

    def test_mint_rejects_path_injection(self):
        for addr in (MINT + "/../../", "0" * 44, "1" * 33):
            with self.assertRaises(SourceError):
                validate_address(addr)

    def test_client_is_read_only_get(self):
        calls = []
        def transport(method, url, body, headers, timeout):
            calls.append((method, url, body))
            return report()
        RugcheckClient(transport=transport).report(MINT, now=NOW)
        self.assertEqual(calls[0][0], "GET")
        self.assertIsNone(calls[0][2])


if __name__ == "__main__":
    unittest.main()
