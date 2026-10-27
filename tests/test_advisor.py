import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sniper.advisor import AdvisoryError, DailyBudget, GrokAdvisor, validate_advice, response_text

NOW = 1793026000


def advice():
    return {"label": "CAUTION", "confidence": 0.8, "summary": "Evidence warrants investigation", "main_risk": "Unverified identity", "claims": [{"text": "Source reports unknown identity", "evidence_ids": ["source1"]}]}


def evidence():
    return [{"id": "source1", "url": "https://example.com/source", "observed_at": NOW - 1, "text": "Identity unknown"}]


class AdvisorTests(unittest.TestCase):
    def test_missing_completion_status_is_not_success(self):
        for status in (None, "incomplete", "failed", "in_progress"):
            with self.subTest(status=status), self.assertRaises(AdvisoryError):
                response_text({"status": status, "output_text": json.dumps(advice())})

    def test_billed_cost_above_reservation_blocks_further_calls(self):
        with tempfile.TemporaryDirectory() as d:
            budget = DailyBudget(Path(d)/"budget.db", Decimal("1"))
            try:
                budget.reserve("one", Decimal("0.10"), NOW)
                budget.record_usage("one", {"usage": {"cost_in_usd_ticks": 9500000000}})
                with self.assertRaisesRegex(AdvisoryError, "budget exhausted"):
                    budget.reserve("two", Decimal("0.10"), NOW)
            finally:
                budget.close()

    def test_missing_billed_cost_blocks_more_requests_pending_reconciliation(self):
        with tempfile.TemporaryDirectory() as d:
            budget = DailyBudget(Path(d)/"budget.db")
            try:
                budget.reserve("one", Decimal("0.10"), NOW)
                budget.record_usage("one", {"usage": {"cost_in_usd_ticks": True}})
                with self.assertRaisesRegex(AdvisoryError, "Unverified prior API cost"):
                    budget.reserve("two", Decimal("0.10"), NOW)
            finally:
                budget.close()
    def test_strict_json_blocks_prose_and_nonfinite(self):
        for text in ("Here's JSON: " + json.dumps(advice()), json.dumps(advice()).replace("0.8", "NaN"),
                     json.dumps(advice()).replace('"confidence": 0.8', '"confidence": true'),
                     json.dumps(advice()).replace('"confidence": 0.8', '"confidence": "0.8"')):
            with self.subTest(text=text), self.assertRaises(AdvisoryError):
                validate_advice(text, {"source1"})

    def test_buy_not_an_advisory_label(self):
        raw = advice()
        raw["label"] = "BUY"
        with self.assertRaises(AdvisoryError):
            validate_advice(json.dumps(raw), {"source1"})

    def test_unknown_reference_rejected(self):
        with self.assertRaises(AdvisoryError):
            validate_advice(json.dumps(advice()), {"other"})

    def test_duplicates_rejected(self):
        text = json.dumps(advice()).replace('"confidence": 0.8', '"confidence": 0.8, "confidence": 0.1')
        with self.assertRaises(AdvisoryError):
            validate_advice(text, {"source1"})

    def test_disabled_never_reads_key_or_calls_network(self):
        def forbidden(*args):
            raise AssertionError("network called")
        result = GrokAdvisor(transport=forbidden).assess(request_id="r", asset_id="solana:x", evidence=[], now=NOW)
        self.assertEqual(result["label"], "UNKNOWN")

    def test_budget_is_persistent_and_duplicate_proof(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "budget.db"
            budget = DailyBudget(path, Decimal("0.20"))
            budget.reserve("one", Decimal("0.10"), NOW)
            budget.close()
            budget = DailyBudget(path, Decimal("0.20"))
            with self.assertRaises(AdvisoryError):
                budget.reserve("one", Decimal("0.10"), NOW)
            budget.reserve("two", Decimal("0.10"), NOW)
            with self.assertRaises(AdvisoryError):
                budget.reserve("three", Decimal("0.10"), NOW)
            budget.close()

    def test_complete_response_and_no_search_tools(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XAI_API_KEY": "test-key-not-real"}):
            requests = []
            def transport(method, url, body, headers, timeout):
                requests.append(body)
                return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(advice())}]}]}
            client = GrokAdvisor(enabled=True, budget_path=Path(d)/"budget.db", transport=transport)
            result = client.assess(request_id="r1", asset_id="solana:x", evidence=evidence(), now=NOW)
            self.assertEqual(result["label"], "CAUTION")
            self.assertTrue(result["advisory_only"])
            self.assertNotIn("tools", requests[0])
            self.assertLessEqual(requests[0]["max_output_tokens"], 1000)
            self.assertNotIn("test-key-not-real", json.dumps(requests[0]))

    def test_failed_request_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XAI_API_KEY": "test-key-not-real"}):
            def fail(*args):
                raise TimeoutError("timeout")
            client = GrokAdvisor(enabled=True, daily_limit_usd=Decimal(".10"), budget_path=Path(d)/"budget.db", transport=fail)
            with self.assertRaises(TimeoutError):
                client.assess(request_id="a", asset_id="a", evidence=evidence(), now=NOW)
            with self.assertRaises(AdvisoryError):
                client.assess(request_id="b", asset_id="a", evidence=evidence(), now=NOW)

    def test_future_evidence_blocked_before_network(self):
        raw = evidence()
        raw[0]["observed_at"] = NOW + 1
        with self.assertRaises(AdvisoryError):
            GrokAdvisor(enabled=True).assess(request_id="x", asset_id="a", evidence=raw, now=NOW)


if __name__ == "__main__":
    unittest.main()
