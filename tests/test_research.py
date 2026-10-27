import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import research
from sniper.adapters.evm import RpcError


class ResearchIntegrationTests(unittest.TestCase):
    def test_missing_archive_state_stops_before_spending_history_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            folder = runtime / "research"
            folder.mkdir()
            (runtime / "blockscout-api-key.txt").write_text("test-key-not-real")
            snapshot = {"block": 105, "timestamp": int(time.time()), "hash": "0x" + "ab" * 32, "start_block": 10}
            (folder / "history-snapshot.json").write_text(json.dumps(snapshot))
            wallet = "0x" + "12" * 20
            state = {"candidates": {wallet: {"wallet": wallet, "first_seen_at": 1,
                                            "sampled_transactions": 1}}}
            rpc = Mock()
            rpc.url = "https://rpc.example"
            rpc.call.return_value = {"hash": snapshot["hash"]}
            cache = Mock()
            cache.call.side_effect = RpcError("historical state is not available")
            with patch.object(research, "discover", return_value=(state, {})), patch.object(
                    research, "RpcClient", return_value=rpc), patch.object(
                    research, "ResearchCache", return_value=cache), patch.object(
                    research, "reconstruct_wallet") as history:
                result = research.run_research(runtime)
            history.assert_not_called()
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["approved_wallet_count"], 0)
            self.assertTrue(any("ARCHIVE_RPC_REQUIRED" in x for x in result["pending"]))
            self.assertTrue((folder / "wallet-candidates.csv").exists())

    def test_wrong_archive_chain_keeps_discovery_but_blocks_history_and_qualification(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "blockscout-api-key.txt").write_text("test-key-not-real")
            (runtime / "rpc-url.txt").write_text("https://archive.example/private-key")
            (runtime / "observation-rpc-url.txt").write_text("https://observe.example")
            wallet = "0x" + "12" * 20
            state = {"candidates": {wallet: {"wallet": wallet, "first_seen_at": 1,
                                            "sampled_transactions": 1}}}
            archive = Mock(url="https://archive.example/private-key")
            archive.verify_chain.side_effect = RpcError("wrong chain: expected 4663, received 1")
            observation = Mock(url="https://observe.example")
            with patch.object(research, "discover", return_value=(state, {"new_candidates": 1})), patch.object(
                    research, "RpcClient", side_effect=[archive, observation]), patch.object(
                    research, "reconstruct_wallet") as history:
                result = research.run_research(runtime)
            archive.verify_chain.assert_called_once()
            history.assert_not_called()
            self.assertEqual(result["discovery"]["new_candidates"], 1)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["approved_wallet_count"], 0)
            self.assertTrue(any("wrong chain" in reason for reason in result["pending"]))
            self.assertEqual(result["rpc_hosts"], {"discovery": "observe.example", "history": "archive.example"})
            self.assertNotIn("private-key", (runtime / "research/wallet-research.json").read_text())
