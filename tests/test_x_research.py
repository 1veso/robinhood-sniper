import json
import io
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from sniper.advisor import AdvisoryError, DailyBudget, response_usage
from sniper.x_research import XResearch, validate_x_response
from test_solana import MINT, NOW

URL = "https://x.com/example/status/123456789"


def response():
    text = json.dumps({"label": "CAUTION", "confidence": 0.6, "summary": "Source requires review",
                       "main_risk": "Unverified identity", "claims": [{"text": "A source makes a claim", "source_urls": [URL]}]})
    return {"status": "completed", "output": [
        {"type": "custom_tool_call", "name": "x_keyword_search", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": text,
           "annotations": [{"type": "url_citation", "url": URL}]}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                  "cost_in_usd_ticks": 280000000, "server_side_tool_usage_details": {
                      "x_posts_fetched": 4, "x_users_fetched": 2}}}


class XResearchTests(unittest.TestCase):
    def test_unfunded_account_is_reported_without_spending_or_leaking_team_details(self):
        def transport(*args):
            data = {"code": "permission-denied", "error": "Your team doesn't have credits or licenses. private-team-id"}
            raise urllib.error.HTTPError("https://api.x.ai/v1/models", 403, "Forbidden", {}, io.BytesIO(json.dumps(data).encode()))
        with patch.dict(os.environ, {"XAI_API_KEY": "secret-test-key"}):
            result = XResearch(transport=transport).check_access()
        self.assertEqual(result["access_status"], "CREDITS_REQUIRED")
        self.assertFalse(result["inference_requested"])
        self.assertNotIn("private-team-id", json.dumps(result))

    def test_auth_check_only_reads_models_and_never_reserves_or_runs_inference(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XAI_API_KEY": "secret-test-key"}):
            path = Path(temporary)/"budget.db"
            calls = []
            def transport(method, url, body, headers, timeout):
                calls.append((method, url, body))
                return {"data": [{"id": "grok-release", "aliases": ["grok-4.7"]}]}
            result = XResearch(budget_path=path, transport=transport).check_access()
            self.assertEqual(calls, [("GET", "https://api.x.ai/v1/models", None)])
            self.assertFalse(path.exists())
            self.assertTrue(result["authentication_verified"])
            self.assertTrue(result["model_available"])
            self.assertFalse(result["x_search_verified"])
            self.assertFalse(result["inference_requested"])

    def test_disabled_never_reads_key_or_calls_provider(self):
        with patch.dict(os.environ, {}, clear=True):
            result = XResearch(transport=lambda *args: self.fail("Network called")).assess(request_id="one", token="invalid")
        self.assertEqual(result["label"], "UNKNOWN")

    def test_completed_search_records_actual_cost_and_citations_without_trade_authority(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XAI_API_KEY": "secret-test-key"}):
            bodies = []
            def transport(method, url, body, headers, timeout):
                bodies.append(body)
                self.assertEqual(url, "https://api.x.ai/v1/responses")
                return response()
            result = XResearch(enabled=True, budget_path=Path(temporary)/"budget.db", transport=transport).assess(
                request_id="one", token=MINT, now=NOW)
            self.assertEqual(result["sources"], [{"id": "x1", "url": URL}])
            self.assertEqual(result["usage"]["billed_usd"], "0.028")
            self.assertEqual(result["usage"]["x_users_fetched"], 2)
            self.assertTrue(result["advisory_only"])
            self.assertEqual(bodies[0]["max_turns"], 2)
            self.assertNotIn("secret-test-key", json.dumps(result) + json.dumps(bodies))

    def test_incomplete_or_unsearched_or_invented_sources_are_rejected(self):
        cases = []
        data = response(); data.pop("status"); cases.append(data)
        data = response(); data["output"].pop(0); cases.append(data)
        data = response(); data["output"][1]["content"][0]["annotations"] = []; cases.append(data)
        data = response(); data["usage"]["server_side_tool_usage_details"]["x_posts_fetched"] = 0; cases.append(data)
        data = response(); data["usage"].pop("cost_in_usd_ticks"); cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(AdvisoryError):
                validate_x_response(data, key="secret-test-key", usage=response_usage(data))

    def test_unknown_with_zero_results_can_be_reported_without_inventing_sources(self):
        data = response()
        part = data["output"][1]["content"][0]
        part["text"] = json.dumps({"label": "UNKNOWN", "confidence": 0, "summary": "No matching posts",
                                   "main_risk": "Missing evidence", "claims": []})
        part["annotations"] = []
        data["usage"]["server_side_tool_usage_details"].update(x_posts_fetched=0, x_users_fetched=0)
        result = validate_x_response(data, key="secret-test-key", usage=response_usage(data))
        self.assertEqual(result["label"], "UNKNOWN")

    def test_rejected_output_still_records_its_billed_cost(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"XAI_API_KEY": "secret-test-key"}):
            data = response(); data["status"] = "incomplete"
            path = Path(temporary)/"budget.db"
            with self.assertRaises(AdvisoryError):
                XResearch(enabled=True, budget_path=path, transport=lambda *args: data).assess(request_id="one", token=MINT, now=NOW)
            budget = DailyBudget(path)
            try:
                self.assertEqual(budget.db.execute("SELECT billed_usd FROM advisory_usage").fetchone()[0], "0.028")
            finally:
                budget.close()
