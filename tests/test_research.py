import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, MagicMock, patch

import research
from sniper.adapters.evm import RpcError


class ResearchIntegrationTests(unittest.TestCase):
    def test_observed_buyers_are_imported_without_granting_qualification(self):
        from sniper.adapters.evm_store import EvmStore
        from sniper.confirmation_book import ConfirmationBook
        from test_paper_revision import proof, persist
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / 'exploration').mkdir()
            store = EvmStore(runtime / 'exploration/events.sqlite3')
            try:
                p = proof()
                persist(store, p)
                ConfirmationBook(store).record('buyer', p)
                state = {}
                self.assertEqual(research.import_observed_buyers(runtime, state), 1)
                candidate = state['candidates'][p['wallet']]
                self.assertTrue(candidate['observed_recipient'])
                self.assertFalse(candidate['qualified'])
                store.db.execute('UPDATE evm_events SET canonical=0')
                empty = {}
                self.assertEqual(research.import_observed_buyers(runtime, empty), 0)
            finally:
                store.close()

    def test_large_history_rotates_after_three_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            folder = runtime / 'research'
            cohort = folder / 'cohort-105'
            cohort.mkdir(parents=True)
            (runtime / 'blockscout-api-key.txt').write_text('fixture-key')
            snapshot = {'block': 105, 'timestamp': int(time.time()), 'hash': '0x'+'ab'*32, 'start_block': 10}
            (folder / 'history-snapshot.json').write_text(json.dumps(snapshot))
            first, active = '0x'+'11'*20, '0x'+'22'*20
            (cohort / 'progress.json').write_text(json.dumps({'active_wallet': active, 'consecutive_batches': 3}))
            (cohort / 'attempts.json').write_text(json.dumps({active: 100}))
            state = {'updated_at': int(time.time()), 'candidates': {
                w: {'wallet': w, 'first_seen_at': i, 'sampled_transactions': 1} for i,w in enumerate((first,active))}}
            (folder / 'discovery.json').write_text(json.dumps(state))
            rpc = MagicMock(url='https://rpc.example')
            rpc.call.return_value = {'hash': snapshot['hash']}
            cache = Mock(max_calls=48, remaining=0, progress={}, checkpoint_progress={})
            with patch.object(research, 'RpcClient', return_value=rpc), patch.object(research, 'ResearchCache', return_value=cache), patch.object(
                    research, 'reconstruct_wallet', side_effect=research.ResearchPending('bounded cycle')) as reconstruct:
                research.run_research(runtime)
            self.assertEqual(reconstruct.call_args.args[1], first)

    def test_history_retains_old_snapshot_and_resumes_active_wallet(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            folder = runtime / "research"
            folder.mkdir()
            (runtime / "blockscout-api-key.txt").write_text("test-key-not-real")
            snapshot = {"block": 105, "timestamp": int(time.time()) - 2 * 86400,
                        "hash": "0x" + "ab" * 32, "start_block": 10}
            (folder / "history-snapshot.json").write_text(json.dumps(snapshot))
            cohort = folder / "cohort-105"
            cohort.mkdir()
            first, active = "0x" + "11" * 20, "0x" + "22" * 20
            (cohort / "progress.json").write_text(json.dumps({"active_wallet": active}))
            state = {"updated_at": int(time.time()), "candidates": {
                w: {"wallet": w, "first_seen_at": i, "sampled_transactions": 1}
                for i, w in enumerate((first, active))}}
            (folder / "discovery.json").write_text(json.dumps(state))
            rpc = MagicMock(url="https://rpc.example")
            rpc.call.return_value = {"hash": snapshot["hash"]}
            with patch.object(research, "discover") as discover, patch.object(
                    research, "RpcClient", return_value=rpc), patch.object(
                    research, "ResearchCache") as cache, patch.object(
                    research, "reconstruct_wallet", side_effect=research.ResearchPending("Cycle budget reached")) as reconstruct:
                cache.return_value.max_calls = 48
                cache.return_value.remaining = 0
                cache.return_value.progress = {}
                cache.return_value.checkpoint_progress = {}
                result = research.run_research(runtime)
            discover.assert_not_called()
            self.assertEqual(reconstruct.call_args.args[1:], (active, snapshot))
            self.assertEqual(result["snapshot"], snapshot)
            self.assertEqual(cache.call_args.kwargs["seconds"], 35)
            self.assertEqual(cache.call_args.kwargs["max_calls"], 48)

    def test_dense_discovery_interval_retries_without_skipping_blocks(self):
        block_hash = "0x" + "ab" * 32
        rpc = Mock()
        rpc.call.side_effect = lambda method, params=None: (
            hex(5000) if method == "eth_blockNumber" else {"number": params[0], "hash": block_hash})
        cache = Mock(rpc=rpc)
        cache.call.side_effect = [RpcError("RPC eth_getLogs returned error -32000"), []]
        state, result = research.discover(cache, {"cursor": 100, "cursor_hash": block_hash})
        intervals = [(int(c.args[1][0]["fromBlock"], 16), int(c.args[1][0]["toBlock"], 16))
                     for c in cache.call.call_args_list]
        self.assertEqual(intervals, [(101, 2100), (101, 1100)])
        self.assertEqual(state["cursor"], 1100)
        self.assertEqual(result["to_block"], 1100)
        self.assertEqual(result["lag_blocks"], 4995 - 1100)

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
            rpc = MagicMock()
            rpc.url = "https://rpc.example"
            rpc.call.return_value = {"hash": snapshot["hash"]}
            cache = Mock(max_calls=48, remaining=47, progress={}, checkpoint_progress={})
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
            archive = MagicMock(url="https://archive.example/private-key")
            archive.verify_chain.side_effect = RpcError("wrong chain: expected 4663, received 1")
            observation = MagicMock(url="https://observe.example")
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
