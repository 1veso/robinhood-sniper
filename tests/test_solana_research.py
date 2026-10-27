from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import solana_research
from test_solana import MINT, NOW, report


class SolanaResearchTests(unittest.TestCase):
    def test_cadence_and_seen_once_survive_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market, rugcheck = Mock(), Mock()
            market.profiles.return_value = [MINT]
            raw = report(); raw["rugged"] = True
            rugcheck.raw_report.return_value = raw
            first = solana_research.scan(root, market=market, rugcheck=rugcheck, clock=lambda: NOW)
            self.assertEqual(first["screened_this_run"], 1)
            self.assertFalse(first["paid_ai_enabled"])
            second = solana_research.scan(root, market=market, rugcheck=rugcheck, clock=lambda: NOW + 60)
            self.assertEqual(second["status"], "NOT_DUE")
            third = solana_research.scan(root, market=market, rugcheck=rugcheck, clock=lambda: NOW + 1800)
            self.assertEqual(third["screened_this_run"], 0)
            self.assertEqual(third["total_screened"], 1)
            self.assertEqual(rugcheck.raw_report.call_count, 1)
            market.pairs.assert_not_called()

    def test_provider_outage_is_reported_without_inventing_an_empty_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            market = Mock(); market.profiles.side_effect = OSError("unavailable")
            result = solana_research.scan(Path(temporary), market=market, clock=lambda: NOW)
            self.assertEqual(result["status"], "DEGRADED")
            self.assertEqual(result["total_screened"], 0)
            self.assertIn("profile_source_unavailable", result["source_error"])
