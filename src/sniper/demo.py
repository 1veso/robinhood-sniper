"""Deterministic, explicitly SYNTHETIC acceptance scenario. No market claims."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .models import Candidate, MarketQuote, SafetyEvidence, WalletConfirmation
from .policy import RiskPolicy

DEMO_TIME = 1793016000


def demo_candidate(policy: RiskPolicy, *, at: int = DEMO_TIME, suffix: str = "a") -> Candidate:
    asset = f"demo:synthetic-{suffix}"
    confirmations = [WalletConfirmation(
        wallet=f"synthetic-wallet-{i}", cluster_id=f"synthetic-independent-{i}",
        tx_hash=f"synthetic-tx-{suffix}-{i}", observed_at=at - 1,
        spent_usd=Decimal("100"), verified_swap=True, qualified=True,
    ) for i in range(5)]
    return Candidate(
        event_id=f"synthetic-event-{suffix}", asset_id=asset, symbol=f"DEMO-{suffix.upper()}",
        observed_at=at - 1, confirmations=confirmations,
        safety=SafetyEvidence(asset, at - 1, {k: True for k in policy.required_safety_checks},
                              "synthetic-fixture", {"synthetic": True}),
        metadata={"decision_at": at, "synthetic": True},
    )


def demo_quote(candidate: Candidate, *, at: int, price: str = "1") -> MarketQuote:
    return MarketQuote(candidate.asset_id, f"pool-{candidate.asset_id}", at,
                       Decimal(price), Decimal("250000"), Decimal("100"), Decimal("100"),
                       Decimal("0.02"), True, "synthetic-size-model")


def run_demo(engine, policy: RiskPolicy) -> dict:
    at = DEMO_TIME
    original = demo_candidate(policy, at=at)
    unknown = replace(original, event_id="synthetic-unknown", safety=replace(
        original.safety, checks={k: None for k in policy.required_safety_checks}))
    clustered = replace(original, event_id="synthetic-cluster", confirmations=[
        replace(c, cluster_id="same-synthetic-controller") for c in original.confirmations])
    events = [
        engine.consider(unknown, demo_quote(unknown, at=at), now=at),
        engine.consider(clustered, demo_quote(clustered, at=at), now=at),
        engine.consider(original, demo_quote(original, at=at), now=at),
        engine.consider(original, demo_quote(original, at=at), now=at),
    ]
    events.extend(engine.watch({original.asset_id: demo_quote(original, at=at + 60, price="2.2")}, now=at + 60))
    events.extend(engine.watch({}, now=at + 120))
    events.extend(engine.watch({original.asset_id: demo_quote(original, at=at + 180, price="1.4")}, now=at + 180))
    losing = demo_candidate(policy, at=at + 240, suffix="b")
    events.append(engine.consider(losing, demo_quote(losing, at=at + 240), now=at + 240))
    events.extend(engine.watch({losing.asset_id: demo_quote(losing, at=at + 300, price="0.5")}, now=at + 300))
    return {"data_mode": "SYNTHETIC_DEMO", "notice": "Fabricated prices and wallet evidence test software behavior. These are not backtest or market returns.",
            "policy_version": policy.version, "events": events, "report": engine.report(now=at + 300)}
