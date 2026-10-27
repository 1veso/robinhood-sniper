import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import monitor
from sniper.adapters.evm import RpcClient, CHAIN_ID
from sniper.alerts import qualify, reviewed_wallets
from sniper.policy import RiskPolicy
from sniper.telegram import AlertStore
from sniper.models import SafetyEvidence
from test_evm import FakeChain, TOKEN, POOL, WALLET, USDG, bh


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.policy = RiskPolicy()
        self.asset = f"eip155:4663/erc20:{TOKEN}"
        self.wallets = {WALLET: {"qualified": True, "cluster_id": "reviewed-a", "qualification_evidence": "fixture-review"}}
        self.pool = {"canonical": True, "chain_id": CHAIN_ID, "chain_timestamp": 900,
                     "pool": POOL, "token0": TOKEN, "token1": USDG, "block_number": 101, "block_hash": bh(101)}
        self.buy = {"canonical": True, "kind": "verified_buy", "chain_id": CHAIN_ID, "chain_timestamp": 990,
                    "asset_id": self.asset, "pool_id": POOL, "qualified": True, "verified_swap": True,
                    "wallet": WALLET, "cluster_id": "reviewed-a", "qualification_evidence": "fixture-review",
                    "amount": "20", "spent_amount": "10", "quote_address": USDG, "block_number": 102,
                    "block_hash": bh(102), "tx_hash": "0x" + "22" * 32, "event_id": "buy-a", "symbol": "<b>FAKE</b>"}
        self.safety = {"asset_id": self.asset, "observed_at": 995,
                       "checks": {k: True for k in self.policy.required_safety_checks}, "source": "offline-fixture"}
        self.onchain = {"asset_id": self.asset, "observed_at": 995, "checks": {"contract_present": True}}

    def result(self):
        return qualify(self.pool, [self.buy], self.wallets, self.safety, self.onchain, self.policy, 1000)

    def test_all_gates_produce_information_only_message(self):
        result, reason = self.result()
        self.assertIsNotNone(result)
        self.assertIn("No order placed", result["message"])
        self.assertEqual(len(result["anchors"]), 2)
        self.assertLessEqual(result["expires_at"], 1295)

    def test_every_missing_failed_stale_or_future_safety_gate_blocks(self):
        original = copy.deepcopy(self.safety)
        for key in self.policy.required_safety_checks:
            for value in (None, False):
                self.safety = copy.deepcopy(original)
                self.safety["checks"][key] = value
                self.assertIsNone(self.result()[0])
        for timestamp in (699, 1001):
            self.safety = {**original, "observed_at": timestamp}
            self.assertIsNone(self.result()[0])

    def test_holdings_transfers_wrong_pool_reorg_and_changed_wallet_review_cannot_alert(self):
        original = dict(self.buy)
        for key, value in (("kind", "transfer"), ("verified_swap", False), ("qualified", False),
                           ("canonical", False), ("pool_id", "other-pool"), ("qualification_evidence", "other-review"),
                           ("chain_timestamp", 1001), ("amount", "NaN"), ("spent_amount", "0")):
            self.buy = {**original, key: value}
            self.assertIsNone(self.result()[0], key)
        self.buy = original
        self.wallets = {}
        self.assertIsNone(self.result()[0])

    def test_examples_and_unreviewed_wallets_are_never_promoted(self):
        self.assertEqual(reviewed_wallets({WALLET: {"qualified": False}}), {})
        self.assertEqual(reviewed_wallets({WALLET: {"qualified": True, "cluster_id": "a"}}), {})

    def test_expired_controller_review_is_rejected_even_in_fresh_manifest(self):
        record = {"qualified": True, "cluster_id": "a", "qualification_evidence": {
            "history_cutoff": 500, "controller_review": {"reviewed_at": 800, "expires_at": 999,
            "chain_id": 4663, "independence_reviewed": True, "history_cutoff": 500}}}
        self.assertEqual(reviewed_wallets({WALLET: record}, now=1000), {})
        record["qualification_evidence"]["controller_review"]["expires_at"] = 1100
        self.assertEqual(len(reviewed_wallets({WALLET: record}, now=1000)), 1)


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        self.now = int(time.time())
        self.chain = FakeChain()
        self.chain.head = 115
        self.rpc = RpcClient(transport=self.chain)
        self.wallets = {WALLET: {"qualified": True, "cluster_id": "reviewed-a", "qualification_evidence": "fixture-review"}}
        (self.runtime / "wallets.json").write_text(json.dumps(self.wallets))
        safety = {f"eip155:4663/erc20:{TOKEN}": {"asset_id": f"eip155:4663/erc20:{TOKEN}", "observed_at": self.now,
                   "checks": {k: True for k in RiskPolicy().required_safety_checks}, "source": "offline-fixture"}}
        (self.runtime / "safety.json").write_text(json.dumps(safety))

    def cycle(self):
        with patch("test_evm.NOW", self.now):
            return monitor.cycle(self.runtime, rpc=self.rpc)

    def test_complete_discovery_receipt_safety_queue_and_restart_without_real_network(self):
        first = self.cycle()
        self.assertEqual(first["telegram"], "SETUP_REQUIRED")
        self.assertEqual(first["alerts_queued"], 1)
        self.assertEqual(first["outbox"], {"pending": 1})
        second = self.cycle()
        self.assertEqual(second["alerts_queued"], 0)
        self.assertEqual(second["outbox"], {"pending": 1})
        self.assertFalse((self.runtime / "observation-001.sqlite3").exists())

    def test_live_screening_persists_failure_and_cannot_be_overridden_by_manifest(self):
        asset = f"eip155:4663/erc20:{TOKEN}"
        evidence = SafetyEvidence(asset, self.now, {'contract_controls': False}, 'offline-provider-fixture')
        with patch('test_evm.NOW', self.now), patch.object(monitor, 'GoPlusClient') as client:
            client.return_value.screen.return_value = evidence
            result = monitor.cycle(self.runtime, rpc=self.rpc, live_screening=True)
        self.assertEqual(result['alerts_queued'], 0)
        saved = json.loads((self.runtime / 'safety-observations.json').read_text())
        self.assertFalse(saved[asset]['checks']['contract_controls'])

    def test_rpc_timeout_retains_cursor_and_later_cycle_recovers(self):
        self.cycle()
        def fail(_):
            raise TimeoutError("synthetic provider failure")
        self.rpc.transport = fail
        self.rpc.max_retries = 0
        failed = self.cycle()
        self.assertEqual(failed["health"], "DEGRADED")
        self.assertEqual(failed["sent_this_cycle"], 0)
        self.rpc.transport = self.chain
        recovered = self.cycle()
        self.assertEqual(recovered["health"], "OK")
        self.assertEqual(recovered["outbox"], {"pending": 1})

    def test_lost_outbox_cannot_reset_duplicate_protection(self):
        self.cycle()
        (self.runtime / "telegram/alerts.sqlite3").unlink()
        with self.assertRaisesRegex(ValueError, "journal is missing"):
            self.cycle()

    def test_stale_automatic_qualifications_are_not_followed(self):
        folder = self.runtime / "research"
        folder.mkdir()
        (folder / "qualified-wallets.json").write_text(json.dumps(self.wallets))
        (folder / "wallet-research.json").write_text(json.dumps({"updated_at": self.now - 301}))
        result = self.cycle()
        self.assertEqual(result["reviewed_wallets"], 0)
        self.assertEqual(result["alerts_queued"], 0)
