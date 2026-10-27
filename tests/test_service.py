import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import supervise


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        shutil.copyfile(supervise.ROOT / "config/paper.toml", self.root / "config/paper.toml")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()

    def cycle(self, result=None, error=None):
        result = result or subprocess.CompletedProcess([], 0, json.dumps({
            "account": {"cash": "1000", "equity": "1000"},
            "source_errors": [], "collector": {"head": 105, "cursor": {"block_number": 100}}
        }), "")
        with patch.object(supervise, "ROOT", self.root), patch.object(
                supervise.subprocess, "run", return_value=result, side_effect=error):
            return supervise.run_cycle(self.runtime, "research", 1000, 180)

    def test_running_collector_never_makes_missing_evidence_eligible(self):
        result = self.cycle()
        self.assertEqual(result["health"], "OK")
        self.assertEqual(result["entry_status"], "BLOCKED")
        self.assertEqual(result["reviewed_clusters"], 0)
        self.assertEqual(len(result["entry_prerequisite_gaps"]), 3)
        self.assertEqual(json.loads((self.runtime / "wallets.json").read_text()), {})
        self.assertTrue((self.runtime / "account.html").exists())

    def test_timeout_retains_manifests_and_records_failure(self):
        wallet_file = self.runtime / "wallets.json"
        wallet_file.write_text('{"candidate": {"qualified": false}}')
        result = self.cycle(error=subprocess.TimeoutExpired("runner", 180))
        self.assertEqual(result["health"], "ERROR")
        self.assertEqual(result["return_code"], 124)
        self.assertEqual(result["entry_status"], "BLOCKED")
        self.assertEqual(json.loads(wallet_file.read_text()), {"candidate": {"qualified": False}})

    def test_private_rpc_is_not_written_to_status_or_logs(self):
        secret_url = "https://rpc.example/v2/private-key-for-test"
        observation_url = "https://observe.example/v2/other-private-key"
        (self.runtime / "rpc-url.txt").write_text(secret_url)
        (self.runtime / "observation-rpc-url.txt").write_text(observation_url)
        result = subprocess.CompletedProcess([], 2, json.dumps({"error": secret_url + observation_url}), "")
        self.cycle(result=result)
        for name in ("status.json", "service.jsonl", "account.html"):
            self.assertNotIn(secret_url, (self.runtime / name).read_text())
            self.assertNotIn(observation_url, (self.runtime / name).read_text())

    def test_observation_and_history_use_separate_configured_endpoints(self):
        history_url = "https://history.example/private-test-key"
        observation_url = "https://observe.example/current-test-key"
        (self.runtime / "rpc-url.txt").write_text(history_url)
        (self.runtime / "observation-rpc-url.txt").write_text(observation_url)
        calls = []

        def child(command, **kwargs):
            calls.append((Path(command[1]).name, kwargs["env"]["SNIPER_RPC_URL"]))
            return subprocess.CompletedProcess(command, 0, '{}', '')

        with patch.object(supervise, "ROOT", self.root), patch.object(supervise.subprocess, "run", side_effect=child):
            result = supervise.run_cycle(self.runtime, "research", 1000, 180)
        self.assertEqual(calls, [("run.py", observation_url), ("research.py", history_url)])
        self.assertEqual(result["entry_status"], "BLOCKED")

    def test_os_lock_blocks_overlap_and_releases_after_exit(self):
        path = self.runtime / "service.lock"
        with supervise.instance_lock(path):
            with self.assertRaises(OSError):
                with supervise.instance_lock(path):
                    self.fail("A second writer acquired the same lock")
        with supervise.instance_lock(path):
            pass
