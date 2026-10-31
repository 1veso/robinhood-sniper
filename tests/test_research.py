import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, MagicMock, patch

import research
from sniper.adapters.evm import RpcError


class ResearchIntegrationTests(unittest.TestCase):
    def test_new_wallet_gets_recent_anchor_and_resumes_it_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary); folder = runtime / 'research'; folder.mkdir()
            now = int(time.time()); old = {'block': 105, 'timestamp': now - 4 * 86400,
                'hash': '0x' + 'ab' * 32, 'start_block': 10}
            fresh = {'block': 205, 'timestamp': now - 5, 'hash': '0x' + 'cd' * 32, 'start_block': 20}
            (runtime / 'blockscout-api-key.txt').write_text('fixture-only')
            (folder / 'history-snapshot.json').write_text(json.dumps(old))
            wallet = '0x' + '11' * 20
            (folder / 'discovery.json').write_text(json.dumps({'updated_at': now, 'candidates': {
                wallet: {'wallet': wallet, 'first_seen_at': now - 10, 'last_seen_at': now - 10,
                         'sampled_transactions': 1}}}))
            rpc = MagicMock(url='https://rpc.example')
            def call(method, params=None):
                if method == 'eth_blockNumber': return hex(210)
                if method == 'eth_getBlockByNumber':
                    return {'hash': fresh['hash'], 'timestamp': hex(fresh['timestamp'])}
                raise AssertionError(method)
            rpc.call.side_effect = call
            cache = Mock(max_calls=48, remaining=0, progress={}, checkpoint_progress={})
            with patch.object(research, 'RpcClient', return_value=rpc), patch.object(
                    research, 'ResearchCache', return_value=cache), patch.object(
                    research, 'block_at_or_before', return_value=20), patch.object(
                    research, 'reconstruct_wallet', side_effect=research.ResearchPending('daily budget')) as reconstruct:
                result = research.run_research(runtime)
                self.assertEqual(reconstruct.call_args.args[2], fresh)
                self.assertEqual(result['approved_wallet_count'], 0)
                self.assertEqual(result['history_queue']['bound_unfinished_wallets'], 1)
                rpc.call.reset_mock()
                research.run_research(runtime)
                self.assertEqual(reconstruct.call_args.args[2], fresh)
                self.assertFalse(any(c.args[0] == 'eth_blockNumber' for c in rpc.call.call_args_list))
            self.assertEqual(json.loads((folder / 'history-snapshot.json').read_text()), old)

    def sender_cache(self, *, reorg=False, wrong_header=False):
        from test_evm import swap_log, receipt, bh, TX_SWAP, OTHER, NOW
        end_reads = [0]
        def rpc_call(method, params=None):
            if method == 'eth_blockNumber': return hex(110)
            if method == 'eth_getBlockByNumber':
                n = int(params[0], 16)
                if n == 105: end_reads[0] += 1
                return {'number':hex(n), 'hash':bh(n, reorg and n==105 and end_reads[0]>=3), 'timestamp':hex(NOW)}
            raise AssertionError(method)
        rpc = Mock()
        rpc.call.side_effect = rpc_call
        def cached_call(method, params, **kwargs):
            if method == 'eth_getLogs': return [swap_log()]
            if method == 'eth_getTransactionByHash': return {'hash':TX_SWAP,'from':OTHER,'blockNumber':hex(102),'blockHash':bh(102)}
            if method == 'eth_getTransactionReceipt': return receipt()
            if method == 'eth_getBlockByNumber': return {'number':hex(102),'hash':bh(102,wrong_header),'timestamp':hex(NOW)}
            raise AssertionError(method)
        cache=Mock(rpc=rpc)
        cache.call.side_effect=cached_call
        return cache

    def test_sender_discovery_records_canonical_proof_without_calling_sender_a_buyer(self):
        from test_evm import bh, OTHER, WALLET
        evidence=Mock()
        state, result=research.discover(self.sender_cache(), {'cursor':100,'cursor_hash':bh(100)},sample=3,evidence=evidence)
        self.assertIn(OTHER,state['candidates'])
        self.assertNotIn(WALLET,state['candidates'])
        self.assertFalse(state['candidates'][OTHER]['qualified'])
        observation=evidence.add_sender_observations.call_args.args[0][0]
        self.assertEqual(observation['role'],'transaction_sender')
        self.assertEqual(observation['block_header']['hash'],bh(102))
        self.assertEqual(result['sampled_transactions'],1)

    def test_failed_discovery_anchor_cannot_mutate_existing_counters_or_write_evidence(self):
        from test_evm import bh, OTHER
        state={'cursor':100,'cursor_hash':bh(100),'candidates':{OTHER:{'wallet':OTHER,'sampled_transactions':7,'last_seen_at':1,'qualified':False}}}
        before=json.loads(json.dumps(state))
        evidence=Mock()
        with self.assertRaises(research.ResearchPending):
            research.discover(self.sender_cache(reorg=True),state,sample=3,evidence=evidence)
        self.assertEqual(state,before)
        evidence.add_sender_observations.assert_not_called()

    def test_sender_discovery_crosses_legacy_capacity_with_same_bounded_sample(self):
        from test_evm import bh, OTHER
        candidates={'0x'+f'{i:040x}':{'wallet':'0x'+f'{i:040x}', 'qualified':False}
                    for i in range(1,5001)}
        state={'cursor':100,'cursor_hash':bh(100),'candidates':candidates}
        evidence=Mock()
        state,result=research.discover(self.sender_cache(),state,sample=3,evidence=evidence)
        self.assertEqual(len(state['candidates']),5001)
        self.assertEqual(result['new_candidates'],1)
        self.assertEqual(result['sampled_transactions'],1)
        self.assertFalse(state['candidates'][OTHER]['qualified'])
        self.assertEqual(len(evidence.add_sender_observations.call_args.args[0]),1)

    def test_sender_discovery_requires_individual_canonical_block(self):
        from test_evm import bh
        state={'cursor':100,'cursor_hash':bh(100)}
        evidence=Mock()
        with self.assertRaises(research.ResearchPending):
            research.discover(self.sender_cache(wrong_header=True),state,sample=3,evidence=evidence)
        self.assertNotIn('candidates',state)
        evidence.add_sender_observations.assert_not_called()

    def test_export_only_never_constructs_network_clients_or_reads_credentials(self):
        from test_evm import WALLET, TX_SWAP
        with tempfile.TemporaryDirectory() as temporary:
            runtime=Path(temporary); folder=runtime/'research';folder.mkdir()
            (folder/'discovery.json').write_text(json.dumps({'candidates':{WALLET:{'wallet':WALLET,'sampled_transactions':1,'first_seen_at':1,'example_transaction':TX_SWAP,'qualified':False}}}))
            with patch.object(research,'RpcClient',side_effect=AssertionError('network client')), patch.object(research,'child_environment',side_effect=AssertionError('secret read')):
                result=research.export_evidence(runtime)
            self.assertEqual(result['network_calls'],0)
            self.assertTrue((folder/'addresses'/f'{WALLET}.json').exists())
            self.assertTrue((folder/'wallet-evidence.csv').exists())

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
