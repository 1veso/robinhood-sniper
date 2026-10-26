"""Deterministic ranking of supplied, verified wallet statistics. No profit forecasts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sniper.adapters.solana import SourceError, decimal_value


@dataclass(frozen=True)
class RankingPolicy:
    min_closed_trades: int = 20
    min_win_rate: Decimal = Decimal("0.55")
    max_top_coin_share: Decimal = Decimal("0.50")
    min_pnl_7d_usd: Decimal = Decimal("0")
    min_avg_hold_minutes: Decimal = Decimal("30")
    max_leaders: int = 3


def rank_traders(traders: list[dict], *, decision_at: int, policy: RankingPolicy | None = None) -> dict:
    policy = policy or RankingPolicy()
    if not isinstance(decision_at, int) or isinstance(decision_at, bool) or decision_at < 0:
        raise ValueError("decision_at must be nonnegative UTC epoch seconds")
    if not isinstance(traders, list):
        raise ValueError("traders must be a list")
    if not 1 <= policy.max_leaders <= 100 or policy.min_closed_trades < 1:
        raise ValueError("invalid ranking policy")
    eligible, rejected, seen = [], [], set()
    for item in traders:
        label = item.get("wallet", "unknown") if isinstance(item, dict) else "unknown"
        try:
            if not isinstance(item, dict):
                raise SourceError("trader record must be an object")
            wallet = item.get("wallet")
            if not isinstance(wallet, str) or not 1 <= len(wallet) <= 128 or wallet in seen:
                raise SourceError("invalid or duplicate wallet")
            seen.add(wallet)
            if item.get("verified_history") is not True:
                raise SourceError("history_not_verified")
            observed = item.get("observed_at")
            if not isinstance(observed, int) or isinstance(observed, bool) or observed > decision_at or observed < 0:
                raise SourceError("history_not_available_at_decision")
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                raise SourceError("history_source_missing")
            trades = item.get("closed_trades")
            if not isinstance(trades, int) or isinstance(trades, bool) or trades < policy.min_closed_trades:
                raise SourceError("too_few_closed_trades")
            win_rate = decimal_value(item.get("win_rate"), "win_rate", maximum=Decimal(1))
            share = decimal_value(item.get("top_coin_share"), "top_coin_share", maximum=Decimal(1))
            pnl7 = decimal_value(item.get("pnl_7d_usd"), "pnl_7d_usd", minimum=Decimal("-1e30"))
            pnl30 = decimal_value(item.get("pnl_30d_usd"), "pnl_30d_usd", minimum=Decimal("-1e30"))
            holding = decimal_value(item.get("avg_hold_minutes"), "avg_hold_minutes")
            followers = decimal_value(item.get("followers"), "followers")
            if followers != followers.to_integral_value():
                raise SourceError("followers must be integer")
            if win_rate < policy.min_win_rate:
                raise SourceError("win_rate")
            if share > policy.max_top_coin_share:
                raise SourceError("one_coin_concentration")
            if pnl7 < policy.min_pnl_7d_usd or pnl30 <= 0:
                raise SourceError("nonpositive_recent_profit")
            if holding < policy.min_avg_hold_minutes:
                raise SourceError("holding_period_too_short")
            # Explicit lexicographic ordering; no unexplained model score or invented probability.
            key = (share, -min(holding, Decimal(1440)), followers, -trades, wallet)
            normalized = dict(item)
            normalized.update({"win_rate": str(win_rate), "top_coin_share": str(share),
                               "pnl_7d_usd": str(pnl7), "pnl_30d_usd": str(pnl30),
                               "avg_hold_minutes": str(holding), "followers": int(followers)})
            eligible.append((key, normalized))
        except (SourceError, TypeError, ValueError) as exc:
            rejected.append({"wallet": str(label)[:128], "reason": str(exc)})
    eligible.sort(key=lambda x: x[0])
    leaders = []
    for rank, (_, record) in enumerate(eligible[:policy.max_leaders], 1):
        record["rank"] = rank
        record["why"] = "Ranked by lower profit concentration, longer copyable holding period, less crowding, and sample size"
        leaders.append(record)
    return {"leaders": leaders, "rejected": rejected, "eligible_count": len(eligible),
            "decision_at": decision_at, "method": "deterministic_v1",
            "limitation": "Supplied verification is an assertion by the data producer, not independently verified here. Rank is not a profit forecast; freeze the selection before forward evaluation."}
