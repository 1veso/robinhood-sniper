import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sniper.engine import PaperEngine
from sniper.ledger import Ledger
from sniper.models import MarketQuote, SafetyEvidence
from sniper.pipeline import PaperPipeline
from sniper.policy import RiskPolicy

NOW = 1793030400
ASSET = "eip155:4663/erc20:0x" + "ab" * 20
POOL = "0x" + "cd" * 20


def buys(count=5, offset=0, **changes):
    result = []
    for i in range(offset, offset + count):
        value = {"kind": "verified_buy", "asset_id": ASSET, "pool_id": POOL, "symbol": "SYNTHETIC",
                 "event_id": f"buy-{i}", "wallet": "0x" + f"{i+1:040x}", "tx_hash": "0x" + f"{i+1:064x}",
                 "block_hash": "0x" + "ef" * 32, "block_number": 100+i, "chain_id": 4663,
                 "chain_timestamp": NOW - 10, "observed_at": NOW, "cluster_id": f"independent-{i}",
                 "verified_swap": True, "qualified": True, "qualification_evidence": "synthetic-test-proof",
                 "spent_usd": "100"}
        value.update(changes)
        result.append(value)
    return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"paper.db"
        self.policy = RiskPolicy()
        self.ledger = Ledger(self.path, self.policy.starting_cash_usd)
        self.engine = PaperEngine(self.ledger, self.policy)
        self.calls = []

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def safety(self, asset_id, now):
        self.calls.append("safety")
        return SafetyEvidence(asset_id, now, {k: True for k in self.policy.required_safety_checks}, "synthetic-test")

    def quote(self, asset_id, pool_id, now):
        self.calls.append("quote")
        return MarketQuote(asset_id, pool_id, now, "1", "1000000", gas_usd="0.01", executable=True, source="synthetic-test")

    def pipeline(self, **kwargs):
        return PaperPipeline(self.engine, self.policy, kwargs.get("safety", self.safety), kwargs.get("quote", self.quote), clock=kwargs.get("clock", lambda: NOW))

    def test_verified_signal_creates_paper_fill_after_safety(self):
        result = self.pipeline().ingest(buys(), now=NOW)
        self.assertEqual(result[-1]["verdict"], "PAPER_BUY")
        self.assertEqual(self.calls, ["safety", "quote"])
        self.assertEqual(self.ledger.snapshot()["open_positions"], 1)

    def test_quorum_survives_restart(self):
        result = self.pipeline().ingest(buys(4), now=NOW)
        self.assertEqual(result[-1]["reason"], "insufficient_independent_wallets")
        self.ledger.close()
        self.ledger = Ledger(self.path, self.policy.starting_cash_usd)
        self.engine = PaperEngine(self.ledger, self.policy)
        result = self.pipeline().ingest(buys(1, offset=4), now=NOW)
        self.assertEqual(result[-1]["verdict"], "PAPER_BUY")

    def test_duplicate_signal_cannot_add_position(self):
        pipeline = self.pipeline()
        pipeline.ingest(buys(), now=NOW)
        result = pipeline.ingest(buys(), now=NOW)
        self.assertEqual(result[-1]["reason"], "duplicate_event")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)

    def test_unknown_safety_skips_without_quote_and_retries_when_available(self):
        pipeline = self.pipeline(safety=lambda *_: None)
        result = pipeline.ingest(buys(), now=NOW)
        self.assertEqual(result[-1]["reason"], "safety_evidence_unavailable")
        self.assertFalse(self.calls)
        result = self.pipeline().ingest([], now=NOW)
        self.assertEqual(result[-1]["verdict"], "PAPER_BUY")

    def test_unverified_unpriced_stale_and_transfer_never_count(self):
        for changes in ({"verified_swap": False}, {"spent_usd": None}, {"chain_timestamp": NOW-901},
                        {"kind": "transfer"}, {"qualification_evidence": None}):
            result = self.pipeline().ingest(buys(**changes), now=NOW)
            self.assertTrue(all(r["verdict"] == "SKIP" for r in result))
        self.assertEqual(self.ledger.snapshot()["open_positions"], 0)
        self.assertFalse(self.calls)

    def test_different_pools_and_same_cluster_cannot_form_quorum(self):
        same = self.pipeline().ingest(buys(cluster_id="one"), now=NOW)
        self.assertEqual(same[-1]["reason"], "insufficient_independent_wallets")
        self.assertFalse(self.calls)
        # Fresh independent source ids in distinct pools still cannot combine.
        split = buys(offset=10)
        for i, value in enumerate(split):
            value["pool_id"] = f"pool-{i}"
        result = self.pipeline().ingest(split, now=NOW)
        self.assertTrue(all(r["verdict"] == "SKIP" for r in result))
        self.assertFalse(self.calls)

    def test_indicative_quote_refused_then_new_route_evidence_can_retry(self):
        pipeline = self.pipeline(quote=lambda a, p, n: replace(self.quote(a, p, n), executable=False))
        first = pipeline.ingest(buys(), now=NOW)
        self.assertEqual(first[-1]["reason"], "quote_not_executable")
        second = self.pipeline().ingest([], now=NOW)
        self.assertEqual(second[-1]["verdict"], "PAPER_BUY")

    def test_quote_requested_after_safety_latency(self):
        clock = [NOW]
        def slow_safety(asset, now):
            result = self.safety(asset, now)
            clock[0] += 10
            return result
        requested = []
        def quote(asset, pool, now):
            requested.append(now)
            return self.quote(asset, pool, now)
        result = self.pipeline(safety=slow_safety, quote=quote, clock=lambda: clock[0]).ingest(buys(), now=NOW)
        self.assertEqual(requested, [NOW + 10])
        self.assertEqual(result[-1]["verdict"], "PAPER_BUY")

    def test_wrong_pool_quote_is_not_substituted(self):
        result = self.pipeline(quote=lambda a, p, n: self.quote(a, "wrong-pool", n)).ingest(buys(), now=NOW)
        self.assertEqual(result[-1]["reason"], "quote_unavailable_or_pool_mismatch")
        self.assertEqual(self.ledger.snapshot()["open_positions"], 0)

    def test_reorg_invalidates_persisted_confirmations_and_halts_entries(self):
        pipeline = self.pipeline(safety=lambda *_: None)
        pipeline.ingest(buys(), now=NOW)
        self.assertEqual(pipeline.invalidate_after(102, now=NOW), 2)
        result = self.pipeline().ingest([], now=NOW)
        self.assertEqual(result[-1]["reason"], "insufficient_independent_wallets")
        self.assertIsNotNone(self.ledger.snapshot()["entry_halt"])
        self.assertEqual(self.ledger.snapshot()["open_positions"], 0)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM events WHERE kind='pipeline_invalidated'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
