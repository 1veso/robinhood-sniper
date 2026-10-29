"""Deterministic wallet-following strategy and PAPER-only execution.

A ticket is a maximum. Capital is sized down to a full-position-loss budget;
stop distance is never used to pretend that a memecoin cannot become worthless.
The price-impact calculation is an explicit simulation assumption, not a DEX quote.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .ledger import BPS, ZERO, Ledger, decimal, impact_fraction, json_text, timestamp
from .models import Candidate, MarketQuote
from .policy import RiskPolicy

D = Decimal
ONE = D("1")
HUNDRED = D("100")


def utc_day(now: int) -> str:
    return datetime.fromtimestamp(timestamp(now), timezone.utc).strftime("%Y-%m-%d")


def normalize_wallet(wallet: str) -> str:
    return wallet.lower() if wallet.startswith(("0x", "0X")) else wallet


class PaperEngine:
    strategy_name = "wallet_follow_v1"
    engine_mode = "strict"
    allow_unqualified_wallets = False
    allow_unknown_safety = False
    require_known_safety_checks = frozenset()
    confirmation_label = "independent_wallets"

    def __init__(self, ledger: Ledger, policy: RiskPolicy):
        if not isinstance(policy, RiskPolicy) or policy.paper_only is not True:
            raise ValueError("PaperEngine requires an immutable PAPER-only policy")
        if ledger.snapshot()["starting_cash"] != policy.starting_cash_usd:
            raise ValueError("Policy starting cash must match the ledger")
        self.ledger = ledger
        self.policy = policy
        with ledger.transaction():
            stored_version = ledger._get("policy_version")
            stored_mode = ledger._get("engine_mode") or ("strict" if stored_version is not None else self.engine_mode)
            if stored_mode != self.engine_mode:
                raise ValueError("Account belongs to another evaluation mode; use a separate ledger")
            ledger._set("engine_mode", self.engine_mode)
            encoded_policy = json_text(policy.as_dict())
            if stored_version is not None:
                if stored_version != policy.version or ledger._get("policy") != encoded_policy:
                    raise ValueError("Risk policy differs from this account's pinned policy. Use a new experiment database; existing positions retain their original exits.")
            else:
                ledger._set("policy_version", policy.version)
                ledger._set("policy", encoded_policy)
            if ledger._get("last_processed_at") is None:
                last = ledger.conn.execute("SELECT MAX(observed_at) FROM (SELECT observed_at FROM decisions UNION ALL SELECT observed_at FROM equity_history)").fetchone()[0]
                ledger._set("last_processed_at", last if last is not None else 0)

    def _advance_time(self, now: int) -> None:
        last = int(self.ledger._get("last_processed_at") or 0)
        if now < last:
            raise ValueError(f"Processing time {now} precedes committed account time {last}. Use a separate database for an earlier replay.")
        self.ledger._set("last_processed_at", now)

    def _day(self, now: int) -> dict[str, Any]:
        day = utc_day(now)
        row = self.ledger.conn.execute("SELECT * FROM daily_equity WHERE day=?", (day,)).fetchone()
        if row is None:
            equity = self.ledger._snapshot()["equity"]
            self.ledger.conn.execute("INSERT INTO daily_equity(day,opening_equity,observed_at) VALUES(?,?,?)", (day, str(equity), now))
            row = self.ledger.conn.execute("SELECT * FROM daily_equity WHERE day=?", (day,)).fetchone()
        return dict(row)

    def _daily_halt(self, now: int) -> bool:
        day = self._day(now)
        if day["halted_at"] is not None:
            return True
        opening = decimal(day["opening_equity"])
        equity = self.ledger._snapshot()["equity"]
        if opening > ZERO and equity <= opening * (ONE - self.policy.daily_loss_halt_pct / HUNDRED):
            self.ledger.conn.execute("UPDATE daily_equity SET halted_at=?,halt_reason=? WHERE day=?", (now, "daily_equity_loss", day["day"]))
            return True
        return False

    def _expire_marks(self, now: int) -> None:
        """Unknown liquidation value is zero for risk, never a fabricated realized sale."""
        self.ledger.conn.execute(
            "UPDATE positions SET mark_status='stale_quote' WHERE mark_status='ok' AND (mark_at>? OR mark_at<?)",
            (now, now - self.policy.max_quote_age_seconds),
        )

    def _finish_decision(self, candidate: Candidate, now: int, result: dict[str, Any], quote: MarketQuote | None = None) -> dict[str, Any]:
        result = {"mode": "PAPER", "event_id": candidate.event_id, "asset_id": candidate.asset_id,
                  "policy_version": self.policy.version, **self.decision_context(candidate), **result}
        payload = {"candidate": candidate.as_dict(), "quote": quote.as_dict() if isinstance(quote, MarketQuote) else None,
                   "policy": self.policy.as_dict(), "observed_at": now, "result": result}
        self.ledger.conn.execute(
            "INSERT INTO decisions(event_id,asset_id,observed_at,verdict,reason,payload) VALUES(?,?,?,?,?,?)",
            (candidate.event_id, candidate.asset_id, now, result["verdict"], result["reason"], json_text(payload)),
        )
        self.ledger._record_equity(now, result["reason"])
        return result

    def _confirmations(self, candidate: Candidate, now: int, *, observed_after: int | None = None) -> tuple[int, str | None]:
        clusters: set[str] = set()
        wallets: set[str] = set()
        transactions: set[str] = set()
        for confirmation in candidate.confirmations:
            if observed_after is not None and confirmation.observed_at <= observed_after:
                continue
            if not confirmation.verified_swap or (not confirmation.qualified and not self.allow_unqualified_wallets):
                continue
            if confirmation.observed_at > now:
                return 0, "future_confirmation"
            if now - confirmation.observed_at > self.policy.max_confirmation_age_seconds:
                continue
            if confirmation.spent_usd < self.policy.min_wallet_spend_usd:
                continue
            wallet = normalize_wallet(confirmation.wallet)
            tx = confirmation.tx_hash.lower() if confirmation.tx_hash.startswith("0x") else confirmation.tx_hash
            if wallet in wallets or tx in transactions or confirmation.cluster_id in clusters:
                continue
            wallets.add(wallet)
            transactions.add(tx)
            clusters.add(confirmation.cluster_id)
        return len(clusters), None

    def _quote_problem(self, quote: MarketQuote, asset_id: str, now: int, decision_at: int | None = None) -> str | None:
        try:
            quote.validate()
        except (ValueError, TypeError, AttributeError):
            return "invalid_quote"
        if quote.asset_id != asset_id:
            return "quote_asset_mismatch"
        if not quote.executable:
            return "quote_not_executable"
        if quote.observed_at > now:
            return "future_quote"
        if now - quote.observed_at > self.policy.max_quote_age_seconds:
            return "stale_quote"
        if decision_at is not None and quote.observed_at < decision_at:
            return "predecision_quote"
        if quote.price_usd <= ZERO or quote.liquidity_usd <= ZERO:
            return "invalid_quote"
        return None

    def _entry_calculation(self, notional: Decimal, quote: MarketQuote) -> dict[str, Decimal]:
        impact = impact_fraction(notional, quote.liquidity_usd)
        buy_fraction = quote.buy_cost_bps / BPS + impact
        sell_fraction = quote.sell_cost_bps / BPS + impact
        qty = notional / quote.price_usd
        friction = notional * buy_fraction
        entry_cost = notional + friction + quote.gas_usd
        exit_cost = notional * sell_fraction + quote.gas_usd
        return {
            "notional": notional, "quantity": qty, "fill_price": quote.price_usd * (ONE + buy_fraction),
            "friction": friction, "entry_cost": entry_cost, "exit_cost": exit_cost,
            "liquidation": max(ZERO, notional - exit_cost), "risk": entry_cost + exit_cost,
            "impact_bps": impact * BPS,
        }

    def _size(self, quote: MarketQuote, snapshot: dict[str, Any]) -> dict[str, Decimal] | None:
        def fits(notional: Decimal) -> bool:
            c = self._entry_calculation(notional, quote)
            after_equity = snapshot["equity"] - c["entry_cost"] + c["liquidation"]
            reference = max(ZERO, min(snapshot["starting_cash"], after_equity))
            return (
                c["entry_cost"] <= snapshot["cash"]
                and c["risk"] <= reference * self.policy.per_position_risk_pct / HUNDRED
                and snapshot["stress_exposure"] + c["risk"] <= reference * self.policy.max_account_risk_pct / HUNDRED
                and c["impact_bps"] <= self.policy.max_price_impact_bps
                and c["liquidation"] > ZERO
            )
        low, high = ZERO, min(self.policy.ticket_usd, snapshot["cash"])
        if quote.route_max_notional_usd is not None:
            high = min(high, quote.route_max_notional_usd, quote.route_max_sell_quantity * quote.price_usd)
            if quote.route_impact_bps > self.policy.max_price_impact_bps:
                return None
        if high <= ZERO:
            return None
        if not fits(high):
            for _ in range(90):
                mid = (low + high) / D("2")
                if fits(mid):
                    low = mid
                else:
                    high = mid
            high = low
        notional = high.quantize(D("0.00000001"), rounding=ROUND_DOWN)
        if notional < D("0.01") or not fits(notional):
            return None
        return self._entry_calculation(notional, quote)

    def consider(self, candidate: Candidate, quote: MarketQuote, now: int) -> dict[str, Any]:
        timestamp(now)
        if not isinstance(candidate, Candidate):
            raise ValueError("A validated Candidate is required")
        # Revalidate nested dictionaries in the otherwise frozen domain records.
        candidate = Candidate.from_dict(candidate.as_dict())
        with self.ledger.transaction():
            self._advance_time(now)
            existing = self.ledger.conn.execute("SELECT verdict FROM decisions WHERE event_id=?", (candidate.event_id,)).fetchone()
            if existing:
                return {"mode": "PAPER", "verdict": "SKIP", "reason": "duplicate_event",
                        "event_id": candidate.event_id, "asset_id": candidate.asset_id, "original_verdict": existing["verdict"]}
            self._day(now)
            self._expire_marks(now)
            def reject(reason: str, **extra: Any) -> dict[str, Any]:
                return self._finish_decision(candidate, now, {"verdict": "SKIP", "reason": reason, **extra}, quote)
            if candidate.strategy != self.strategy_name:
                return reject("unsupported_strategy")
            if self.ledger._get("entry_halt"):
                return reject("manual_entry_halt")
            if self._daily_halt(now):
                return reject("daily_equity_loss")
            if candidate.observed_at > now:
                return reject("future_candidate")
            decision_at = candidate.metadata.get("decision_at", candidate.observed_at)
            if type(decision_at) is not int or decision_at < candidate.observed_at or decision_at > now:
                return reject("invalid_decision_time")
            if candidate.safety.observed_at > now:
                return reject("future_safety_evidence")
            if now - candidate.safety.observed_at > self.policy.max_evidence_age_seconds:
                return reject("stale_safety_evidence")
            if decision_at < candidate.safety.observed_at:
                return reject("evidence_after_decision")
            if any(c.observed_at > decision_at for c in candidate.confirmations):
                return reject("confirmation_after_decision")
            for name, value in candidate.safety.checks.items():
                if value is False:
                    return reject("safety_failed:" + name)
            for name in self.policy.required_safety_checks:
                value = candidate.safety.checks.get(name)
                if value is None and self.allow_unknown_safety and name not in self.require_known_safety_checks:
                    continue
                if value is not True:
                    return reject(("safety_unknown:" if value is None else "safety_failed:") + name)
            confirmations, error = self._confirmations(candidate, now)
            if error:
                return reject(error)
            if confirmations < self.policy.min_independent_wallets:
                return reject("insufficient_" + self.confirmation_label, **{self.confirmation_label: confirmations})
            problem = self._quote_problem(quote, candidate.asset_id, now, decision_at)
            if problem:
                return reject(problem)
            if quote.route_side != 'both':
                return reject('quote_not_entry_route')
            report_pool_id = candidate.safety.details.get("report_pool_id")
            if report_pool_id is not None and normalize_wallet(str(report_pool_id)) != normalize_wallet(quote.pool_id):
                return reject("safety_pool_mismatch")
            if quote.liquidity_usd < self.policy.min_liquidity_usd:
                return reject("insufficient_liquidity")
            snapshot = self.ledger._snapshot()
            if any(p["asset_id"] == candidate.asset_id for p in snapshot["positions"]):
                return reject("already_open")
            if self.policy.max_open_positions and snapshot["open_positions"] >= self.policy.max_open_positions:
                return reject("max_open_positions")
            day_start = int(datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            count = self.ledger.conn.execute("SELECT COUNT(*) FROM fills WHERE action='OPEN' AND observed_at>=? AND observed_at<=?", (day_start, now)).fetchone()[0]
            if self.policy.max_entries_per_day and count >= self.policy.max_entries_per_day:
                return reject("max_entries_per_day")
            closed = self.ledger.conn.execute("SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?", (candidate.asset_id,)).fetchone()[0]
            if closed is not None and now - closed < self.policy.cooldown_seconds:
                return reject("cooldown")
            if closed is not None and self.policy.cooldown_seconds == 0:
                fresh, error = self._confirmations(candidate, now, observed_after=closed)
                if error:
                    return reject(error)
                if fresh < self.policy.min_independent_wallets:
                    return reject("reentry_requires_new_buyers_after_close", observed_buyers_after_close=fresh,
                                  required_buyers=self.policy.min_independent_wallets, last_closed_at=closed)
            amounts = self._size(quote, snapshot)
            if amounts is None:
                return reject("risk_or_cash_budget_exhausted")
            position_id = "position:" + candidate.event_id
            p = {
                "position_id": position_id, "asset_id": candidate.asset_id, "symbol": candidate.symbol,
                "strategy": candidate.strategy, "event_id": candidate.event_id, "opened_at": now,
                "pool_id": quote.pool_id, "entry_price": amounts["fill_price"],
                "reference_entry_price": quote.price_usd, "qty0": amounts["quantity"], "qty": amounts["quantity"],
                "total_entry_cost": amounts["entry_cost"], "remaining_basis": amounts["entry_cost"],
                "realized_pnl": ZERO, "net_exit_proceeds": ZERO, "total_costs": amounts["friction"] + quote.gas_usd,
                "liq0": quote.liquidity_usd, "peak_price": quote.price_usd, "mark_price": quote.price_usd,
                "mark_liquidity": quote.liquidity_usd, "mark_sell_bps": quote.sell_cost_bps,
                "mark_gas": quote.gas_usd, "mark_at": quote.observed_at, "mark_status": "ok",
                "reserved_exit_cost": amounts["exit_cost"], "tp_done": 0,
            }
            keys = list(p)
            self.ledger.conn.execute(
                f"INSERT INTO positions({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                tuple(str(v) if isinstance(v, Decimal) else v for v in p.values()),
            )
            self.ledger._set("cash", snapshot["cash"] - amounts["entry_cost"])
            self.ledger._set("fees_paid", snapshot["fees_paid"] + amounts["friction"] + quote.gas_usd)
            self._fill(position_id, candidate.event_id, candidate.asset_id, candidate.symbol, candidate.strategy,
                       "OPEN", now, amounts["quantity"], quote.price_usd, amounts["fill_price"],
                       amounts["notional"], amounts["friction"], quote.gas_usd, -amounts["entry_cost"], ZERO, "entry")
            self._daily_halt(now)
            return self._finish_decision(candidate, now, {
                "verdict": "PAPER_BUY", "reason": "eligible", "position_id": position_id,
                "quantity": amounts["quantity"], "notional_usd": amounts["notional"],
                "cash_spent_usd": amounts["entry_cost"], "full_loss_risk_usd": amounts["risk"],
                "fill_price": amounts["fill_price"], "reference_price": quote.price_usd,
                "estimated_impact_bps": amounts["impact_bps"], self.confirmation_label: confirmations,
                "execution_model": "indicative_price_plus_bps_gas_and_constant_product_proxy",
            }, quote)

    def decision_context(self, candidate: Candidate) -> dict[str, Any]:
        return {}

    def _fill(self, position_id: str, event_id: str, asset_id: str, symbol: str, strategy: str,
              action: str, now: int, qty: Decimal, reference: Decimal, price: Decimal,
              gross: Decimal, friction: Decimal, gas: Decimal, cash_delta: Decimal,
              pnl: Decimal, reason: str) -> None:
        order_id = f"{position_id}:{action}:{reason}:{now}"
        self.ledger.conn.execute("""
            INSERT INTO fills(order_id,position_id,event_id,asset_id,symbol,strategy,action,observed_at,
                              quantity,reference_price,fill_price,gross_usd,friction_usd,gas_usd,cash_delta,pnl,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (order_id, position_id, event_id, asset_id, symbol, strategy, action, now,
              str(qty), str(reference), str(price), str(gross), str(friction), str(gas), str(cash_delta), str(pnl), reason))

    def _sell(self, p: dict[str, Any], qty: Decimal, quote: MarketQuote, now: int, reason: str) -> dict[str, Any]:
        qty = min(qty, p["qty"])
        gross = qty * quote.price_usd
        friction = gross * (quote.sell_cost_bps / BPS + impact_fraction(gross, quote.liquidity_usd))
        proceeds = gross - friction - quote.gas_usd
        if proceeds <= ZERO or qty <= ZERO:
            self.ledger.conn.execute("UPDATE positions SET mark_status='unexecutable_costs' WHERE position_id=?", (p["position_id"],))
            return {"mode": "PAPER", "verdict": "UNRESOLVED", "asset_id": p["asset_id"], "reason": "unexecutable_costs"}
        closing = qty == p["qty"]
        allocated_basis = p["remaining_basis"] if closing else p["remaining_basis"] * qty / p["qty"]
        pnl = proceeds - allocated_basis
        remaining = p["qty"] - qty
        total_pnl = p["realized_pnl"] + pnl
        total_proceeds = p["net_exit_proceeds"] + proceeds
        total_costs = p["total_costs"] + friction + quote.gas_usd
        self.ledger._set("cash", decimal(self.ledger._get("cash")) + proceeds)
        self.ledger._set("realized_pnl", decimal(self.ledger._get("realized_pnl")) + pnl)
        self.ledger._set("fees_paid", decimal(self.ledger._get("fees_paid")) + friction + quote.gas_usd)
        action = "CLOSE" if closing else "PARTIAL"
        self._fill(p["position_id"], p["event_id"], p["asset_id"], p["symbol"], p["strategy"], action,
                   now, qty, quote.price_usd, (gross - friction) / qty, gross, friction, quote.gas_usd, proceeds, pnl, reason)
        if closing:
            self.ledger.conn.execute("""INSERT INTO closed_positions(position_id,asset_id,symbol,strategy,opened_at,
                                       closed_at,entry_cost,net_exit_proceeds,realized_pnl,total_costs,reason)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                     (p["position_id"], p["asset_id"], p["symbol"], p["strategy"], p["opened_at"], now,
                                      str(p["total_entry_cost"]), str(total_proceeds), str(total_pnl), str(total_costs), reason))
            self.ledger.conn.execute("DELETE FROM positions WHERE position_id=?", (p["position_id"],))
        else:
            # A remaining position still needs one full gas payment to exit.
            exit_reserve = max(quote.gas_usd, p["reserved_exit_cost"] * remaining / p["qty"])
            self.ledger.conn.execute("""UPDATE positions SET qty=?,remaining_basis=?,realized_pnl=?,net_exit_proceeds=?,
                                     total_costs=?,reserved_exit_cost=?,tp_done=1 WHERE position_id=?""",
                                     (str(remaining), str(p["remaining_basis"] - allocated_basis), str(total_pnl),
                                      str(total_proceeds), str(total_costs), str(exit_reserve), p["position_id"]))
        return {"mode": "PAPER", "verdict": "PAPER_SELL", "asset_id": p["asset_id"], "position_id": p["position_id"],
                "reason": reason, "action": action, "quantity": qty, "cash_received_usd": proceeds,
                "pnl": pnl, "closed": closing, "fill_price": (gross - friction) / qty}

    def watch(self, quotes: dict[str, MarketQuote], now: int) -> list[dict[str, Any]]:
        timestamp(now)
        if not isinstance(quotes, dict):
            raise ValueError("Quotes must be keyed by asset ID")
        results: list[dict[str, Any]] = []
        with self.ledger.transaction():
            self._advance_time(now)
            self._day(now)
            for p in self.ledger._positions():
                quote = quotes.get(p["asset_id"])
                problem = "missing_quote" if quote is None else self._quote_problem(quote, p["asset_id"], now)
                if not problem and normalize_wallet(quote.pool_id) != normalize_wallet(p["pool_id"]):
                    problem = "quote_pool_mismatch"
                if not problem and quote.observed_at < max(p["mark_at"], p["opened_at"]):
                    problem = "out_of_order_quote"
                if not problem and quote.route_max_sell_quantity is not None and p["qty"] > quote.route_max_sell_quantity:
                    problem = "route_size_not_covered"
                if problem:
                    self.ledger.conn.execute("UPDATE positions SET mark_status=? WHERE position_id=?", (problem, p["position_id"]))
                    result = {"mode": "PAPER", "verdict": "UNRESOLVED", "asset_id": p["asset_id"],
                              "reason": problem, "quantity_unchanged": p["qty"], "conservative_mark_usd": ZERO}
                    self.ledger.record_event(f"unresolved:{p['position_id']}:{now}", "unresolved_position", result, now)
                    results.append(result)
                    continue
                quote_id = hashlib.sha256(json_text(quote.as_dict()).encode()).hexdigest()[:16]
                self.ledger.record_event(f"mark:{p['position_id']}:{quote.observed_at}:{quote_id}", "market_mark", quote.as_dict(), now)
                peak = max(p["peak_price"], quote.price_usd)
                self.ledger.conn.execute("""UPDATE positions SET mark_price=?,mark_liquidity=?,mark_sell_bps=?,mark_gas=?,
                                         mark_at=?,mark_status='ok',peak_price=? WHERE position_id=?""",
                                         (str(quote.price_usd), str(quote.liquidity_usd), str(quote.sell_cost_bps),
                                          str(quote.gas_usd), quote.observed_at, str(peak), p["position_id"]))
                gain = (quote.price_usd / p["entry_price"] - ONE) * HUNDRED
                age = now - p["opened_at"]
                qty, reason = ZERO, None
                if gain <= -self.policy.stop_loss_pct:
                    qty, reason = p["qty"], "stop_loss"
                elif p["tp_done"] and quote.price_usd <= peak * (ONE - self.policy.trailing_stop_pct / HUNDRED):
                    qty, reason = p["qty"], "trailing_stop"
                elif not p["tp_done"] and gain >= self.policy.take_profit_pct:
                    qty, reason = min(p["qty"], p["qty0"] * self.policy.take_profit_fraction), "take_profit"
                elif age >= self.policy.time_stop_seconds and gain < self.policy.time_stop_min_gain_pct:
                    qty, reason = p["qty"], "time_stop"
                if reason:
                    results.append(self._sell(p, qty, quote, now, reason))
            self._daily_halt(now)
            self.ledger._record_equity(now, "watch")
        return results

    def report(self, now: int) -> dict[str, Any]:
        timestamp(now)
        with self.ledger.transaction():
            self._advance_time(now)
            self._day(now)
            self._expire_marks(now)
            self._daily_halt(now)
            self.ledger._record_equity(now, "report")
            result = self.ledger._snapshot()
            day = self._day(now)
            rows = self.ledger.conn.execute("SELECT realized_pnl FROM closed_positions").fetchall()
            profits = [decimal(row["realized_pnl"]) for row in rows]
            wins = [p for p in profits if p > ZERO]
            losses = [p for p in profits if p < ZERO]
            gross_profit = sum(wins, ZERO)
            gross_loss = -sum(losses, ZERO)
            result.update({
                "as_of": now, "day": day["day"], "daily_opening_equity": decimal(day["opening_equity"]),
                "daily_equity_pnl": result["equity"] - decimal(day["opening_equity"]),
                "daily_halted": day["halted_at"] is not None, "policy_version": self.policy.version,
                "net_pnl": result["equity"] - result["starting_cash"],
                "return_pct": (result["equity"] / result["starting_cash"] - ONE) * HUNDRED,
                "win_rate": D(len(wins)) / D(len(profits)) if profits else None,
                "average_win": gross_profit / D(len(wins)) if wins else None,
                "average_loss": gross_loss / D(len(losses)) if losses else None,
                "profit_factor": gross_profit / gross_loss if gross_loss > ZERO else None,
                "risk_basis": "remaining_entry_cost_plus_reserved_exit_cost; entry-time budget",
                "fill_model": "indicative prices plus friction, gas and constant-product depth proxy; not executable V3 quotes",
                "unresolved_valuation": "zero for conservative equity; holdings remain open and unrealized",
                "benchmark": None,
            })
            return result
