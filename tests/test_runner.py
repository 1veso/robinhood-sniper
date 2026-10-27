"""Focused integration tests: source journal -> runner -> actual paper ledger.

Network clients are mocked; the pipeline, risk engine, and SQLite stores are real.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sniper import cli
from sniper.adapters.evm import DEFAULT_RPC
from sniper.adapters.evm_store import EvmStore
from sniper.engine import PaperEngine
from sniper.ledger import Ledger
from sniper.models import Candidate, MarketQuote, SafetyEvidence, WalletConfirmation
from sniper.pipeline import PaperPipeline
from sniper.policy import RiskPolicy

NOW = 1793030400
ASSET = "eip155:4663/erc20:0x" + "ab" * 20
POOL = "0x" + "cd" * 20
HASH = "0x" + "ef" * 32


def observations(count=5):
    return [{"kind": "verified_buy", "asset_id": ASSET, "pool_id": POOL, "symbol": "TEST",
             "event_id": f"source-buy-{i}", "wallet": "0x" + f"{i+1:040x}",
             "tx_hash": "0x" + f"{i+1:064x}", "log_index": i,
             "block_hash": HASH, "block_number": 100+i, "chain_id": 4663,
             "chain_timestamp": NOW-10, "observed_at": NOW-5,
             "cluster_id": f"reviewed-cluster-{i}", "verified_swap": True,
             "qualified": True, "qualification_evidence": "synthetic-dated-review", "spent_usd": "100"}
            for i in range(count)]


def fresh_quote(price="1", *, executable=True, now=NOW):
    return MarketQuote(ASSET, POOL, now, price, "1000000", gas_usd="0.01",
                       executable=executable, source="synthetic-runner-test")


def good_safety(policy, now=NOW):
    return SafetyEvidence(ASSET, now, {k: True for k in policy.required_safety_checks}, "synthetic-reviewed-evidence")


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = RiskPolicy()
        self.args = SimpleNamespace(
            config=None, db=str(self.root/"paper.sqlite3"), events_db=str(self.root/"events.sqlite3"),
            wallets=None, base_prices=str(self.root/"prices.json"), safety=None, quotes=None,
            rpc=DEFAULT_RPC, confirmations=5, start_block=None, max_blocks=500,
            follow=False, iterations=1, interval=1,
        )
        Path(self.args.base_prices).write_text("{}")
        self.outputs = []
        self.pipeline_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def seed_source(self, buys=None):
        buys = observations() if buys is None else buys
        store = EvmStore(self.args.events_db)
        try:
            store.commit_chunk(4663, buys, [{"number": 99, "hash": "0x" + "99" * 32}],
                               {"number": 104, "hash": HASH})
        finally:
            store.close()

    def set_evidence(self, *, safe=True, executable=True, price="1"):
        if safe:
            self.args.safety = str(self.root/"safety.json")
            Path(self.args.safety).write_text(json.dumps({ASSET: good_safety(self.policy).as_dict()}))
        self.args.quotes = str(self.root/"quotes.json")
        Path(self.args.quotes).write_text(json.dumps({ASSET: fresh_quote(price, executable=executable).as_dict()}))

    def ledger(self):
        return Ledger(self.args.db, self.policy.starting_cash_usd)

    def run_once(self, *, poll_error=None, polled_buys=None, market_quote=None, collector_status="complete"):
        calls = self.pipeline_calls
        class InstrumentedPipeline(PaperPipeline):
            def __init__(self, *args, **kwargs):
                kwargs["clock"] = lambda: NOW
                super().__init__(*args, **kwargs)
            def invalidate_after(self, *args, **kwargs):
                calls.append("invalidate")
                return super().invalidate_after(*args, **kwargs)
            def ingest(self, buys, *args, **kwargs):
                calls.append("ingest")
                return super().ingest(buys, *args, **kwargs)
        collector = Mock()
        collector.poll.side_effect = poll_error
        collector.poll.return_value = {
            "cursor": {"block_number": 104, "block_hash": HASH}, "head": 109,
            "tracked_wallets": 5, "reorg": None, "errors": [],
            "status": collector_status,
            "new_pools": [], "swaps": [], "verified_buys": polled_buys or [],
        }
        market = Mock()
        if market_quote is None:
            market.quote.side_effect = RuntimeError("test market unavailable")
        else:
            market.quote.return_value = market_quote
        with patch("sniper.adapters.evm.RpcClient"), \
             patch("sniper.adapters.evm.EvmCollector", return_value=collector), \
             patch("sniper.adapters.solana.DexScreenerClient", return_value=market), \
             patch("sniper.pipeline.PaperPipeline", InstrumentedPipeline), \
             patch("sniper.cli.time.time", return_value=NOW), \
             patch("sniper.cli.output", side_effect=self.outputs.append):
            cli.command_run(self.args)
        return self.outputs[-1], market

    def test_collection_budget_exhaustion_defers_entries_without_losing_signals(self):
        self.seed_source(); self.set_evidence()
        cycle, _ = self.run_once(collector_status="budget_exhausted")
        self.assertEqual(cycle["decisions"], [])
        self.assertEqual(cycle["account"]["open_positions"], 0)
        recovered, _ = self.run_once()
        self.assertTrue(any(d["verdict"] == "PAPER_BUY" for d in recovered["decisions"]))

    def test_restart_replays_source_committed_before_pipeline_crash(self):
        # Collector persisted these before an imagined process crash. The next
        # poll has no new buys, so only journal recovery can produce the signal.
        self.seed_source(); self.set_evidence()
        cycle, _ = self.run_once(polled_buys=[])
        self.assertTrue(any(d["verdict"] == "PAPER_BUY" for d in cycle["decisions"]))

        ledger = self.ledger()
        try:
            self.assertEqual(ledger.snapshot()["open_positions"], 1)
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        finally:
            ledger.close()
        # Retrying the bridge after a second restart cannot create another fill.
        self.run_once(polled_buys=[])
        ledger = self.ledger()
        try:
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        finally:
            ledger.close()

    def test_switching_event_journals_cannot_reuse_old_account_signals(self):
        self.seed_source(); self.set_evidence()
        self.run_once()
        original = EvmStore(self.args.events_db)
        identity = original.identity
        original.close()
        self.args.events_db = str(self.root / "different-events.sqlite3")
        with self.assertRaisesRegex(ValueError, "Event journal differs"):
            self.run_once()
        account = self.ledger()
        try:
            self.assertEqual(account._get("runner_event_store_id"), identity)
            self.assertEqual(account.snapshot()["open_positions"], 1)
        finally:
            account.close()

    def test_collector_outage_does_not_disable_existing_stop_exit(self):
        self.set_evidence(price="0.1")
        ledger = self.ledger()
        try:
            engine = PaperEngine(ledger, self.policy)
            confirmations = [WalletConfirmation(f"wallet-{i}", f"cluster-{i}", f"tx-{i}", NOW-1, "100", True, True) for i in range(5)]
            c = Candidate("seed-position", ASSET, "TEST", NOW-1, confirmations,
                          good_safety(self.policy, NOW-1), metadata={"decision_at": NOW-1})
            self.assertEqual(engine.consider(c, fresh_quote(now=NOW-1), now=NOW-1)["verdict"], "PAPER_BUY")
        finally:
            ledger.close()
        def blocked_collector(**kwargs):
            # Inspect committed account state at the moment collection starts.
            # A slow/hung RPC must not precede this stop exit.
            account = self.ledger()
            try:
                self.assertEqual(account.snapshot()["open_positions"], 0)
            finally:
                account.close()
            raise RuntimeError("RPC source unavailable")
        cycle, _ = self.run_once(poll_error=blocked_collector)
        self.assertTrue(any(e["stage"] == "collector" for e in cycle["source_errors"]))
        self.assertTrue(any(e["verdict"] == "PAPER_SELL" for e in cycle["exits"]))
        self.assertEqual(cycle["account"]["open_positions"], 0)

    def test_base_price_file_outage_is_recorded_without_aborting_exit_processing(self):
        self.set_evidence(price="0.1")
        ledger = self.ledger()
        try:
            engine = PaperEngine(ledger, self.policy)
            confirmations = [WalletConfirmation(f"wallet-{i}", f"cluster-{i}", f"tx-{i}", NOW-1, "100", True, True) for i in range(5)]
            c = Candidate("seed-position", ASSET, "TEST", NOW-1, confirmations,
                          good_safety(self.policy, NOW-1), metadata={"decision_at": NOW-1})
            engine.consider(c, fresh_quote(now=NOW-1), now=NOW-1)
        finally:
            ledger.close()
        Path(self.args.base_prices).unlink()
        cycle, _ = self.run_once()
        self.assertTrue(cycle["source_errors"])
        self.assertTrue(any(e["verdict"] == "PAPER_SELL" for e in cycle["exits"]))

    def test_durable_reorg_notice_invalidates_cached_quorum_before_replay(self):
        self.seed_source(); self.set_evidence()
        ledger = self.ledger()
        try:
            engine = PaperEngine(ledger, self.policy)
            # All five confirmations were bridged, but no entry was authorized.
            pipeline = PaperPipeline(engine, self.policy, lambda *_: None, lambda *_: None, clock=lambda: NOW)
            pipeline.ingest(observations(), now=NOW-1)
        finally:
            ledger.close()
        # Rewind persisted, then the process crashed before notifying pipeline.
        store = EvmStore(self.args.events_db)
        try:
            store.rewind(4663, {"number": 99, "hash": "0x" + "99" * 32}, NOW-1)
            self.assertEqual(len(store.reorgs(4663)), 1)
        finally:
            store.close()
        cycle, _ = self.run_once(polled_buys=[])
        self.assertIn("invalidate", self.pipeline_calls)
        if "ingest" in self.pipeline_calls:
            self.assertLess(self.pipeline_calls.index("invalidate"), self.pipeline_calls.index("ingest"))
        self.assertFalse(any(d["verdict"] == "PAPER_BUY" for d in cycle["decisions"]))
        ledger = self.ledger()
        try:
            self.assertEqual(ledger.snapshot()["open_positions"], 0)
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM events WHERE kind='pipeline_invalidated'").fetchone()[0], 5)
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM events WHERE event_id='collector-reorg:1'").fetchone()[0], 1)
        finally:
            ledger.close()
        self.run_once(polled_buys=[])
        ledger = self.ledger()
        try:
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM events WHERE kind='pipeline_invalidated'").fetchone()[0], 5)
        finally:
            ledger.close()

    def test_missing_safety_blocks_recovered_signal_before_quote(self):
        self.seed_source()
        cycle, market = self.run_once(polled_buys=[])
        self.assertTrue(any(d["reason"].startswith("safety_unknown:") for d in cycle["decisions"]))
        self.assertEqual(cycle["account"]["open_positions"], 0)
        market.quote.assert_not_called()

    def test_indicative_feed_does_not_authorize_recovered_entry(self):
        self.seed_source(); self.set_evidence()
        self.args.quotes = None
        cycle, market = self.run_once(polled_buys=[], market_quote=fresh_quote(executable=False))
        self.assertTrue(any(d["reason"] == "quote_not_executable" for d in cycle["decisions"]))
        self.assertEqual(cycle["account"]["open_positions"], 0)
        market.quote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
