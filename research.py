"""Discover wallet candidates and resume bounded historical qualification work."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.adapters.evm import RpcClient, RpcError, DEFAULT_RPC, USDG, SWAP_TOPIC, decode_swap, address, hash32, quantity
from sniper.adapters.wallet_history import ResearchCache, ResearchPending, CoverageExceeded, block_at_or_before, reconstruct_wallet
from sniper.wallet_analysis import analyse_wallet, cluster_controllers, apply_controller_reviews
from sniper.candidate_evidence import CandidateEvidence
from sniper.research_queue import HistoryQueue, DAY
from supervise import atomic_json, child_environment, instance_lock


def sync_observed_evidence(runtime, state, evidence):
    """Import a bounded oldest-first batch; the evidence journal owns its checkpoint."""
    path = runtime / 'exploration/events.sqlite3'
    if not path.exists():
        return {'added_candidates': 0, 'imported_observations': 0, 'remaining': 0, 'status': 'source_unavailable'}
    manifest = runtime / 'exploration/trial.json'
    source_id = json.loads(manifest.read_text()).get('source_id') if manifest.exists() else None
    return evidence.import_confirmations(path, state, limit=500, expected_source_id=source_id)


def import_observed_buyers(runtime, state):
    """Compatibility API: durable canonical-recipient import, no qualification."""
    folder = runtime / 'research'
    folder.mkdir(exist_ok=True)
    with closing(CandidateEvidence(folder / 'candidate-evidence.sqlite3')) as evidence:
        return sync_observed_evidence(runtime, state, evidence)['added_candidates']


def discover(cache, state, *, blocks=2000, sample=12, evidence=None):
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
    while True:
        anchor = rpc.call("eth_getBlockByNumber", [hex(end), False])
        try:
            logs = cache.call("eth_getLogs", [{"topics": [SWAP_TOPIC], "fromBlock": hex(start), "toBlock": hex(end)}], anchor=anchor["hash"])
            break
        except RpcError:
            # Dense intervals can exceed the provider's event/response limit.
            # Retry a smaller prefix; never advance over the unqueried suffix.
            width = end - start + 1
            if width <= 250:
                raise
            end = start + max(250, width // 2) - 1
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
    # Never mutate state on a timed-out/reorged pass; nested candidate counters
    # must not leak into the later CSV when the anchor check fails.
    candidates = deepcopy(state.get("candidates", {}))
    observations = []
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
        block_header = cache.call('eth_getBlockByNumber', [hex(swap['block_number']), False], anchor=anchor['hash'])
        if (not isinstance(block_header, dict) or quantity(block_header['number']) != swap['block_number']
                or hash32(block_header['hash']) != swap['block_hash']
                or quantity(receipt['blockNumber']) != swap['block_number']
                or quantity(tx['blockNumber']) != swap['block_number']):
            raise ResearchPending('Discovery sampled block changed; interval will retry')
        observed_at = quantity(block_header['timestamp'])
        wallet = address(tx["from"])
        # Provider/sample budgets bound each pass. A lifetime candidate cap would
        # permanently wedge this contiguous cursor and the canonical buyer queue.
        if wallet not in candidates:
            added += 1
            candidates[wallet] = {"wallet": wallet, "first_seen_at": int(time.time()), "sampled_transactions": 0,
                                  "example_transaction": tx_hash, "example_pool": swap["address"],
                                  "qualified": False, "source": "successful swap-transaction sender; route qualification pending"}
        candidates[wallet]["sampled_transactions"] += 1
        candidates[wallet]["last_seen_at"] = max(candidates[wallet].get('last_seen_at', 0), observed_at)
        observations.append({'source_id':'rpc-swap-discovery-v1', 'source':'rpc_swap_sample',
            'wallet':wallet, 'role':'transaction_sender', 'tx_hash':tx_hash,
            'log_index':swap['log_index'], 'block_number':swap['block_number'], 'block_hash':swap['block_hash'],
            'observed_at':observed_at, 'pool':swap['address'], 'canonical_at_capture':True,
            'transaction':tx, 'receipt':receipt, 'block_header':block_header,
            'proof_scope':'successful swap transaction sender; recipient, route and funding ownership not established',
            'interval_anchor':{'block':end,'hash':anchor['hash']}})
    check = rpc.call("eth_getBlockByNumber", [hex(end), False])
    if check["hash"] != anchor["hash"]:
        raise ResearchPending("Chain changed during discovery; interval will retry")
    if evidence is not None:
        evidence.add_sender_observations(observations)
    state.update(cursor=end, cursor_hash=anchor["hash"], candidates=candidates,
                 first_block=state.get("first_block", start), updated_at=int(time.time()))
    return state, {"new_candidates": added, "sampled_transactions": len(chosen), "observed_swap_events": len(swaps),
                   "observed_pools": len({s["address"] for s in swaps}), "from_block": start, "to_block": end,
                   "head": head, "lag_blocks": head - end, "coverage": "sampled swap senders; history qualification is separate"}


def run_research(runtime: Path, *, max_calls=60, seconds=45):
    folder = runtime / 'research'
    folder.mkdir(exist_ok=True)
    progress = {'run_id': uuid.uuid4().hex, 'pid': os.getpid(), 'started_at': int(time.time()),
                'finished_at': None, 'status': 'RUNNING'}
    if os.name == 'nt':
        import ctypes
        session = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            progress['windows_session_id'] = session.value
    def phase(name):
        progress.update(phase=name, updated_at=int(time.time()))
        atomic_json(folder/'publication-status.json', progress)
    try:
        phase('OPEN_JOURNAL')
        with closing(CandidateEvidence(folder / 'candidate-evidence.sqlite3')) as evidence:
            result = _run_research(runtime, evidence, max_calls=max_calls, seconds=seconds, phase=phase)
        progress.update(status='COMPLETE', finished_at=int(time.time()))
        phase('COMPLETE')
        return result
    except Exception as exc:
        progress.update(status='ERROR', finished_at=int(time.time()), error_type=type(exc).__name__)
        phase(progress['phase'])
        raise


def _run_research(runtime: Path, evidence, *, max_calls=60, seconds=45, phase=lambda name: None):
    started = time.monotonic()
    folder = runtime / "research"
    folder.mkdir(exist_ok=True)
    env = child_environment(runtime)
    key_file = runtime / "blockscout-api-key.txt"
    key = key_file.read_text(encoding="utf-8-sig").strip() if key_file.exists() else os.environ.get("BLOCKSCOUT_API_KEY", "")
    rpc = RpcClient(env.get("SNIPER_RPC_URL") or DEFAULT_RPC, timeout=10, max_retries=1)
    discovery_rpc = RpcClient(env.get("SNIPER_OBSERVATION_RPC_URL") or rpc.url, timeout=10, max_retries=1)
    discovery_seconds = min(10, seconds / 4)
    discovery_calls = min(12, max(4, max_calls // 4))
    cache = ResearchCache(folder / "cache", discovery_rpc, blockscout_key=key,
                          max_calls=discovery_calls, seconds=discovery_seconds)
    path = folder / "discovery.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    phase('IMPORT')
    imported = sync_observed_evidence(runtime, state, evidence)
    if state.get('candidates'):
        atomic_json(path, state)
    pending = []
    discovery = {}
    phase('DISCOVERY')
    try:
        # Discovery must not spend the history worker's budget. Once candidates
        # exist, a five-minute discovery cadence leaves time to finish histories.
        if not state.get("candidates") or time.time() - state.get("updated_at", 0) >= 300:
            with discovery_rpc.budget(discovery_seconds):
                # Each sampled transaction needs tx + receipt + canonical header;
                # reserve one call for logs without consuming the history budget.
                state, discovery = discover(cache, state, sample=max(1, (discovery_calls - 1) // 3), evidence=evidence)
            atomic_json(path, state)
        else:
            discovery = {"status": "cached", "cursor": state.get("cursor"),
                         "next_due_at": state.get("updated_at", 0) + 300}
    except ResearchPending as exc:
        pending.append(str(exc))
    except RpcError as exc:
        pending.append(f"DISCOVERY_RPC_UNAVAILABLE: {exc}")
    except (ValueError, KeyError, TypeError):
        pending.append('DISCOVERY_EVIDENCE_INVALID: checkpoint retained; no unverified sender added')
    cache = ResearchCache(folder / "cache", rpc, blockscout_key=key,
                          max_calls=max_calls - discovery_calls,
                          seconds=seconds - discovery_seconds)
    candidates = state.get("candidates", {})
    snapshot_path = folder / "history-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
    # Existing work keeps its exact anchor. New/refresh jobs get a recent
    # snapshot independently, so a large unfinished cohort cannot freeze all
    # future research at its original date.
    queue = HistoryQueue(folder, candidates, snapshot, int(time.time()), atomic_json) if snapshot else None
    verified = set()
    def new_snapshot():
        end = quantity(rpc.call("eth_blockNumber")) - 5
        header = rpc.call("eth_getBlockByNumber", [hex(end), False])
        value = {"block": end, "timestamp": quantity(header["timestamp"]), "hash": hash32(header["hash"])}
        value["start_block"] = block_at_or_before(cache, value["timestamp"] - 30 * DAY, end, value["hash"])
        return value
    def current_snapshot(required_at=0):
        path = folder / 'history-current-snapshot.json'
        value = json.loads(path.read_text()) if path.exists() else snapshot
        if not 0 <= int(time.time()) - value['timestamp'] < DAY or required_at > value['timestamp']:
            value = new_snapshot()
            atomic_json(path, value)
        if value['timestamp'] < required_at:
            raise ResearchPending('Confirmed history anchor has not reached candidate activity; retry later')
        return value
    def verify_snapshot(value):
        if value['hash'] in verified:
            return
        canonical = rpc.call("eth_getBlockByNumber", [hex(value['block']), False])
        if canonical['hash'] != value['hash']:
            raise ResearchPending("Frozen history snapshot reorged; do not use its qualifications")
        try:
            cache.call("eth_call", [{"to": USDG, "data": "0x313ce567"}, hex(value['start_block'])], anchor=value['hash'])
        except RpcError:
            raise ResearchPending("ARCHIVE_RPC_REQUIRED: historical state unavailable; discovery continues") from None
        verified.add(value['hash'])
    phase('HISTORY')
    if not key:
        pending.append("BLOCKSCOUT_API_KEY_REQUIRED")
    elif candidates:
        try:
            rpc.verify_chain()
            if snapshot is None:
                snapshot = new_snapshot()
                atomic_json(snapshot_path, snapshot)
                queue = HistoryQueue(folder, candidates, snapshot, int(time.time()), atomic_json)
            # Verify reviewed completed reports even when no new job is due.
            # Mixed cohorts never inherit another snapshot's canonical check.
            for report in queue.reports():
                if report['performance_qualified']:
                    verify_snapshot(report['history_snapshot'])
            for wallet in queue.next_wallets():
                job_snapshot = queue.begin(wallet, lambda: current_snapshot(candidates[wallet].get('last_seen_at', 0)))
                verify_snapshot(job_snapshot)
                cohort = folder / f"cohort-{job_snapshot['block']}"
                cohort.mkdir(exist_ok=True)
                report_path = cohort / f"wallet-{wallet}.json"
                try:
                    history = reconstruct_wallet(cache, wallet, job_snapshot, eligibility_only=True)
                except CoverageExceeded:
                    # This is a recorded rejection, never a truncated profitable
                    # history. Move on instead of retrying this candidate forever.
                    history = {"wallet": wallet, "cutoff": job_snapshot["timestamp"],
                               "complete": False, "gaps": ["history_exceeds_10000_records_per_index"],
                               "records": [], "source": "Blockscout coverage limit; no performance conclusion"}
                atomic_json(cohort / f"history-{wallet}.json", history)
                report = analyse_wallet(history)
                atomic_json(report_path, report)
                queue.complete(wallet, job_snapshot, report)
        except ResearchPending as exc:
            pending.append(str(exc))
        except RpcError as exc:
            pending.append(f"HISTORY_RPC_UNAVAILABLE: {exc}")
        except (ValueError, KeyError, RuntimeError) as exc:
            pending.append(f"{type(exc).__name__}: historical evidence failed validation")
    reports = queue.reports() if queue else []
    reports = cluster_controllers(reports)
    reviews_path = runtime / "controller-reviews.json"
    reviews = json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    if not isinstance(reviews, dict):
        raise ValueError("Controller reviews must be an object")
    selected = apply_controller_reviews(reports, reviews, now=int(time.time()))
    selected = {w: record for w, record in selected.items()
                if next(r for r in reports if r['wallet'] == w)['history_snapshot']['hash'] in verified}
    for report in reports:
        report['qualified'] = report['wallet'] in selected
    # Publish a single coherent admission view before slow address exports.
    # This timestamp is the actual qualification check, never an export-only
    # refresh. Export completion remains separately reported below.
    checked_at = int(time.time())
    atomic_json(folder / 'registry-publication.json', {
        'schema': 'wallet_registry_v1', 'updated_at': checked_at,
        'reports': reports, 'qualified_wallets': selected})
    atomic_json(folder / "qualified-wallets.json", selected)
    ranked = sorted(reports, key=lambda r: (not r["performance_qualified"], Decimal(r["top_token_profit_share"]),
                    -min(Decimal(r["average_hold_minutes"]), Decimal(1440)), -r["closed_positions"],
                    -Decimal(r["net_pnl_after_follower_stress_usd"]), r["wallet"]))
    shortlist = [{"rank": i + 1, **r} for i, r in enumerate(ranked) if r["performance_qualified"]]
    complete = [r for r in reports if r.get('history_complete') is True]
    pipeline = {
        'assessed_wallets': len(reports), 'unassessed_wallets': len(set(candidates) - {r['wallet'] for r in reports}),
        'complete_histories': len(complete),
        'complete_with_closed_trades': sum(r['closed_positions'] > 0 for r in complete),
        'incomplete_or_unsupported': len(reports) - len(complete),
        'performance_qualified': len(shortlist), 'approved_wallets': len(selected),
        'controller_review_records': len(reviews),
        'controller_reviews_present': reviews_path.exists(),
        'complete_history_blockers': dict(Counter(b.split(':')[0] for r in complete for b in r['qualification_blockers'])),
        'history_cutoffs': dict(Counter(str(r['cutoff']) for r in reports)),
        'scope': 'Latest completed assessment per wallet; older cohorts retained. Incomplete histories have no performance conclusion.'}
    summary = {"mode": "RESEARCH_ONLY", "updated_at": checked_at,
               "rpc_hosts": {"discovery": urlsplit(discovery_rpc.url).hostname,
                             "history": urlsplit(rpc.url).hostname},
               "discovery": discovery, "candidate_count": len(candidates), "history_reports": len(reports),
               "imported_observed_buyers": imported['added_candidates'], "observed_evidence_import": imported,
               "complete_history_count": sum(r.get('history_complete') is True for r in reports),
               "unsupported_history_rejections": sum(r.get('assessment') == 'REJECTED_UNSUPPORTED_HISTORY' for r in reports),
               "performance_qualified_count": len(shortlist), "approved_wallet_count": len(selected),
               "pending": pending, "snapshot": snapshot, "shortlist": shortlist,
               "history_queue": queue.summary() if queue else None,
               "qualification_pipeline": pipeline,
               "history_progress": {"requests_this_cycle": cache.max_calls - cache.remaining,
                                    "last_operation": cache.progress,
                                    "checkpoint": getattr(cache, 'checkpoint_progress', {})},
               "controller_policy": "Automatic links merge suspected controllers. No observed link is not proof of independence; no automatic qualified:true assertion.",
               "reports": reports, "candidate_file": str(folder / "wallet-candidates.csv")}
    export_started = time.monotonic()
    phase('EXPORT')
    summary['address_evidence'] = evidence.export(folder, state, reports, selected, snapshot, int(time.time()))
    summary['export_seconds'] = round(time.monotonic() - export_started, 3)
    summary['finished_at'] = int(time.time())
    summary['duration_seconds'] = round(time.monotonic() - started, 3)
    phase('PUBLISH')
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


def export_evidence(runtime: Path):
    """Refresh local evidence/exports without reading keys or calling a provider."""
    folder = runtime / 'research'
    folder.mkdir(exist_ok=True)
    state_path = folder / 'discovery.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    summary_path = folder / 'wallet-research.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    snapshot = summary.get('snapshot')
    reports = summary.get('reports', [])
    selected_path = folder / 'qualified-wallets.json'
    selected = json.loads(selected_path.read_text()) if selected_path.exists() else {}
    with closing(CandidateEvidence(folder / 'candidate-evidence.sqlite3')) as evidence:
        imported = sync_observed_evidence(runtime, state, evidence)
        atomic_json(state_path, state)
        result = evidence.export(folder, state, reports, selected, snapshot, int(time.time()))
    summary.update(candidate_count=len(state.get('candidates', {})), address_evidence=result,
                   observed_evidence_import=imported, evidence_updated_at=int(time.time()))
    atomic_json(summary_path, summary)
    return {'mode':'RESEARCH_ONLY','network_calls':0,'observed_evidence_import':imported,'address_evidence':result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=60)
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument('--export-only', action='store_true', help='Refresh saved address evidence and exports without network calls')
    args = parser.parse_args()
    if not 10 <= args.calls <= 500 or not 15 <= args.seconds <= 300:
        parser.error("calls must be 10..500 and seconds 15..300")
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    try:
        with instance_lock(runtime / "research.lock"):
            result = export_evidence(runtime) if args.export_only else run_research(runtime, max_calls=args.calls, seconds=args.seconds)
    except Exception as exc:
        print(json.dumps({"mode": "RESEARCH_ONLY", "error": type(exc).__name__, "qualified": False}))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k not in ("reports", "shortlist")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
