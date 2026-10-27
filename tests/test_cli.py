from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.cli import main, validate_envelope
from sniper.demo import demo_candidate, demo_quote, DEMO_TIME
from sniper.policy import RiskPolicy
from sniper.reporting import write_html


class CliTests(unittest.TestCase):
    def invoke(self, args):
        stream = StringIO()
        with redirect_stdout(stream):
            result = main(args)
        return result, json.loads(stream.getvalue())

    def test_complete_demo_is_isolated_and_labels_synthetic_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "never-create.db"
            report = Path(directory) / "demo.html"
            code, result = self.invoke(["--db", str(ledger), "demo", "--html", str(report)])
            self.assertEqual(code, 0)
            self.assertFalse(ledger.exists())
            self.assertEqual(result["data_mode"], "SYNTHETIC_DEMO")
            self.assertEqual(result["report"]["closed_positions"], 2)
            self.assertEqual(result["report"]["wins"], 1)
            self.assertEqual(result["report"]["losses"], 1)
            self.assertIn("SYNTHETIC_DEMO", report.read_text())

    def test_doctor_needs_no_credentials_or_network(self):
        code, result = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        self.assertFalse(result["live_order_capability"])
        self.assertEqual(result["policy"]["min_independent_wallets"], 5)

    def test_rpc_environment_is_used_without_printing_api_key(self):
        secret_url = "https://rpc.example/v2/private-test-key"
        with patch.dict("os.environ", {"SNIPER_RPC_URL": secret_url}), patch(
                "sniper.adapters.evm.RpcClient") as client:
            client.return_value.call.return_value = "0x1237"
            code, result = self.invoke(["doctor", "--network"])
            client.assert_called_once_with(secret_url)
        self.assertEqual(code, 0)
        self.assertEqual(result["network"]["rpc_host"], "rpc.example")
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_live_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.toml"
            config.write_text("[risk]\npaper_only=false\n")
            code, result = self.invoke(["--config", str(config), "doctor"])
            self.assertEqual(code, 2)
            self.assertIn("PAPER", result["error"])

    def test_replay_rejects_reordering_before_writing_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text('{"kind":"marks","at":20,"quotes":[]}\n{"kind":"marks","at":10,"quotes":[]}\n')
            db = Path(directory) / "paper.db"
            code, result = self.invoke(["--db", str(db), "replay", "--input", str(path)])
            self.assertEqual(code, 2)
            self.assertFalse(db.exists())

    def test_duplicate_quote_assets_rejected(self):
        c = demo_candidate(RiskPolicy())
        q = demo_quote(c, at=DEMO_TIME).as_dict()
        with self.assertRaises(ValueError):
            validate_envelope({"kind": "marks", "at": DEMO_TIME, "quotes": [q, q]}, replay=True)

    def test_generated_report_escapes_token_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            report = {"positions": [{"symbol": "<script>alert(1)</script>", "asset_id": "a"}]}
            path = write_html(report, Path(directory) / "report.html")
            text = path.read_text()
            self.assertNotIn("<script>alert(1)</script>", text)
            self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
