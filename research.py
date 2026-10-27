"""Discover wallet candidates and resume bounded historical qualification work."""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.adapters.evm import RpcClient, RpcError, DEFAULT_RPC, USDG, SWAP_TOPIC, decode_swap, address, hash32, quantity
from sniper.adapters.wallet_history import ResearchCache, ResearchPending, block_at_or_before, reconstruct_wallet
from sniper.wallet_analysis import analyse_wallet, cluster_controllers, apply_controller_reviews
from supervise import atomic_json, child_environment, instance_lock


def discover(cache, state, *, blocks=1000, sample=12):
    rpc = cache.rpc
    rpc.verify_chain()
    head = quantity(rpc.call("eth_blockNumber")) - 5
    header = rpc.call("eth_getBlockByNumber", [hex(head), False])
    if not isinstance(header, dict) or quantity(header["number"]) != head:
        raise ValueError("Discovery snapshot unavailable")
    if state.get("cursor"):
        previous = rpc.call("eth_getBlockByNumber", [hex(state["cursor"]), False])
        if previous["hash"] != state["cursor_hash"]:
            raise ResearchPending("Discovery reorg detected; review snapshot before restarting research")
    start = max(0, state.get("cursor", head - blocks) + 1)
    if start > head:
        return state, {"new_candidates": 0, "sampled_transactions": 0}
    end = min(head, start + blocks - 1)
    anchor = rpc.call("eth_getBlockByNumber", [hex(end), False])
    logs = cache.call("eth_getLogs", [{"topics": [SWAP_TOPIC], "fromBlock": hex(start), "toBlock": hex(end)}], anchor=anchor["hash"])
    if not isinstance(logs, list):
        raise ValueError("Invalid discovery logs")
    swaps = [decode_swap(log) for log in logs]
    if any(not start <= s["block_number"] <= end for s in swaps):
        raise ValueError("Discovery event is outside requested interval")
    by_tx = {s["tx_hash"]: s for s in sorted(swaps, key=lambda s: (s["block_number"], s["log_index"]))}
    tx_hashes = list(by_tx)
    # Uniform deterministic sample across the interval, rather than a largest-
    # balance leaderboard. Sampling affects discovery only, never history coverage.
    chosen = [tx_hashes[i * len(tx_hashes) // min(sample, len(tx_hashes))] for i in range(min(sample, len(tx_hashes)))] if tx_hashes else []
    candidates = dict(state.get("candidates", {}))
    added = 0
    for tx_hash in chosen:
        swap = by_tx[tx_hash]
        tx = cache.call("eth_getTransactionByHash", [tx_hash], anchor=anchor["hash"])
        receipt = cache.call("eth_getTransactionReceipt", [tx_hash], anchor=anchor["hash"])
        if not isinstance(tx, dict) or not isinstance(receipt, dict):
            raise ResearchPending("Discovery transaction unavailable")
        if hash32(tx["hash"]) != tx_hash or hash32(receipt["transactionHash"]) != tx_hash:
            raise ValueError("Discovery transaction mismatch")
        if quantity(receipt["status"]) != 1 or receipt["blockHash"] != swap["block_hash"] or tx.get("blockHash") != receipt["blockHash"]:
            raise ValueError("Discovery receipt is not canonical and successful")
        if not any(decode_swap(log) == swap for log in receipt.get("logs", [])
                   if (log.get("topics") or [None])[0] == SWAP_TOPIC):
            raise ValueError("Discovery receipt lacks sampled swap")
        wallet = address(tx["from"])
        if wallet not in candidates:
            if len(candidates) >= 5000:
                raise ResearchPending("Candidate capacity reached; review research universe")
            added += 1
            candidates[wallet] = {"wallet": wallet, "first_seen_at": int(time.time()), "sampled_transactions": 0,
                                  "example_transaction": tx_hash, "example_pool": swap["address"],
                                  "qualified": False, "source": "successful swap-transaction sender; route qualification pending"}
        candidates[wallet]["sampled_transactions"] += 1
        candidates[wallet]["last_seen_at"] = int(time.time())
    check = rpc.call("eth_getBlockByNumber", [hex(end), False])
    if check["hash"] != anchor["hash"]:
        raise ResearchPending("Chain changed during discovery; interval will retry")
    state.update(cursor=end, cursor_hash=anchor["hash"], candidates=candidates,
                 first_block=state.get("first_block", start), updated_at=int(time.time()))
    return state, {"new_candidates": added, "sampled_transactions": len(chosen), "observed_swap_events": len(swaps),
                   "observed_pools": len({s["address"] for s in swaps}), "from_block": start, "to_block": end,
                   "head": head, "lag_blocks": head - end, "coverage": "sampled swap senders; history qualification is separate"}


def run_research(runtime: Path, *, max_calls=60, seconds=45):
    folder = runtime / "research"
    folder.mkdir(exist_ok=True)
    env = child_environment(runtime)
    key_file = runtime / "blockscout-api-key.txt"
    key = key_file.read_text(encoding="utf-8-sig").strip() if key_file.exists() else os.environ.get("BLOCKSCOUT_API_KEY", "")
    rpc = RpcClient(env.get("SNIPER_RPC_URL") or DEFAULT_RPC, timeout=10, max_retries=1)
    cache = ResearchCache(folder / "cache", rpc, blockscout_key=key, max_calls=max_calls, seconds=seconds)
    path = folder / "discovery.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    pending = []
    discovery = {}
    try:
        state, discovery = discover(cache, state)
        atomic_json(path, state)
    except ResearchPending as exc:
        pending.append(str(exc))
    candidates = state.get("candidates", {})
    snapshot_path = folder / "history-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
    # Daily cohorts retain earlier results without mixing observation cutoffs.
    if snapshot and time.time() - snapshot["timestamp"] > 86400:
        snapshot = None
    cohort = folder / f"cohort-{snapshot['block']}" if snapshot else None
    snapshot_verified = False
    if not key:
        pending.append("BLOCKSCOUT_API_KEY_REQUIRED")
    elif candidates:
        try:
            if snapshot is None:
                end = quantity(rpc.call("eth_blockNumber")) - 5
                header = rpc.call("eth_getBlockByNumber", [hex(end), False])
                snapshot = {"block": end, "timestamp": quantity(header["timestamp"]), "hash": hash32(header["hash"])}
                snapshot["start_block"] = block_at_or_before(cache, snapshot["timestamp"] - 30 * 86400, end, snapshot["hash"])
                atomic_json(snapshot_path, snapshot)
            cohort = folder / f"cohort-{snapshot['block']}"
            cohort.mkdir(exist_ok=True)
            canonical = rpc.call("eth_getBlockByNumber", [hex(snapshot["block"]), False])
            if canonical["hash"] != snapshot["hash"]:
                raise ResearchPending("Frozen history snapshot reorged; do not use its qualifications")
            try:
                cache.call("eth_call", [{"to": USDG, "data": "0x313ce567"}, hex(snapshot["start_block"])], anchor=snapshot["hash"])
            except RpcError:
                raise ResearchPending("ARCHIVE_RPC_REQUIRED: historical state unavailable; discovery continues") from None
            snapshot_verified = True
            # Oldest unprocessed candidates first. Cache and budgets allow large
            # histories to continue across invocations instead of being truncated.
            attempts_path = cohort / "attempts.json"
            attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else {}
            queue = sorted(candidates.items(), key=lambda x: (x[1]["first_seen_at"], x[0]))
            queue = [(w, c) for w, c in queue if not (cohort / f"wallet-{w}.json").exists()][:100]
            queue.sort(key=lambda x: (attempts.get(x[0], 0), x[1]["first_seen_at"], x[0]))
            for wallet, candidate in queue:
                report_path = cohort / f"wallet-{wallet}.json"
                if report_path.exists():
                    continue
                attempts[wallet] = int(time.time())
                atomic_json(attempts_path, attempts)
                history = reconstruct_wallet(cache, wallet, snapshot)
                atomic_json(cohort / f"history-{wallet}.json", history)
                report = analyse_wallet(history)
                atomic_json(report_path, report)
                break
        except ResearchPending as exc:
            pending.append(str(exc))
        except (ValueError, KeyError, RuntimeError) as exc:
            pending.append(f"{type(exc).__name__}: historical evidence failed validation")
    reports = [json.loads(p.read_text()) for p in sorted(cohort.glob("wallet-0x*.json"))] if cohort and cohort.exists() else []
    reports = cluster_controllers(reports)
    reviews_path = runtime / "controller-reviews.json"
    reviews = json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    if not isinstance(reviews, dict):
        raise ValueError("Controller reviews must be an object")
    selected = apply_controller_reviews(reports, reviews, now=int(time.time())) if snapshot_verified else {}
    atomic_json(folder / "qualified-wallets.json", selected)
    ranked = sorted(reports, key=lambda r: (not r["performance_qualified"], Decimal(r["top_token_profit_share"]),
                    -min(Decimal(r["average_hold_minutes"]), Decimal(1440)), -r["closed_positions"],
                    -Decimal(r["net_pnl_after_follower_stress_usd"]), r["wallet"]))
    shortlist = [{"rank": i + 1, **r} for i, r in enumerate(ranked) if r["performance_qualified"]]
    summary = {"mode": "RESEARCH_ONLY", "updated_at": int(time.time()),
               "discovery": discovery, "candidate_count": len(candidates), "history_reports": len(reports),
               "performance_qualified_count": len(shortlist), "approved_wallet_count": len(selected),
               "pending": pending, "snapshot": snapshot, "shortlist": shortlist,
               "controller_policy": "Automatic links merge suspected controllers. No observed link is not proof of independence; no automatic qualified:true assertion.",
               "reports": reports, "candidate_file": str(folder / "wallet-candidates.csv")}
    atomic_json(folder / "wallet-research.json", summary)
    atomic_json(folder / "wallet-shortlist.json", shortlist)
    csv_path = folder / "wallet-candidates.csv"
    with csv_path.with_suffix(".tmp").open("w", newline="", encoding="utf-8") as handle:
        fields = ["wallet", "sampled_transactions", "first_seen_at", "last_seen_at", "example_transaction", "qualified"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(candidates.values(), key=lambda r: (-r["sampled_transactions"], r["wallet"])))
    csv_path.with_suffix(".tmp").replace(csv_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=60)
    parser.add_argument("--seconds", type=int, default=45)
    args = parser.parse_args()
    if not 10 <= args.calls <= 500 or not 15 <= args.seconds <= 300:
        parser.error("calls must be 10..500 and seconds 15..300")
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    try:
        with instance_lock(runtime / "research.lock"):
            result = run_research(runtime, max_calls=args.calls, seconds=args.seconds)
    except Exception as exc:
        print(json.dumps({"mode": "RESEARCH_ONLY", "error": type(exc).__name__, "qualified": False}))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k not in ("reports", "shortlist")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
