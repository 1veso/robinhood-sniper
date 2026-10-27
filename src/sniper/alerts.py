"""Qualify informational launch alerts; never changes the trading policy or ledger."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time

from .adapters.evm import CHAIN_ID, EvmRegistry, address
from .models import SafetyEvidence


def reviewed_wallets(raw, *, now=None):
    if not isinstance(raw, dict):
        raise ValueError("Wallet manifest must map addresses to qualification records")
    selected = {}
    now = int(time.time()) if now is None else now
    for wallet, value in raw.items():
        if (isinstance(value, dict) and value.get("qualified") is True
                and isinstance(value.get("cluster_id"), str) and value["cluster_id"].strip()
                and value.get("qualification_evidence")):
            evidence = value["qualification_evidence"]
            if isinstance(evidence, dict) and "controller_review" in evidence:
                review = evidence["controller_review"]
                if (not isinstance(review, dict) or type(review.get("reviewed_at")) is not int
                        or type(review.get("expires_at")) is not int
                        or not review["reviewed_at"] <= now <= review["expires_at"]
                        or not 0 < review["expires_at"] - review["reviewed_at"] <= 7 * 86400
                        or review.get("chain_id") != CHAIN_ID or review.get("independence_reviewed") is not True
                        or review.get("history_cutoff") != evidence.get("history_cutoff")):
                    continue
            selected[address(wallet)] = value
    return selected


def fresh(timestamp, now, age):
    return type(timestamp) is int and 0 <= now - timestamp <= age


def positive(value):
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def qualify(pool, buys, wallets, safety_raw, onchain, policy, now):
    """One corroborated purchase is enough for an alert, never for a paper entry."""
    if (pool.get("canonical") is not True or pool.get("chain_id") != CHAIN_ID
            or not fresh(pool.get("chain_timestamp"), now, 86400)):
        return None, "pool_not_canonical_or_not_recent"
    bases = EvmRegistry().bases
    if (pool.get("token0") in bases) == (pool.get("token1") in bases):
        return None, "unsupported_pair"
    token = pool["token1"] if pool["token0"] in bases else pool["token0"]
    asset = f"eip155:{CHAIN_ID}/erc20:{token}"
    try:
        safety = SafetyEvidence.from_dict(safety_raw)
    except (ValueError, TypeError, KeyError):
        return None, "safety_evidence_missing_or_malformed"
    if safety.asset_id != asset or not fresh(safety.observed_at, now, policy.max_evidence_age_seconds):
        return None, "safety_evidence_stale_or_mismatched"
    if any(v is False for v in safety.checks.values()) or any(safety.checks.get(k) is not True for k in policy.required_safety_checks):
        return None, "safety_checks_failed_or_unknown"
    if (not isinstance(onchain, dict) or onchain.get("asset_id") != asset
            or not fresh(onchain.get("observed_at"), now, policy.max_evidence_age_seconds)
            or onchain.get("checks", {}).get("contract_present") is not True):
        return None, "current_contract_observation_missing"
    selected = []
    for buy in buys:
        wallet = buy.get("wallet")
        reviewed = wallets.get(wallet)
        if (buy.get("canonical") is True and buy.get("kind") == "verified_buy"
                and buy.get("chain_id") == CHAIN_ID and buy.get("asset_id") == asset
                and buy.get("pool_id") == pool["pool"] and buy.get("qualified") is True
                and buy.get("verified_swap") is True and buy.get("qualification_evidence")
                and reviewed and reviewed.get("qualified") is True
                and reviewed.get("cluster_id") == buy.get("cluster_id")
                and reviewed.get("qualification_evidence") == buy.get("qualification_evidence")
                and fresh(buy.get("chain_timestamp"), now, policy.max_confirmation_age_seconds)
                and type(buy.get("block_number")) is int and buy["block_number"] >= pool["block_number"]
                and positive(buy.get("amount")) and positive(buy.get("spent_amount"))):
            selected.append(buy)
    if not selected:
        return None, "no_recent_verified_reviewed_wallet_purchase"
    buy = max(selected, key=lambda item: (item["chain_timestamp"], item["event_id"]))
    quote = bases.get(buy.get("quote_address"), "base token")
    # Plain text avoids interpreting token names or evidence labels as HTML.
    symbol = " ".join(str(buy.get("symbol", "Unknown")).split())[:60]
    checked_at = datetime.fromtimestamp(safety.observed_at, timezone.utc).isoformat()
    message = (f"SNIPER • PAPER / RESEARCH ALERT\nNew pool + reviewed-wallet purchase\n\n"
        f"Token: {symbol}\nContract: {token}\nPool: {pool['pool']}\nChain: Robinhood (4663)\n"
        f"Buyer: {buy['wallet']}\nReceived: {str(buy['amount'])[:80]} tokens\n"
        f"Spent: {str(buy['spent_amount'])[:80]} {quote}\nPurchase block: {buy['block_number']}\n"
        f"Safety: configured checks passed at {checked_at}\n"
        f"Source: {' '.join(safety.source.split())[:100]}\n\n"
        f"Token: https://robinhoodchain.blockscout.com/address/{token}\n"
        f"Purchase: https://robinhoodchain.blockscout.com/tx/{buy['tx_hash']}\n\n"
        "Informational only. No order placed. One buyer does not satisfy the paper-entry quorum; "
        "screening does not guarantee safety or an executable price.")
    payload = {"token": token, "pool": pool, "buy": buy, "message": message,
               "created_at": now, "expires_at": min(now + 300, safety.observed_at + policy.max_evidence_age_seconds,
                   buy["chain_timestamp"] + policy.max_confirmation_age_seconds),
               "anchors": [[pool["block_number"], pool["block_hash"]], [buy["block_number"], buy["block_hash"]]]}
    return payload, "qualified_information_alert"
