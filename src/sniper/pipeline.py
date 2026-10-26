"""Persist verified buys, assemble independent confirmations, and PAPER-evaluate them."""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Callable

from .ledger import json_text
from .models import Candidate, MarketQuote, SafetyEvidence, WalletConfirmation, identifier, timestamp


def _digest(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


class PaperPipeline:
    def __init__(self, engine, policy, safety_provider: Callable, quote_provider: Callable, clock=time.time):
        if engine.policy.version != policy.version:
            raise ValueError("Pipeline and engine must use the same policy")
        self.engine, self.policy, self.ledger = engine, policy, engine.ledger
        self.safety_provider, self.quote_provider, self.clock = safety_provider, quote_provider, clock

    def _skip(self, reason, now, asset_id=None, context=None):
        result = {"mode": "PAPER", "verdict": "SKIP", "reason": reason, "asset_id": asset_id}
        payload = {**result, "context": context}
        self.ledger.record_event("pipeline-skip:" + _digest(payload), "pipeline_skip", payload, now)
        return result

    def _buy(self, raw, now):
        if not isinstance(raw, dict) or raw.get("kind") != "verified_buy":
            raise ValueError("not_verified_buy")
        if raw.get("canonical") is False:
            raise ValueError("noncanonical_buy")
        if raw.get("verified_swap") is not True or raw.get("qualified") is not True:
            raise ValueError("unverified_or_unqualified_buy")
        if not raw.get("qualification_evidence"):
            raise ValueError("qualification_evidence_missing")
        observed = timestamp(raw.get("chain_timestamp"), "chain_timestamp")
        if not 0 <= now - observed <= self.policy.max_confirmation_age_seconds:
            raise ValueError("stale_or_future_chain_event")
        confirmation = WalletConfirmation(
            wallet=raw.get("wallet"), cluster_id=raw.get("cluster_id"), tx_hash=raw.get("tx_hash"),
            observed_at=observed, spent_usd=raw.get("spent_usd"), verified_swap=True, qualified=True)
        if confirmation.spent_usd < self.policy.min_wallet_spend_usd:
            raise ValueError("wallet_purchase_below_minimum")
        value = {"asset_id": identifier(raw.get("asset_id"), "asset_id"),
                 "pool_id": identifier(raw.get("pool_id"), "pool_id"),
                 "symbol": identifier(raw.get("symbol"), "symbol"),
                 "source_event_id": identifier(raw.get("event_id"), "event_id"),
                 "block_hash": identifier(raw.get("block_hash"), "block_hash"),
                 "block_number": timestamp(raw.get("block_number"), "block_number"),
                 "chain_id": timestamp(raw.get("chain_id"), "chain_id"),
                 "qualification_evidence": raw["qualification_evidence"],
                 "confirmation": confirmation.as_dict()}
        value["id"] = "pipeline-buy:" + _digest([value["source_event_id"], value["block_hash"]])
        return value

    def invalidate_after(self, block_number: int, *, now: int, chain_id: int = 4663) -> int:
        """Call after a collector reorg rewind, before ingesting replacement observations."""
        timestamp(block_number); timestamp(now); timestamp(chain_id)
        count = 0
        with self.ledger.transaction():
            rows = self.ledger.conn.execute("SELECT payload FROM events WHERE kind='pipeline_buy'").fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                if value["chain_id"] == chain_id and value["block_number"] > block_number:
                    count += self.ledger.record_event("pipeline-invalid:" + value["id"], "pipeline_invalidated",
                                                       {"buy_id": value["id"], "reason": "chain_reorganization"}, now)
            if count:
                # A reorg invalidates the research signal, not an executed sale.
                self.ledger.set_halt("chain_reorganization: review affected paper signals before resuming entries")
        return count

    def ingest(self, buys: list[dict], now: int) -> list[dict]:
        timestamp(now)
        if not isinstance(buys, list) or len(buys) > 10000:
            raise ValueError("buys must be a list of at most 10000 observations")
        results = []
        for raw in buys:
            try:
                value = self._buy(raw, now)
                self.ledger.record_event(value["id"], "pipeline_buy", value, value["confirmation"]["observed_at"])
            except (ValueError, TypeError) as exc:
                results.append(self._skip(str(exc), now, context={"event_id": raw.get("event_id") if isinstance(raw, dict) else None}))
        with self.ledger.transaction():
            rows = self.ledger.conn.execute("SELECT payload FROM events WHERE kind='pipeline_buy' AND observed_at>=? AND observed_at<=?",
                                           (now - self.policy.max_confirmation_age_seconds, now)).fetchall()
            invalid = {json.loads(r["payload"])["buy_id"] for r in self.ledger.conn.execute("SELECT payload FROM events WHERE kind='pipeline_invalidated'")}
        groups = defaultdict(list)
        for row in rows:
            value = json.loads(row["payload"])
            if value["id"] not in invalid:
                groups[(value["asset_id"], value["pool_id"])].append(value)
        for (asset_id, pool_id), observations in sorted(groups.items()):
            observations.sort(key=lambda v: (v["confirmation"]["observed_at"], v["id"]), reverse=True)
            selected, wallets, clusters, transactions = [], set(), set(), set()
            for value in observations:
                c = WalletConfirmation.from_dict(value["confirmation"])
                wallet = c.wallet.lower() if c.wallet.startswith("0x") else c.wallet
                tx_hash = c.tx_hash.lower() if c.tx_hash.startswith("0x") else c.tx_hash
                if wallet in wallets or c.cluster_id in clusters or tx_hash in transactions:
                    continue
                wallets.add(wallet); clusters.add(c.cluster_id); transactions.add(tx_hash)
                selected.append(value)
            signal = sorted(v["id"] for v in selected)
            if len(selected) < self.policy.min_independent_wallets:
                results.append(self._skip("insufficient_independent_wallets", now, asset_id, {"signal": signal, "count": len(selected)}))
                continue
            try:
                safety = self.safety_provider(asset_id, now)
                decision_at = max(now, int(self.clock()))
                if not isinstance(safety, SafetyEvidence):
                    results.append(self._skip("safety_evidence_unavailable", decision_at, asset_id, signal)); continue
                if safety.asset_id != asset_id or not 0 <= decision_at - safety.observed_at <= self.policy.max_evidence_age_seconds:
                    results.append(self._skip("safety_evidence_mismatched_or_stale", decision_at, asset_id, signal)); continue
                failure = next((f"safety_failed:{k}" for k, v in safety.checks.items() if v is False), None)
                failure = failure or next((f"safety_unknown:{k}" for k in self.policy.required_safety_checks if safety.checks.get(k) is not True), None)
                if failure:
                    results.append(self._skip(failure, decision_at, asset_id, {"signal": signal, "safety": safety.as_dict()})); continue
            except Exception as exc:
                results.append(self._skip("safety_provider_failed", max(now, int(self.clock())), asset_id, {"type": type(exc).__name__, "signal": signal})); continue
            try:
                quote = self.quote_provider(asset_id, pool_id, decision_at)
                filled_at = max(decision_at, int(self.clock()))
                if not isinstance(quote, MarketQuote) or quote.pool_id != pool_id:
                    results.append(self._skip("quote_unavailable_or_pool_mismatch", filled_at, asset_id, signal)); continue
            except Exception as exc:
                results.append(self._skip("quote_provider_failed", max(decision_at, int(self.clock())), asset_id, {"type": type(exc).__name__, "signal": signal})); continue
            event_id = "wallet-signal:" + _digest({"ids": signal, "safety": safety.as_dict(), "quote": quote.as_dict(), "policy": self.policy.version})
            candidate = Candidate(event_id, asset_id, selected[0]["symbol"], max(v["confirmation"]["observed_at"] for v in selected),
                                  [WalletConfirmation.from_dict(v["confirmation"]) for v in selected], safety,
                                  metadata={"decision_at": decision_at, "source_event_ids": signal, "pool_id": pool_id})
            results.append(self.engine.consider(candidate, quote, now=filled_at))
        return results
