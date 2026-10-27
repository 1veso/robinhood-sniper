"""Two-minute Robinhood launch monitor. --send enables Telegram; never places orders."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.adapters.evm import CHAIN_ID, EvmCollector, EvmRegistry, RpcClient
from sniper.adapters.evm_store import EvmStore
from sniper.alerts import fresh, qualify, reviewed_wallets
from sniper.policy import RiskPolicy
from sniper.telegram import AlertStore, TelegramClient
from sniper.adapters.goplus import GoPlusClient, combine_review
from sniper.models import SafetyEvidence
from supervise import atomic_json, instance_lock


def read_object(path, default=None):
    if not path.exists() and default is not None:
        return default
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def current_wallets(runtime):
    # Keep frozen paper-experiment membership untouched. New qualifications may
    # feed informational alerts after the research process records its evidence.
    path = runtime / "research/qualified-wallets.json"
    if path.exists():
        summary = read_object(runtime / "research/wallet-research.json", {})
        if not fresh(summary.get("updated_at"), int(time.time()), 300):
            return {}
        return reviewed_wallets(read_object(path))
    return reviewed_wallets(read_object(runtime / "wallets.json", {}))


def rpc_url(runtime):
    for name in ("observation-rpc-url.txt", "rpc-url.txt"):
        path = runtime / name
        if path.exists():
            return path.read_text(encoding="utf-8-sig").strip()
    return os.environ.get("SNIPER_OBSERVATION_RPC_URL") or "https://rpc.mainnet.chain.robinhood.com"


def cycle(runtime, *, send=False, rpc=None, collector_seconds=45, live_screening=False):
    folder = runtime / "telegram"
    folder.mkdir(parents=True, exist_ok=True)
    policy = RiskPolicy.from_file(ROOT / "config/paper.toml")
    marker = folder / "identity.json"
    previous = read_object(marker) if marker.exists() else None
    if previous and any(not (folder / name).is_file() for name in ("events.sqlite3", "alerts.sqlite3")):
        raise ValueError("Established alert journal is missing; restore it to preserve duplicate protection")
    with ExitStack() as resources:
        source = EvmStore(folder / "events.sqlite3")
        resources.callback(source.close)
        alerts = AlertStore(folder / "alerts.sqlite3")
        resources.callback(alerts.close)
        identity = {"events": source.identity, "alerts": alerts.identity}
        if previous is not None and previous != identity:
            raise ValueError("Alert journal identity changed; restore original state")
        if previous is None:
            atomic_json(marker, identity)
        wallets = current_wallets(runtime)
        rpc = rpc or RpcClient(rpc_url(runtime), timeout=6, max_retries=1)
        collector = EvmCollector(rpc, store=source, watchlist=wallets, max_blocks_per_request=500)
        status = {"mode": "ALERTS_ONLY", "orders_enabled": False, "started_at": int(time.time()),
                  "reviewed_wallets": len(wallets), "health": "OK", "alerts_queued": 0, "sent_this_cycle": 0}
        collected = None
        try:
            # A small initial window, then larger catch-up polls. The RPC time
            # budget still bounds work; larger chunks reduce repeated log and
            # boundary-header requests when clearing a restart backlog.
            maximum = 5000 if source.cursor(CHAIN_ID) else 2000
            collected = collector.poll(max_blocks=maximum, max_seconds=collector_seconds)
            status["collector"] = {k: collected.get(k) for k in ("head", "cursor", "status", "errors")}
            cursor = collected.get("cursor") or {}
            status["collector_lag_blocks"] = max(0, collected["head"] - cursor["block_number"]) if cursor else None
            if collected.get("status") == "budget_exhausted" or collected.get("errors"):
                status["health"] = "DEGRADED"
        except (ValueError, OSError, RuntimeError) as exc:
            status.update(health="DEGRADED", collector_error=f"{type(exc).__name__}: collection will retry saved cursor")
        now = int(time.time())
        pools = [p for p in source.pools(CHAIN_ID).values() if fresh(p.get("chain_timestamp"), now, 86400)]
        buys = source.events(CHAIN_ID, kind="verified_buy")
        bases = EvmRegistry().bases
        targets = {}
        for pool in pools:
            if (pool["token0"] in bases) != (pool["token1"] in bases):
                token = pool["token1"] if pool["token0"] in bases else pool["token0"]
                targets[token] = pool
        # Unchecked launches first, then oldest assessment. Every unresolved
        # launch remains in the journal and can be revisited on later cycles.
        queue = sorted(targets, key=lambda token: (alerts.assessment(token) or {}).get("observed_at", 0))
        try:
            with rpc.budget(15):
                for token in queue[:5]:
                    old = alerts.assessment(token)
                    if old and fresh(old.get("observed_at"), now, policy.max_evidence_age_seconds):
                        continue
                    facts = collector.contract_safety(token)
                    alerts.assess(token, facts, int(time.time()))
        except (ValueError, OSError, RuntimeError) as exc:
            status.update(health="DEGRADED", safety_error=f"{type(exc).__name__}: remaining launches retry next cycle")
        safety = read_object(runtime / "safety.json", {})
        if live_screening:
            observation_path = runtime / "safety-observations.json"
            observations = read_object(observation_path, {})
            screening_errors = []
            screened = 0
            # Two public API calls per cycle, with stale/unchecked tokens first.
            screen_queue = sorted(targets, key=lambda token: observations.get(
                f"eip155:{CHAIN_ID}/erc20:{token}", {}).get("observed_at", 0))
            for token in screen_queue[:2]:
                asset = f"eip155:{CHAIN_ID}/erc20:{token}"
                if fresh(observations.get(asset, {}).get("observed_at"), int(time.time()), 180):
                    continue
                try:
                    observations[asset] = GoPlusClient().screen(token).as_dict()
                    screened += 1
                except (ValueError, OSError, RuntimeError) as exc:
                    screening_errors.append({"asset_id": asset, "error": type(exc).__name__})
            atomic_json(observation_path, observations)
            status["safety_screening"] = {"updated": screened, "errors": screening_errors}

        def safety_for(asset):
            reviewed = read_object(runtime / "safety.json", {}).get(asset)
            if not live_screening:
                return reviewed
            raw = read_object(runtime / "safety-observations.json", {}).get(asset)
            if raw is None:
                return None
            try:
                return combine_review(SafetyEvidence.from_dict(raw), reviewed, now=int(time.time())).as_dict()
            except ValueError:
                return None
        reasons = {}
        # A failed/incomplete source read cannot authorize an outgoing alert.
        source_ok = collected is not None and collected.get("status") != "budget_exhausted" and not collected.get("errors")
        for pool in pools:
            token = pool["token1"] if pool["token0"] in bases else pool["token0"]
            asset = f"eip155:{CHAIN_ID}/erc20:{token}"
            signal, reason = qualify(pool, buys, wallets, safety_for(asset), alerts.assessment(token), policy, int(time.time()))
            reasons[reason] = reasons.get(reason, 0) + 1
            if source_ok and signal:
                status["alerts_queued"] += alerts.enqueue(token, signal, int(time.time()))
        status.update(tracked_recent_pools=len(pools), gate_counts=reasons)
        recipient_path, token_path = runtime / "telegram-recipient.json", runtime / "telegram-bot-token.txt"
        configured = recipient_path.exists() and token_path.exists()
        status["telegram"] = "READY" if configured else "SETUP_REQUIRED"
        if send and configured and source_ok:
            recipient = read_object(recipient_path)
            client = TelegramClient(token_path.read_text(encoding="utf-8-sig").strip())
            # Bind every send to the configured bot; a replaced key cannot send
            # through an unexpected bot or recipient.
            me = client.call("getMe")
            if not isinstance(me, dict) or me.get("id") != recipient.get("bot_id"):
                raise ValueError("Telegram bot differs from paired bot")
            alerts.bind(recipient["bot_id"], recipient["chat_id"])
            def valid(payload):
                current = int(time.time())
                if current > payload["expires_at"]:
                    return False
                if payload.get("kind") == "connection_test":
                    return True
                token = payload["token"]
                asset = f"eip155:{CHAIN_ID}/erc20:{token}"
                signal, _ = qualify(payload["pool"], [payload["buy"]], current_wallets(runtime),
                    safety_for(asset), alerts.assessment(token), policy, current)
                if signal is None:
                    return False
                for number, expected in payload["anchors"]:
                    if collector.block(number)["hash"] != expected:
                        return False
                return True
            with rpc.budget(15):
                status["sent_this_cycle"] = alerts.deliver(client, recipient["chat_id"], valid)
        status.update(outbox=alerts.counts(), finished_at=int(time.time()))
        atomic_json(folder / "status.json", status)
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--resolve-uncertain")
    parser.add_argument("--resolution", choices=["received", "suppress", "not-delivered"])
    args = parser.parse_args(argv)
    if args.interval < 120:
        parser.error("interval must be at least 120 seconds")
    runtime = ROOT / "runtime"
    folder = runtime / "telegram"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with instance_lock(folder / "monitor.lock"):
            if args.resolve_uncertain:
                if not args.resolution:
                    parser.error("--resolution is required")
                store = AlertStore(folder / "alerts.sqlite3")
                try:
                    store.resolve(args.resolve_uncertain, args.resolution)
                finally:
                    store.close()
                print(json.dumps({"resolved": args.resolution, "messages_sent": 0}))
                return 0
            while not (folder / "STOP").exists():
                started = time.monotonic()
                try:
                    status = cycle(runtime, send=args.send, live_screening=True)
                except Exception as exc:
                    status = {"mode": "ALERTS_ONLY", "health": "ERROR", "finished_at": int(time.time()),
                              "error": f"{type(exc).__name__}: check credentials, manifests and journals; retry next cycle"}
                    atomic_json(folder / "status.json", status)
                print(json.dumps(status, allow_nan=False), flush=True)
                if not args.watch:
                    return 0 if status.get("health") == "OK" else 2
                while time.monotonic() - started < args.interval:
                    if (folder / "STOP").exists():
                        return 0
                    time.sleep(1)
    except OSError:
        print(json.dumps({"status": "BUSY", "reason": "Monitor already running or lock unavailable"}))
        return 2
    return 0


if __name__ == "__main__":
    # pythonw.exe avoids a console window and a slow PowerShell startup in the
    # scheduled task. Cycle results remain durable in telegram/status.json.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    raise SystemExit(main())
