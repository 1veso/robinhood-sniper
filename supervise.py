"""Restartable, single-instance PAPER observation service; standard library only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.cli import read_json
from sniper.policy import RiskPolicy
from sniper.reporting import write_html
from sniper.ledger import Ledger
from sniper.engine import PaperEngine


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def instance_lock(path: Path):
    """OS lock is released on process exit, including crashes."""
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def child_environment(runtime: Path) -> dict:
    env = dict(os.environ)
    for filename, variable in (("rpc-url.txt", "SNIPER_RPC_URL"),
                               ("observation-rpc-url.txt", "SNIPER_OBSERVATION_RPC_URL")):
        private_rpc = runtime / filename
        if private_rpc.exists():
            env[variable] = private_rpc.read_text(encoding="utf-8-sig").strip()
            if not env[variable]:
                raise ValueError(f"runtime/{filename} is empty; remove it to use the default endpoint")
    history_key = runtime / "blockscout-api-key.txt"
    if history_key.exists():
        env["BLOCKSCOUT_API_KEY"] = history_key.read_text(encoding="utf-8-sig").strip()
    env["PYTHONUTF8"] = "1"
    return env


def redact(value: str, env: dict) -> str:
    for name in ("SNIPER_RPC_URL", "SNIPER_OBSERVATION_RPC_URL", "XAI_API_KEY", "BLOCKSCOUT_API_KEY"):
        if env.get(name):
            value = value.replace(env[name], "[REDACTED]")
    return value


def guard_account(runtime: Path, account: str, policy: RiskPolicy) -> None:
    """An established service must never interpret lost state as fresh capital."""
    database = runtime / f"{account}.sqlite3"
    marker = runtime / f"{account}.identity.json"
    identity = read_json(marker) if marker.exists() else None
    previous = read_json(runtime / "status.json") if (runtime / "status.json").exists() else {}
    established = identity is not None or previous.get("account") == account
    if established and not database.is_file():
        raise ValueError("Established account database is missing; restore it before restarting")
    ledger = Ledger(database, policy.starting_cash_usd, require_existing=database.exists() or established)
    try:
        PaperEngine(ledger, policy)
        account_id = ledger._get("account_id")
        if identity is not None and identity != {"account": account, "account_id": account_id}:
            raise ValueError("Account identity changed; restore the original database and identity record")
        if identity is None:
            atomic_json(marker, {"account": account, "account_id": account_id})
    finally:
        ledger.close()


def run_cycle(runtime: Path, account: str, max_blocks: int, timeout: int) -> dict:
    policy_path = ROOT / "config" / "paper.toml"
    policy = RiskPolicy.from_file(policy_path)
    guard_account(runtime, account, policy)
    paths = {name: runtime / f"{name}.json" for name in ("wallets", "safety", "quotes")}
    # Empty manifests preserve unknown evidence. Never manufacture passes.
    for path in paths.values():
        if not path.exists():
            atomic_json(path, {})
    env = child_environment(runtime)
    observation_env = dict(env)
    if env.get("SNIPER_OBSERVATION_RPC_URL"):
        observation_env["SNIPER_RPC_URL"] = env["SNIPER_OBSERVATION_RPC_URL"]
    command = [sys.executable, str(ROOT / "run.py"), "--config", str(policy_path),
               "--db", str(runtime / f"{account}.sqlite3"), "--existing-account", "run",
               "--events-db", str(runtime / f"{account}-events.sqlite3"),
               "--max-blocks", str(max_blocks)]
    for name, path in paths.items():
        command.extend([f"--{name}", str(path)])
    started = int(time.time())
    try:
        result = subprocess.run(command, cwd=ROOT, env=observation_env, capture_output=True,
                                text=True, encoding="utf-8", timeout=timeout)
        safe_output = redact(result.stdout, env)
        try:
            cycle = json.loads(safe_output)
            if not isinstance(cycle, dict):
                raise ValueError("cycle is not an object")
        except ValueError:
            cycle = {"error": "Runner did not return valid JSON",
                     "detail": redact(result.stderr, env)[-1000:]}
        return_code = result.returncode
    except subprocess.TimeoutExpired:
        cycle = {"error": "Runner timed out; committed state is retained and the next cycle retries"}
        return_code = 124
    blockers = []
    wallets = read_json(paths["wallets"])
    if not isinstance(wallets, dict):
        raise ValueError("Wallet manifest must be an object")
    clusters = {item["cluster_id"] for item in wallets.values()
                if isinstance(item, dict) and item.get("qualified") is True
                and isinstance(item.get("cluster_id"), str) and item["cluster_id"].strip()
                and item.get("qualification_evidence")}
    if len(clusters) < policy.min_independent_wallets:
        blockers.append(f"Reviewed wallet clusters: {len(clusters)}/{policy.min_independent_wallets}")
    for name in ("safety", "quotes"):
        if not read_json(paths[name]):
            blockers.append(f"No external {name} evidence; entries remain blocked")
    errors = cycle.get("source_errors", [])
    if return_code or cycle.get("error"):
        health = "ERROR"
    elif errors:
        health = "DEGRADED"
    else:
        health = "OK"
    collector = cycle.get("collector") or {}
    cursor = collector.get("cursor") or {}
    head = collector.get("head")
    lag = max(0, head - cursor["block_number"]) if head is not None and cursor.get("block_number") is not None else None
    if collector.get("status") == "budget_exhausted":
        if health == "OK":
            health = "DEGRADED"
        blockers.append("Collector time budget reached; saved progress resumes next cycle")
    if lag is not None and lag > max_blocks:
        health = "DEGRADED"
        blockers.append(f"Collector catching up: {lag} blocks behind head")
    status = {"mode": "PAPER_ONLY", "health": health,
              "updated_at_utc": datetime.now(timezone.utc).isoformat(),
              "started_at": started, "finished_at": int(time.time()),
              "account": account, "return_code": return_code,
              "wallet_candidates": len(wallets), "reviewed_clusters": len(clusters),
              "entry_prerequisite_gaps": blockers,
              "entry_status": "BLOCKED" if blockers or health != "OK" else "ENGINE_CHECKS_REQUIRED",
              "collector_lag_blocks": lag, "cycle": cycle}
    if return_code == 0:
        try:
            research = subprocess.run([sys.executable, str(ROOT / "research.py")],
                                      cwd=ROOT, env=env, capture_output=True,
                                      text=True, encoding="utf-8", timeout=75)
            status["wallet_research"] = json.loads(redact(research.stdout, env))
        except (subprocess.TimeoutExpired, ValueError):
            status["wallet_research"] = {"status": "PENDING", "reason": "Research will retry next cycle"}
        try:
            solana = subprocess.run([sys.executable, str(ROOT / "solana_research.py")],
                                    cwd=ROOT, env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=55)
            status["solana_research"] = json.loads(redact(solana.stdout, env))
        except (subprocess.TimeoutExpired, ValueError):
            status["solana_research"] = {"status": "PENDING", "reason": "Screening status unavailable; see runtime/solana evidence journal"}
    status["finished_at"] = int(time.time())
    status["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_json(runtime / "status.json", status)
    log = runtime / "service.jsonl"
    if log.exists() and log.stat().st_size > 5_000_000:
        os.replace(log, runtime / "service.previous.jsonl")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, allow_nan=False) + "\n")
    temporary = runtime / "account.tmp.html"
    write_html({"snapshot": cycle.get("account", {}), "service": status}, temporary)
    os.replace(temporary, runtime / "account.html")
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--account", default="observation-001")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-blocks", type=int, default=3000)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", args.account):
        parser.error("account must contain only letters, digits, underscore and hyphen")
    if args.interval < 30 or not 1 <= args.max_blocks <= 5000 or args.timeout < 30:
        parser.error("interval >= 30, max-blocks 1..5000 and timeout >= 30 are required")
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    try:
        lock = instance_lock(runtime / "service.lock")
        lock.__enter__()
    except OSError:
        print("Another SNIPER service cycle is already active.")
        return 0
    try:
        while not (runtime / "STOP").exists():
            started = time.monotonic()
            try:
                status = run_cycle(runtime, args.account, args.max_blocks, args.timeout)
            except Exception as exc:
                # No arbitrary exception text: provider URLs can carry secrets.
                status = {"mode": "PAPER_ONLY", "health": "ERROR", "entry_status": "BLOCKED",
                          "account": args.account,
                          "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                          "error": f"{type(exc).__name__}: check manifests and local configuration"}
                atomic_json(runtime / "status.json", status)
            print(json.dumps({k: v for k, v in status.items() if k != "cycle"}), flush=True)
            if args.once:
                return 0 if status["health"] == "OK" else 2
            while time.monotonic() - started < args.interval:
                if (runtime / "STOP").exists():
                    return 0
                time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        lock.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
