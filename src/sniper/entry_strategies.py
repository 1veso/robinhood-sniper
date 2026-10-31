"""Independent, read-only entry signals over canonical purchase proofs.

The caller binds proofs to its source journal and rechecks their block hashes.
This module neither obtains quotes nor authorizes an order: liquidity, safety,
account risk, duplicate holdings and execution remain separate final checks.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from .adapters.evm import CHAIN_ID, USDG, WETH, address, decode_swap, hash32, verify_purchase
from .models import WalletConfirmation, json_safe
from .wallet_analysis import apply_controller_reviews, cluster_controllers


ENTRY_REVISION = "independent-whale-and-token-v3-v1"
REGISTRY_MAX_AGE_SECONDS = 300


def entry_rules(*, whale_eligibility="reviewed_tracked", whale_reentry="fresh_post_close"):
    """Return supported rules; constructing them does not activate a policy."""
    if whale_eligibility not in {"reviewed_tracked", "observed_recipient"}:
        raise ValueError("Unsupported whale eligibility")
    if whale_reentry not in {"fresh_post_close", "cooldown_24h"}:
        raise ValueError("Unsupported whale re-entry rule")
    return {
        "revision": ENTRY_REVISION, "chain_id": CHAIN_ID,
        "max_confirmation_age_seconds": 900, "minimum_spend_usd": "10",
        "whale_follow": {"eligibility": whale_eligibility, "required_wallets": 1,
                         "reentry": whale_reentry},
        "two_wallet_v3": {"eligibility": "observed_recipient", "required_wallets": 2,
                          "reentry": "fresh_post_close"},
    }


def _canonical(value):
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def rules_hash(rules):
    _validate_rules(rules)
    return hashlib.sha256(_canonical(rules).encode()).hexdigest()


def _validate_rules(rules):
    if not isinstance(rules, dict) or not isinstance(rules.get("whale_follow"), dict):
        raise ValueError("Invalid independent entry rules")
    expected = entry_rules(whale_eligibility=rules["whale_follow"].get("eligibility"),
                           whale_reentry=rules["whale_follow"].get("reentry"))
    # Canonical bytes distinguish True from 1, including chain/count fields.
    if _canonical(rules) != _canonical(expected):
        raise ValueError("Independent entry rules do not match a supported revision")


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid evidence timestamp")
    return value


def _decimal(value):
    if isinstance(value, (bool, float)):
        raise ValueError("Evidence amount must be a decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Evidence amount must be finite")
    return result


def _reviewed_record(record, now):
    """No legacy string attestation or expired identity review qualifies."""
    if (not isinstance(record, dict) or record.get("qualified") is not True
            or not isinstance(record.get("cluster_id"), str) or not record["cluster_id"].strip()):
        return False
    evidence = record.get("qualification_evidence")
    if not isinstance(evidence, dict) or evidence.get("method") != "receipt_fifo_with_costs_v1":
        return False
    review = evidence.get("controller_review")
    if not isinstance(review, dict):
        return False
    try:
        reviewed = _timestamp(review.get("reviewed_at"))
        expires = _timestamp(review.get("expires_at"))
        cutoff = _timestamp(evidence.get("history_cutoff"))
        refs = review.get("evidence_urls")
        return (reviewed <= now <= expires and 0 < expires - reviewed <= 7 * 86400
                and cutoff <= now and type(review.get("chain_id")) is int
                and review["chain_id"] == CHAIN_ID and type(review.get("history_cutoff")) is int
                and review["history_cutoff"] == cutoff
                and review.get("independence_reviewed") is True
                and review.get("cluster_id") == record["cluster_id"]
                and isinstance(review.get("scope"), str) and bool(review["scope"].strip())
                and isinstance(refs, list) and bool(refs)
                and all(isinstance(ref, str) and ref.startswith("https://") for ref in refs))
    except (ValueError, TypeError):
        return False


def _performance_report(report, now):
    """Recheck the existing numeric/cost screen, without inventing history."""
    try:
        return (isinstance(report, dict) and report.get("history_complete") is True
                and report.get('history_refresh_pending') is not True
                and report.get("performance_estimate_available") is True
                and report.get("performance_qualified") is True and report.get("is_eoa") is True
                and report.get("qualification_blockers") == []
                and _timestamp(report["cutoff"]) <= now
                and type(report["closed_positions"]) is int and report["closed_positions"] >= 20
                and Decimal("0.55") <= _decimal(report["win_rate"]) <= 1
                and 0 <= _decimal(report["top_token_profit_share"]) <= Decimal("0.50")
                and _decimal(report["average_hold_minutes"]) >= 30
                and _decimal(report["net_pnl_after_follower_stress_usd"]) > 0
                and _decimal(report["last_week_realized_less_open_basis_usd"]) >= 0
                and _decimal(report["gas_usd"]) >= 0
                and _decimal(report["open_cost_basis_usd"]) >= 0
                and _decimal(report["turnover_usd"]) > 0
                and _decimal(report["follower_cost_bps_each_side"]) == 100
                and report.get("open_inventory_mark") == "zero_lower_bound")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def load_wallet_registry(runtime, now):
    """Load only current, cost-accounted research plus bound controller reviews.

    An empty/missing/stale registry disables reviewed-whale signals only. The
    legacy wallets.json/string-evidence alert list is not a trading fallback.
    Prefer the atomic admission publication, independent of slow address export.
    Legacy installations retain the two-file validation and fail closed.
    """
    now = _timestamp(now)
    runtime = Path(runtime)
    result = {"wallets": {}, "status": "UNAVAILABLE", "reason": None,
              "source": "research/qualified-wallets.json", "checked_at": now,
              "updated_at": None, "evidence_hash": None}
    try:
        folder = runtime / "research"
        publication = folder / 'registry-publication.json'
        if publication.exists():
            result['source'] = 'research/registry-publication.json'
            summary = json.loads(publication.read_text(encoding='utf-8'))
            if not isinstance(summary, dict) or summary.get('schema') != 'wallet_registry_v1':
                raise ValueError('Invalid registry publication')
            qualified = summary.get('qualified_wallets')
        else:
            summary = json.loads((folder / "wallet-research.json").read_text(encoding="utf-8"))
            qualified = json.loads((folder / "qualified-wallets.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict) or not isinstance(qualified, dict):
            raise ValueError("Malformed registry publication")
        updated = _timestamp(summary.get("updated_at"))
        result["updated_at"] = updated
        if not 0 <= now - updated <= REGISTRY_MAX_AGE_SECONDS:
            result.update(status="STALE", reason="research_publication_stale")
            return result
        reports = summary.get("reports")
        if not isinstance(reports, list):
            raise ValueError("Missing research reports")
        reviews_path = runtime / "controller-reviews.json"
        reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {}
        if not isinstance(reviews, dict):
            raise ValueError("Malformed controller reviews")
        normalized = []
        seen = set()
        for original in reports:
            if not isinstance(original, dict):
                raise ValueError("Malformed research report")
            wallet = address(original.get("wallet"))
            if wallet in seen:
                raise ValueError("Duplicate wallet research report")
            seen.add(wallet)
            report = deepcopy(original)
            report["wallet"] = wallet
            # Keep all graph members, including unqualified wallets: removing a
            # linked report could incorrectly dissolve an identity conflict.
            report["performance_qualified"] = _performance_report(report, now)
            normalized.append(report)
        expected = apply_controller_reviews(cluster_controllers(normalized), reviews, now=now)
        selected = {}
        for raw_wallet, record in qualified.items():
            wallet = address(raw_wallet)
            if wallet in selected:
                raise ValueError("Duplicate qualified wallet")
            if (_reviewed_record(record, now) and wallet in expected
                    and _canonical(record) == _canonical(expected[wallet])):
                selected[wallet] = deepcopy(record)
        result.update(wallets=selected, status="READY" if selected else "EMPTY",
                      reason=None if selected else "no_current_reviewed_wallets")
        result["evidence_hash"] = hashlib.sha256(_canonical({"updated_at": updated,
                                                     "wallets": selected}).encode()).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        # Do not publish raw file contents or exception text (paths may contain
        # private metadata). A subsequent publication may recover on its own.
        result.update(wallets={}, status="UNAVAILABLE", reason="registry_evidence_unavailable")
    return result


def _valid_proof(proof, token, now, rules):
    try:
        if not isinstance(proof, dict):
            return False
        chain = proof.get("chain_id", CHAIN_ID)
        if (type(chain) is not int or chain != rules["chain_id"]
                or proof.get("verified_swap") is not True or proof.get("factory_verified") is not True
                or address(proof["token_address"]) != token):
            return False
        # Legacy proofs omitted chain_id. They are only usable because the
        # caller's canonical journal is explicitly bound to chain 4663.
        address(proof["wallet"])
        address(proof["pool"])
        hash32(proof["tx_hash"])
        hash32(proof["block_hash"])
        _timestamp(proof["block_number"])
        _timestamp(proof["log_index"])
        observed = _timestamp(proof["observed_at"])
        base = address(proof["quote_address"])
        return (base in {WETH, USDG} and token not in {WETH, USDG}
                and type(proof["fee"]) is int and 0 < proof["fee"] < 1_000_000
                and 0 <= now - observed <= rules["max_confirmation_age_seconds"]
                and _decimal(proof["spent_usd"]) >= _decimal(rules["minimum_spend_usd"])
                and type(proof["amount_raw"]) is str and proof["amount_raw"].isdigit()
                and int(proof["amount_raw"]) > 0
                and type(proof["spent_raw"]) is str and proof["spent_raw"].isdigit()
                and int(proof["spent_raw"]) > 0)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _direct_purchase(proof):
    """Reverify direct wallet funding; a descriptive proof_scope is insufficient."""
    try:
        receipt, transaction = proof["receipt"], proof["transaction"]
        if not isinstance(receipt, dict) or not isinstance(transaction, dict):
            return False
        token, base, pool = (address(proof[k]) for k in ("token_address", "quote_address", "pool"))
        token0, token1 = sorted((token, base))
        for raw in receipt["logs"]:
            try:
                swap = decode_swap(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            if (swap["address"] != pool or swap["tx_hash"] != hash32(proof["tx_hash"])
                    or swap["log_index"] != proof["log_index"]
                    or swap["block_hash"] != hash32(proof["block_hash"])
                    or swap["block_number"] != proof["block_number"]):
                continue
            checked = verify_purchase(swap, {"pool": pool, "token0": token0, "token1": token1},
                                      receipt, transaction, proof["wallet"], {WETH: "WETH", USDG: "USDG"})
            return bool(checked and all(checked[key] == proof[key] for key in checked))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return False


def _distinct(proofs, limit=None):
    """Maximum wallet/transaction matching after strategy-specific filtering."""
    choices, owners, selected = {}, {}, {}
    for proof in sorted(proofs, key=lambda p: (-p["observed_at"], p["tx_hash"], p["log_index"], p["pool"])):
        choices.setdefault(address(proof["wallet"]), {}).setdefault(hash32(proof["tx_hash"]), proof)
    def assign(wallet, visited):
        for tx, proof in choices[wallet].items():
            if tx in visited:
                continue
            visited.add(tx)
            owner = owners.get(tx)
            if owner is None or assign(owner, visited):
                owners[tx], selected[wallet] = wallet, proof
                return True
        return False
    for wallet in choices:
        assign(wallet, set())
        if limit is not None and len(selected) >= limit:
            break
    return sorted(selected.values(), key=lambda p: (-p["observed_at"], p["wallet"], p["tx_hash"]))


def evaluate_signals(proofs, *, token, now, last_closed_at, rules, registry):
    """Return independent A/B eligibility snapshots, without shared entry gates."""
    _validate_rules(rules)
    token, now = address(token), _timestamp(now)
    if last_closed_at is not None and _timestamp(last_closed_at) > now:
        raise ValueError("Last close cannot be in the future")
    if not isinstance(proofs, (list, tuple)):
        raise ValueError("Purchase proofs must be a sequence")
    valid = [p for p in proofs if _valid_proof(p, token, now, rules)]
    registry = registry if isinstance(registry, dict) else {}
    registry_current = (registry.get("status") == "READY"
        and type(registry.get("checked_at")) is int and 0 <= now - registry["checked_at"] <= REGISTRY_MAX_AGE_SECONDS
        and type(registry.get("updated_at")) is int and 0 <= now - registry["updated_at"] <= REGISTRY_MAX_AGE_SECONDS)
    wallets = registry.get("wallets", {}) if registry_current else {}
    wallets = wallets if isinstance(wallets, dict) else {}
    snapshots = []
    for strategy_id in ("whale_follow", "two_wallet_v3"):
        rule = rules[strategy_id]
        reviewed = rule["eligibility"] == "reviewed_tracked"
        eligible = [p for p in valid if not reviewed or (
            _reviewed_record(wallets.get(address(p["wallet"])), now) and _direct_purchase(p))]
        if last_closed_at is not None and rule["reentry"] == "fresh_post_close":
            eligible = [p for p in eligible if p["observed_at"] > last_closed_at]
        # A minimal deterministic witness bounds canonical RPC checks to A1/B2.
        # All other purchases and their complete evidence remain in the source
        # journal; unchecked extra buyers are not claimed as fresh qualification.
        selected = _distinct(eligible, limit=rule['required_wallets'])
        cooldown = (last_closed_at is not None and rule["reentry"] == "cooldown_24h"
                    and now - last_closed_at < 86400)
        enough = len(selected) >= rule["required_wallets"]
        qualified = enough and not cooldown
        reason = ("cooldown_24h" if cooldown else "qualified" if qualified
                  else "reviewed_wallet_registry_unavailable" if reviewed and not registry_current
                  else "no_eligible_tracked_purchase" if reviewed
                  else "insufficient_fresh_post_close_buyers" if last_closed_at is not None
                  else "insufficient_distinct_buyers")
        confirmations = [WalletConfirmation(address(p["wallet"]),
            wallets[address(p["wallet"])]["cluster_id"] if reviewed else "unreviewed:" + address(p["wallet"]),
            hash32(p["tx_hash"]), p["observed_at"], _decimal(p["spent_usd"]), True, reviewed).as_dict()
            for p in selected]
        snapshots.append({
            "revision": ENTRY_REVISION, "rules_hash": rules_hash(rules), "strategy_id": strategy_id,
            "chain_id": CHAIN_ID, "token_address": token, "evaluated_at": now,
            "qualified": qualified, "qualification_reason": reason,
            "observed_wallets": len(selected), "required_wallets": rule["required_wallets"],
            "source_pools": sorted({address(p["pool"]) for p in selected}),
            "source_proofs": deepcopy(json_safe(selected)), "confirmations": confirmations,
            "wallet_qualification": rule["eligibility"],
            "wallet_qualification_evidence": {address(p["wallet"]): deepcopy(wallets[address(p["wallet"])])
                for p in selected} if reviewed else {},
            "registry_evidence_hash": registry.get("evidence_hash") if reviewed else None,
            "controller_independence": "current_external_review" if reviewed else "unestablished",
            "reentry_rule": rule["reentry"], "last_closed_at": last_closed_at,
            "rejected_proof_count": len(proofs) - len(valid),
            "scope": "entry_signal_only; shared safety, route and account checks still required",
        })
    return snapshots
