"""Operator commands. All market access is read-only; all execution is PAPER."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
from urllib.parse import urlsplit

from . import __version__
from .models import Candidate, MarketQuote, SafetyEvidence, json_safe, loads_strict
from .policy import RiskPolicy


def output(value) -> None:
    print(json.dumps(json_safe(value), indent=2, allow_nan=False))


def read_json(path: str | Path):
    path = Path(path)
    if path.stat().st_size > 4_000_000:
        raise ValueError("Input exceeds 4 MB limit")
    return loads_strict(path.read_text(encoding="utf-8"))


def policy_for(args) -> RiskPolicy:
    return RiskPolicy.from_file(args.config) if args.config else RiskPolicy()


def open_engine(args):
    from .ledger import Ledger
    from .engine import PaperEngine
    policy = policy_for(args)
    ledger = Ledger(args.db, policy.starting_cash_usd)
    try:
        engine = PaperEngine(ledger, policy)
    except Exception:
        ledger.close()
        raise
    return policy, ledger, engine


def validate_envelope(value, *, replay: bool = False):
    if not isinstance(value, dict) or value.get("kind") not in ("candidate", "marks"):
        raise ValueError("Envelope kind must be candidate or marks")
    now = value.get("at") if replay else int(time.time())
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("Replay envelope needs integer UTC epoch seconds in at")
    if value["kind"] == "candidate":
        if set(value) - {"kind", "at", "candidate", "quote"}:
            raise ValueError("Unknown candidate envelope fields")
        candidate = Candidate.from_dict(value["candidate"])
        quote = MarketQuote.from_dict(value["quote"])
        return now, candidate, quote
    if set(value) - {"kind", "at", "quotes"}:
        raise ValueError("Unknown marks envelope fields")
    quotes = value.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("marks.quotes must be a list (empty means unavailable)")
    mapping = {}
    for item in quotes:
        quote = MarketQuote.from_dict(item)
        if quote.asset_id in mapping:
            raise ValueError("Duplicate asset quote")
        mapping[quote.asset_id] = quote
    return now, None, mapping


def command_demo(args):
    from .demo import run_demo
    from .engine import PaperEngine
    from .ledger import Ledger
    from .reporting import write_html
    policy = policy_for(args)
    # Always isolated: a demonstration can never contaminate the operator's account.
    ledger = Ledger(":memory:", policy.starting_cash_usd)
    try:
        result = run_demo(PaperEngine(ledger, policy), policy)
        if args.csv:
            result["csv_path"] = str(ledger.export_csv(args.csv))
        if args.html:
            result["html_path"] = str(write_html(result["report"], args.html, data_mode="SYNTHETIC_DEMO"))
        output(result)
    finally:
        ledger.close()


def command_paper(args):
    policy, ledger, engine = open_engine(args)
    try:
        value = read_json(args.input)
        now, candidate, quote = validate_envelope(value)
        result = engine.consider(candidate, quote, now=now) if candidate else engine.watch(quote, now=now)
        output({"mode": "PAPER", "policy_version": policy.version, "result": result, "report": engine.report(now=now)})
    finally:
        ledger.close()


def command_replay(args):
    # Validate entire input before mutating the account; no retrospective time shuffling.
    path = Path(args.input)
    if path.stat().st_size > 16_000_000:
        raise ValueError("Replay exceeds 16 MB limit")
    records = [validate_envelope(loads_strict(line), replay=True)
               for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records or any(records[i][0] < records[i-1][0] for i in range(1, len(records))):
        raise ValueError("Replay must contain chronologically ordered events")
    policy, ledger, engine = open_engine(args)
    try:
        events = []
        for now, candidate, quote in records:
            events.append(engine.consider(candidate, quote, now=now) if candidate else engine.watch(quote, now=now))
        report = engine.report(now=records[-1][0])
        if args.csv:
            ledger.export_csv(args.csv)
        output({"mode": "PAPER_REPLAY", "notice": "Input provenance and executable assumptions require independent validation.",
                "policy_version": policy.version, "events": events, "report": report})
    finally:
        ledger.close()


def command_report(args):
    from .reporting import write_html
    policy, ledger, engine = open_engine(args)
    try:
        report = engine.report(now=int(time.time()))
        report["policy_version"] = policy.version
        if args.csv:
            report["csv_path"] = str(ledger.export_csv(args.csv))
        if args.html:
            report["html_path"] = str(write_html(report, args.html))
        output(report)
    finally:
        ledger.close()


def command_halt(args):
    policy, ledger, engine = open_engine(args)
    try:
        if args.command == "halt":
            ledger.set_halt(args.reason)
        else:
            ledger.clear_halt()
        output({"mode": "PAPER", "entry_halt": ledger.snapshot()["entry_halt"],
                "exits": "continue to be processed by watch events", "daily_loss_halt": "not overridden"})
    finally:
        ledger.close()


def command_screen(args):
    from .adapters.solana import RugcheckClient, DexScreenerClient
    now = int(time.time())
    result = {"mode": "READ_ONLY", "chain": args.chain, "token": args.token}
    evidence = None
    if args.chain == "solana":
        evidence = RugcheckClient().report(args.token, now=now)
        result["safety"] = evidence.as_dict()
    quote = DexScreenerClient(chain_id=args.chain).quote(args.token, now=int(time.time()), pool_id=args.pool)
    result["quote"] = quote.as_dict()
    result["entry_eligible"] = False
    result["reason"] = "Indicative market screen; executable route and mandatory safety evidence are not established"
    output(result)


def command_collect(args):
    from .adapters.evm import RpcClient, EvmCollector
    from .adapters.evm_store import EvmStore
    watchlist = read_json(args.wallets) if args.wallets else {}
    if not isinstance(watchlist, dict):
        raise ValueError("Wallet file must be an address-to-record object")
    quote_usd = read_json(args.base_prices) if args.base_prices else {}
    store = EvmStore(args.events_db)
    try:
        collector = EvmCollector(RpcClient(args.rpc), store=store, watchlist=watchlist,
                                 quote_usd=quote_usd, confirmations=args.confirmations)
        iterations = 0
        while True:
            if args.base_prices:
                collector.quote_usd = read_json(args.base_prices)
            result = collector.poll(start_block=args.start_block if iterations == 0 else None,
                                    max_blocks=args.max_blocks)
            output({"mode": "READ_ONLY", "observed_at": int(time.time()), "result": result})
            iterations += 1
            if not args.follow or (args.iterations is not None and iterations >= args.iterations):
                break
            time.sleep(args.interval)
    finally:
        store.close()


def command_run(args):
    """Read-only source collection, evidence checks, and PAPER processing loop."""
    from .adapters.evm import RpcClient, EvmCollector, USDG, WETH
    from .adapters.evm_store import EvmStore
    from .adapters.solana import DexScreenerClient
    from .pipeline import PaperPipeline
    watchlist = read_json(args.wallets) if args.wallets else {}
    if not isinstance(watchlist, dict):
        raise ValueError("Wallet file must be an address-to-record object")

    def manifest_record(path, asset_id):
        values = read_json(path)
        if not isinstance(values, dict):
            raise ValueError("Evidence manifest must map canonical asset IDs to records")
        return values.get(asset_id)

    with ExitStack() as resources:
        policy, ledger, engine = open_engine(args)
        resources.callback(ledger.close)
        store = EvmStore(args.events_db)
        resources.callback(store.close)
        market = DexScreenerClient(chain_id="robinhoodchain")
        collector = EvmCollector(RpcClient(args.rpc), store=store, watchlist=watchlist,
                                 confirmations=args.confirmations)
        # Selection belongs to an experiment, including across process restarts.
        selection = json.dumps(watchlist, sort_keys=True, default=str, allow_nan=False)
        with ledger.transaction():
            previous_store = ledger._get("runner_event_store_id")
            if previous_store is not None and previous_store != store.identity:
                raise ValueError("Event journal differs from this account's pinned source; restore the original journal or use a new paper database")
            ledger._set("runner_event_store_id", store.identity)
            previous = ledger._get("runner_wallet_selection")
            if previous is not None and previous != selection:
                raise ValueError("Wallet selection differs from this account's frozen experiment; use a new paper database")
            ledger._set("runner_wallet_selection", selection)

        def token_from_asset(asset_id):
            if not asset_id.startswith("eip155:4663/erc20:"):
                raise ValueError("This runner supports Robinhood Chain assets only")
            return asset_id.split(":")[-1]

        def safety_provider(asset_id, now):
            if args.safety:
                record = manifest_record(args.safety, asset_id)
                if record is not None:
                    return SafetyEvidence.from_dict(record)
            return SafetyEvidence(asset_id, now, {name: None for name in policy.required_safety_checks},
                                  "unverified:independent-contract-and-exit-evidence-required")

        def quote_provider(asset_id, pool_id, now):
            if args.quotes:
                record = manifest_record(args.quotes, asset_id)
                if record is not None:
                    return MarketQuote.from_dict(record)
            token = token_from_asset(asset_id)
            indicative = market.quote(token, now=int(time.time()), pool_id=pool_id)
            return replace(indicative, asset_id=asset_id)

        pipeline = PaperPipeline(engine, policy, safety_provider, quote_provider)
        iterations = 0
        while True:
            cycle = {"mode": "PAPER", "observed_at": int(time.time()), "source_errors": []}
            entry_inputs_ok = True
            if args.wallets:
                try:
                    updated = read_json(args.wallets)
                    if updated != watchlist:
                        raise ValueError("Wallet selection changed during a frozen run; use a new experiment")
                except (ValueError, OSError, RuntimeError) as exc:
                    entry_inputs_ok = False
                    ledger.set_halt("wallet_selection_unavailable_or_changed")
                    cycle["source_errors"].append({"stage": "wallet_selection", "reason": str(exc)[:200]})
            if args.base_prices:
                try:
                    collector.quote_usd = read_json(args.base_prices)
                    if not isinstance(collector.quote_usd, dict):
                        raise ValueError("Base prices must be a mapping")
                except (ValueError, OSError, RuntimeError) as exc:
                    collector.quote_usd = {}
                    cycle["source_errors"].append({"stage": "base_valuation", "reason": str(exc)[:200]})
            else:
                collector.quote_usd = {}
                for base in (USDG, WETH):
                    try:
                        quote = market.quote(base, now=int(time.time()))
                        collector.quote_usd[base] = {"price_usd": str(quote.price_usd), "observed_at": quote.observed_at}
                    except (ValueError, OSError, RuntimeError) as exc:
                        cycle["source_errors"].append({"stage": "base_valuation", "asset": base, "reason": str(exc)[:200]})
            collected = None
            try:
                collected = collector.poll(start_block=args.start_block if iterations == 0 else None,
                                           max_blocks=args.max_blocks)
                cycle["collector"] = {k: collected.get(k) for k in ("cursor", "head", "tracked_wallets", "reorg", "errors")}
                cycle["counts"] = {k: len(collected.get(k, [])) for k in ("new_pools", "swaps", "verified_buys")}
            except (ValueError, OSError, RuntimeError) as exc:
                cycle["source_errors"].append({"stage": "collector", "reason": str(exc)[:300]})
                collected = None
            # The collector and the account use separate journals. Replay saved
            # rewinds even after a crash between source commit and signal ingest.
            for reorg in store.reorgs(4663):
                reorg_id = f"collector-reorg:{reorg['id']}"
                with ledger.transaction():
                    previous = ledger.conn.execute("SELECT payload FROM events WHERE event_id=?", (reorg_id,)).fetchone()
                    if previous is None:
                        pipeline.invalidate_after(reorg["rewind_to"], now=int(time.time()))
                        ledger.record_event(reorg_id, "collector_reorg", reorg, int(time.time()))
                    elif json.loads(previous["payload"]) != reorg:
                        raise ValueError("Collector journal changed; use a separate experiment database")
            # Exit processing remains active on an entry-source failure.
            marks = {}
            for position in ledger.positions():
                try:
                    quote = quote_provider(position["asset_id"], position["pool_id"], int(time.time()))
                    marks[position["asset_id"]] = quote
                except (ValueError, OSError, RuntimeError) as exc:
                    cycle["source_errors"].append({"stage": "exit_quote", "asset_id": position["asset_id"], "reason": str(exc)[:200]})
            cycle["exits"] = engine.watch(marks, now=int(time.time()))
            cycle["decisions"] = []
            if collected is not None and entry_inputs_ok:
                # Recover a source commit that preceded an interrupted ingest.
                # Only canonical, still-fresh chain observations may form a signal.
                now = int(time.time())
                recent = [buy for buy in store.events(4663, kind="verified_buy", canonical=True)
                          if isinstance(buy.get("chain_timestamp"), int)
                          and 0 <= now - buy["chain_timestamp"] <= policy.max_confirmation_age_seconds]
                cycle["decisions"] = pipeline.ingest(recent, now=now)
            cycle["account"] = engine.report(now=int(time.time()))
            cycle["entry_evidence"] = "External verified safety and executable-route observations required; indicative screens cannot authorize a paper entry"
            output(cycle)
            iterations += 1
            if not args.follow or (args.iterations is not None and iterations >= args.iterations):
                break
            time.sleep(args.interval)


def command_rank(args):
    from .ranking import rank_traders
    output(rank_traders(read_json(args.input), decision_at=int(time.time())))


def command_advise(args):
    from .advisor import GrokAdvisor
    request = read_json(args.input)
    if not isinstance(request, dict) or set(request) != {"request_id", "asset_id", "evidence"}:
        raise ValueError("Advisory request requires request_id, asset_id, evidence")
    policy = policy_for(args)
    advisor = GrokAdvisor(enabled=args.enable_paid_ai, budget_path=args.budget_db,
                          daily_limit_usd=policy.max_api_cost_usd_day, model=args.model)
    output(advisor.assess(**request, now=int(time.time())))


def command_doctor(args):
    from .adapters.evm import RpcClient, EvmRegistry
    policy = policy_for(args)
    result = {"mode": "PAPER_ONLY", "version": __version__, "python": sys.version.split()[0],
              "runtime_dependencies": "Python standard library", "policy_version": policy.version,
              "signing_capability": False, "live_order_capability": False,
              "ai_configured": bool(os.environ.get("XAI_API_KEY")), "ai_enabled_by_default": False,
              "policy": policy.as_dict()}
    if args.network:
        rpc = RpcClient(args.rpc)
        chain = rpc.call("eth_chainId", [])
        chain_id = int(chain, 16)
        if chain_id != 4663:
            raise ValueError(f"Wrong chain: {chain_id}, expected 4663")
        result["network"] = {"chain_id": chain_id, "rpc_host": urlsplit(args.rpc).hostname, "verified": True}
    output(result)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SNIPER · auditable market research and PAPER trading")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="Risk policy TOML; defaults match config/paper.toml")
    p.add_argument("--db", default="runtime/paper.sqlite3", help="Persistent PAPER ledger")
    sub = p.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run isolated synthetic end-to-end example, no keys/network")
    demo.add_argument("--html", help="Write standalone HTML report")
    demo.add_argument("--csv", help="Write fill ledger CSV")
    demo.set_defaults(func=command_demo)
    paper = sub.add_parser("paper-check", help="Process a fresh candidate/marks JSON envelope")
    paper.add_argument("--input", required=True)
    paper.set_defaults(func=command_paper)
    replay = sub.add_parser("replay", help="Replay chronological JSONL observations into a PAPER ledger")
    replay.add_argument("--input", required=True)
    replay.add_argument("--csv")
    replay.set_defaults(func=command_replay)
    report = sub.add_parser("report", help="Account status, whole-position statistics and audit exports")
    report.add_argument("--html")
    report.add_argument("--csv")
    report.set_defaults(func=command_report)
    halt = sub.add_parser("halt", help="Persistently halt entries; exit processing remains available")
    halt.add_argument("--reason", required=True)
    halt.set_defaults(func=command_halt)
    resume = sub.add_parser("resume", help="Clear manual entry halt (does not override daily loss halt)")
    resume.set_defaults(func=command_halt)
    screen = sub.add_parser("screen", help="Read token screening and indicative market evidence")
    screen.add_argument("token")
    screen.add_argument("--chain", choices=["solana", "robinhoodchain"], default="solana")
    screen.add_argument("--pool", help="Pin the exact pool; never silently switch")
    screen.set_defaults(func=command_screen)
    for command, help_text, handler in (
        ("collect", "Collect canonical Robinhood events; no order execution", command_collect),
        ("run", "Run collection, verified signal checks, and PAPER position monitoring", command_run),
    ):
        collect = sub.add_parser(command, help=help_text)
        collect.add_argument("--rpc", default=os.environ.get("SNIPER_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com")
        collect.add_argument("--events-db", default="runtime/events.sqlite3")
        collect.add_argument("--wallets", help="Verified wallet/controller provenance JSON")
        collect.add_argument("--base-prices", help="Explicit current base-token/USD valuation mapping")
        collect.add_argument("--start-block", type=int)
        collect.add_argument("--max-blocks", type=int, default=100)
        collect.add_argument("--confirmations", type=int, default=5, help="Block confirmation buffer, distinct from wallet quorum")
        collect.add_argument("--follow", action="store_true")
        collect.add_argument("--iterations", type=int, help="Bounded number of polls with --follow")
        collect.add_argument("--interval", type=int, default=15)
        if command == "run":
            collect.add_argument("--safety", help="Timestamped external safety evidence by canonical asset ID")
            collect.add_argument("--quotes", help="Timestamped external route observations by canonical asset ID")
        collect.set_defaults(func=handler)
    rank = sub.add_parser("rank", help="Deterministically rank supplied verified wallet statistics")
    rank.add_argument("--input", required=True)
    rank.set_defaults(func=command_rank)
    advise = sub.add_parser("advise", help="Optional evidence annotation; disabled unless explicitly enabled")
    advise.add_argument("--input", required=True)
    advise.add_argument("--enable-paid-ai", action="store_true")
    advise.add_argument("--budget-db", default="runtime/advisory.sqlite3")
    advise.add_argument("--model", default="grok-4.7")
    advise.set_defaults(func=command_advise)
    doctor = sub.add_parser("doctor", help="Validate configuration; optionally check the read-only RPC")
    doctor.add_argument("--network", action="store_true")
    doctor.add_argument("--rpc", default=os.environ.get("SNIPER_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com")
    doctor.set_defaults(func=command_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        for name in ("interval", "max_blocks", "confirmations", "iterations"):
            value = getattr(args, name, None)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        args.func(args)
        return 0
    except KeyboardInterrupt:
        print("Stopped. Persistent state is retained.", file=sys.stderr)
        return 130
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        message = str(exc)
        key = os.environ.get("XAI_API_KEY")
        if key:
            message = message.replace(key, "[REDACTED]")
        output({"error": message[:1000], "mode": "PAPER_ONLY", "funds_moved": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
