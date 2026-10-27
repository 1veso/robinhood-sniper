import csv
import tempfile
import threading
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from sniper.engine import PaperEngine
from sniper.ledger import Ledger
from sniper.models import Candidate, MarketQuote, SafetyEvidence, WalletConfirmation
from sniper.policy import RiskPolicy

NOW = 1793030400


def quote(asset="eip155:4663:token1", now=NOW, price="1", **changes):
    values = dict(asset_id=asset, pool_id="pool1", observed_at=now, price_usd=D(price),
                  liquidity_usd=D("1000000"), buy_cost_bps=D("100"), sell_cost_bps=D("100"),
                  gas_usd=D("0.01"), executable=True, source="synthetic-test")
    values.update(changes)
    return MarketQuote(**values)


def candidate(event="one", asset="eip155:4663:token1", now=NOW, **changes):
    policy = RiskPolicy()
    values = dict(event_id=event, asset_id=asset, symbol="TEST", observed_at=now,
                  confirmations=[WalletConfirmation(wallet=f"wallet{i}", cluster_id=f"cluster{i}", tx_hash=f"tx{i}:{event}",
                  observed_at=now, spent_usd=D("100"), verified_swap=True, qualified=True) for i in range(5)],
                  safety=SafetyEvidence(asset, now, {name: True for name in policy.required_safety_checks}, "synthetic-test"),
                  metadata={"decision_at": now})
    values.update(changes)
    return Candidate(**values)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.policy = RiskPolicy()
        self.ledger = Ledger(self.path, self.policy.starting_cash_usd)
        self.engine = PaperEngine(self.ledger, self.policy)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def buy(self, event="one", asset="eip155:4663:token1", now=NOW):
        return self.engine.consider(candidate(event, asset, now), quote(asset, now), now)

    def test_entry_is_sized_by_full_loss_and_costs_not_stop_distance(self):
        result = self.buy()
        self.assertEqual(result["verdict"], "PAPER_BUY")
        snapshot = self.ledger.snapshot()
        self.assertLess(result["notional_usd"], D("5"))
        self.assertLessEqual(result["full_loss_risk_usd"], snapshot["equity"] * D("0.005"))
        self.assertEqual(snapshot["cash"] + snapshot["position_basis"], D("1000"))
        self.assertLess(snapshot["equity"], D("1000"))
        self.assertGreater(snapshot["fees_paid"], 0)

    def test_paper_entry_cannot_exceed_verified_route_size(self):
        q = quote(route_max_notional_usd=D('2'), route_max_sell_quantity=D('1'), route_impact_bps=D('5'))
        result = self.engine.consider(candidate(), q, NOW)
        self.assertEqual(result['verdict'], 'PAPER_BUY')
        self.assertLessEqual(result['quantity'], D('1'))

    def test_undersized_or_wrong_pool_exit_quote_preserves_position(self):
        self.buy()
        original = self.ledger.positions()[0]['qty']
        small = quote(now=NOW+1, price='0.1', route_max_notional_usd=D('2'),
                      route_max_sell_quantity=D('.001'), route_impact_bps=D('5'))
        result = self.engine.watch({small.asset_id: small}, NOW+1)
        self.assertEqual(result[0]['reason'], 'route_size_not_covered')
        self.assertEqual(self.ledger.positions()[0]['qty'], original)
        other = quote(now=NOW+2, price='0.1', pool_id='different-pool')
        result = self.engine.watch({other.asset_id: other}, NOW+2)
        self.assertEqual(result[0]['reason'], 'quote_pool_mismatch')
        self.assertEqual(self.ledger.positions()[0]['qty'], original)

    def test_observed_route_impact_can_block_entry(self):
        q = quote(route_max_notional_usd=D('25'), route_max_sell_quantity=D('25'), route_impact_bps=D('200'))
        result = self.engine.consider(candidate(), q, NOW)
        self.assertEqual(result['verdict'], 'SKIP')
        self.assertFalse(self.ledger.positions())

    def test_aggregate_full_loss_budget_and_position_count(self):
        for i in range(4):
            result = self.buy(f"event{i}", f"asset{i}", NOW + i)
            self.assertEqual(result["verdict"], "PAPER_BUY")
        snap = self.ledger.snapshot()
        self.assertLessEqual(snap["stress_exposure"], min(snap["equity"], D("1000")) * D("0.02"))
        rejected = self.buy("fifth", "asset5", NOW + 4)
        self.assertEqual(rejected["reason"], "max_open_positions")

    def test_unknown_or_false_safety_never_passes(self):
        c = candidate()
        for value in (None, False):
            checks = dict(c.safety.checks)
            checks["sellability"] = value
            case = replace(c, event_id=f"safety-{value}", safety=replace(c.safety, checks=checks))
            self.assertEqual(self.engine.consider(case, quote(), NOW)["verdict"], "SKIP")
        checks = dict(c.safety.checks)
        checks["authority_revoked"] = False
        case = replace(c, event_id="extra-false", safety=replace(c.safety, checks=checks))
        self.assertEqual(self.engine.consider(case, quote(), NOW)["reason"], "safety_failed:authority_revoked")
        self.assertEqual(self.ledger.snapshot()["cash"], D("1000"))

    def test_safety_and_quote_must_refer_to_same_pool(self):
        c = candidate()
        c = replace(c, safety=replace(c.safety, details={"report_pool_id": "other-pool"}))
        self.assertEqual(self.engine.consider(c, quote(), NOW)["reason"], "safety_pool_mismatch")

    def test_duplicate_wallets_transactions_or_clusters_do_not_count_as_independent(self):
        base = candidate()
        for field in ("wallet", "tx_hash", "cluster_id"):
            items = [replace(x, **{field: "same"}) for x in base.confirmations]
            c = replace(base, event_id=field, confirmations=items)
            result = self.engine.consider(c, quote(), NOW)
            self.assertEqual(result["reason"], "insufficient_independent_wallets")
            self.assertEqual(result["independent_wallets"], 1)

    def test_dust_and_unqualified_swaps_do_not_count(self):
        base = candidate()
        for field, value in (("spent_usd", D("0.01")), ("verified_swap", False), ("qualified", False)):
            c = replace(base, event_id=field, confirmations=[replace(x, **{field: value}) for x in base.confirmations])
            self.assertEqual(self.engine.consider(c, quote(), NOW)["reason"], "insufficient_independent_wallets")

    def test_price_must_be_observed_after_decision_and_be_fresh(self):
        c = candidate(now=NOW - 10, metadata={"decision_at": NOW})
        self.assertEqual(self.engine.consider(c, quote(now=NOW - 1), NOW)["reason"], "predecision_quote")
        c = candidate("stale", now=NOW - 100)
        self.assertEqual(self.engine.consider(c, quote(now=NOW - 100), NOW)["reason"], "stale_quote")
        c = candidate("future")
        self.assertEqual(self.engine.consider(c, quote(now=NOW + 1), NOW)["reason"], "future_quote")

    def test_duplicate_decision_survives_restart(self):
        first = self.buy()
        before = self.ledger.snapshot()["cash"]
        other = Ledger(self.path, D("1000"))
        try:
            result = PaperEngine(other, self.policy).consider(candidate(), quote(), NOW)
            self.assertEqual(result["reason"], "duplicate_event")
            self.assertEqual(other.snapshot()["cash"], before)
            self.assertEqual(other.snapshot()["open_positions"], 1)
        finally:
            other.close()
        self.assertEqual(first["verdict"], "PAPER_BUY")

    def test_same_asset_different_events_cannot_double_buy(self):
        self.buy()
        self.assertEqual(self.buy("another")["reason"], "already_open")

    def test_missing_feed_keeps_tokens_and_does_not_realize_loss(self):
        self.buy()
        before = self.ledger.snapshot()
        result = self.engine.watch({}, NOW + 1)
        after = self.ledger.snapshot()
        self.assertEqual(result[0]["verdict"], "UNRESOLVED")
        self.assertEqual(after["open_positions"], 1)
        self.assertEqual(after["positions"][0]["qty"], before["positions"][0]["qty"])
        self.assertEqual(after["cash"], before["cash"])
        self.assertEqual(after["realized_pnl"], ZERO)
        self.assertEqual(after["market_value"], ZERO)
        self.engine.watch({"eip155:4663:token1": quote(now=NOW + 2)}, NOW + 2)
        self.assertEqual(self.ledger.snapshot()["unresolved_positions"], 0)

    def test_stale_quote_cannot_trigger_a_sale(self):
        self.buy()
        result = self.engine.watch({"eip155:4663:token1": quote(price="0.1")}, NOW + 31)
        self.assertEqual(result[0]["reason"], "stale_quote")
        self.assertEqual(self.ledger.snapshot()["open_positions"], 1)

    def test_stop_fills_at_observed_gap_price_not_stop_threshold(self):
        self.buy()
        result = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="0.1")}, NOW + 1)
        self.assertEqual(result[0]["reason"], "stop_loss")
        self.assertLess(result[0]["fill_price"], D("0.1"))
        snap = self.ledger.snapshot()
        self.assertEqual(snap["open_positions"], 0)
        self.assertEqual(snap["cash"] - D("1000"), snap["realized_pnl"])
        self.assertEqual(snap["losses"], 1)

    def test_partial_and_final_exit_are_one_completed_trade(self):
        self.buy()
        first = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="2.2")}, NOW + 1)
        self.assertEqual(first[0]["action"], "PARTIAL")
        self.assertEqual(self.engine.report(NOW + 1)["closed_positions"], 0)
        self.assertIsNone(self.engine.report(NOW + 1)["win_rate"])
        repeat = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="2.2")}, NOW + 1)
        self.assertEqual(repeat, [])
        second = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 2, price="1.4")}, NOW + 2)
        self.assertEqual(second[0]["reason"], "trailing_stop")
        report = self.engine.report(NOW + 2)
        self.assertEqual(report["closed_positions"], 1)
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["win_rate"], D("1"))
        self.assertEqual(report["cash"] - D("1000"), report["realized_pnl"])
        self.assertEqual(report["unrealized_pnl"], ZERO)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 3)

    def test_manual_entry_halt_does_not_disable_stop_exit(self):
        self.buy()
        self.ledger.set_halt("manual safety pause")
        result = self.buy("blocked", "other")
        self.assertEqual(result["reason"], "manual_entry_halt")
        result = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="0.4")}, NOW + 1)
        self.assertEqual(result[0]["verdict"], "PAPER_SELL")
        self.assertEqual(self.ledger.snapshot()["open_positions"], 0)

    def test_unrealized_loss_triggers_daily_halt_without_fake_sale(self):
        self.ledger.close()
        self.policy = replace(self.policy, daily_loss_halt_pct=D("0.1"))
        self.ledger = Ledger(Path(self.tmp.name) / "low-daily-cap.sqlite", self.policy.starting_cash_usd)
        self.engine = PaperEngine(self.ledger, self.policy)
        self.buy()
        self.engine.watch({}, NOW + 1)
        self.assertTrue(self.engine.report(NOW + 1)["daily_halted"])
        self.assertEqual(self.ledger.snapshot()["realized_pnl"], ZERO)
        self.ledger.clear_halt()
        result = self.buy("blocked", "other", NOW + 2)
        self.assertEqual(result["reason"], "daily_equity_loss")

    def test_cooldown_applies_after_actual_simulated_close(self):
        self.buy()
        self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="0.4")}, NOW + 1)
        self.assertEqual(self.buy("reentry", now=NOW + 2)["reason"], "cooldown")

    def test_time_stop_exits_at_current_price(self):
        self.buy()
        later = NOW + self.policy.time_stop_seconds
        result = self.engine.watch({"eip155:4663:token1": quote(now=later)}, later)
        self.assertEqual(result[0]["reason"], "time_stop")
        self.assertLess(self.ledger.snapshot()["cash"], D("1000"))

    def test_concurrent_same_signal_buys_exactly_once(self):
        other = Ledger(self.path, D("1000"))
        other_engine = PaperEngine(other, self.policy)
        barrier = threading.Barrier(2)
        results = []
        def buy(engine):
            barrier.wait()
            results.append(engine.consider(candidate(), quote(), NOW))
        threads = [threading.Thread(target=buy, args=(engine,)) for engine in (self.engine, other_engine)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        other.close()
        self.assertEqual(sorted(r["verdict"] for r in results), ["PAPER_BUY", "SKIP"])
        self.assertEqual(self.ledger.snapshot()["open_positions"], 1)

    def test_exit_quote_cannot_move_backwards_within_freshness_window(self):
        self.buy()
        self.engine.watch({"eip155:4663:token1": quote(now=NOW + 10, price="2.2")}, NOW + 10)
        quantity = self.ledger.positions()[0]["qty"]
        result = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="0.4")}, NOW + 11)
        self.assertEqual(result[0]["reason"], "out_of_order_quote")
        self.assertEqual(self.ledger.positions()[0]["qty"], quantity)
        self.assertEqual(self.ledger.snapshot()["closed_positions"], 0)

    def test_csv_export_neutralizes_untrusted_symbol_formulas(self):
        c = replace(candidate(), symbol='=HYPERLINK("https://example.invalid","token")')
        self.engine.consider(c, quote(), NOW)
        path = self.ledger.export_csv(Path(self.tmp.name) / "fills.csv")
        with path.open() as handle:
            row = next(csv.DictReader(handle))
        self.assertTrue(row["symbol"].startswith("'="))
        self.assertTrue(row["cash_delta"].startswith("-"))
        self.assertNotIn("'", row["cash_delta"])

    def test_changed_policy_is_rejected_without_mutating_original(self):
        self.buy()
        old_version = self.ledger._get("policy_version")
        with self.assertRaisesRegex(ValueError, "pinned policy"):
            PaperEngine(self.ledger, replace(self.policy, stop_loss_pct=D("80")))
        self.assertEqual(self.ledger._get("policy_version"), old_version)
        # The original rule still exits; no hidden switch to the new 80% stop.
        result = self.engine.watch({"eip155:4663:token1": quote(now=NOW + 1, price="0.4")}, NOW + 1)
        self.assertEqual(result[0]["reason"], "stop_loss")

    def test_backwards_entry_watch_and_report_do_not_mutate_account(self):
        self.buy(now=NOW + 10)
        before = self.ledger.snapshot()
        operations = [
            lambda: self.buy("past", "other", now=NOW),
            lambda: self.engine.watch({"eip155:4663:token1": quote(price="0.1")}, NOW),
            lambda: self.engine.report(NOW),
        ]
        for operation in operations:
            with self.assertRaisesRegex(ValueError, "precedes committed account time"):
                operation()
            self.assertEqual(self.ledger.snapshot(), before)
        other = Ledger(self.path, D("1000"))
        try:
            with self.assertRaisesRegex(ValueError, "precedes committed account time"):
                PaperEngine(other, self.policy).watch({}, NOW)
        finally:
            other.close()

    def test_unknown_strategy_cannot_mislabel_wallet_rule_results(self):
        c = replace(candidate(), strategy="unimplemented_magic_strategy")
        result = self.engine.consider(c, quote(), NOW)
        self.assertEqual(result["reason"], "unsupported_strategy")
        self.assertEqual(self.ledger.snapshot()["cash"], D("1000"))

    def test_nan_and_infinite_quotes_cannot_enter_domain(self):
        for value in (D("NaN"), D("Infinity"), D("-1")):
            with self.assertRaises(ValueError):
                quote(price_usd=value)
        self.assertEqual(self.ledger.snapshot()["cash"], D("1000"))


ZERO = D("0")
if __name__ == "__main__":
    unittest.main()
