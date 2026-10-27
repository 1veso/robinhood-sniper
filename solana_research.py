"""Bounded free Solana profile screening; no account and no paid AI calls."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from sniper.adapters.solana import DexScreenerClient, RugcheckClient
from sniper.models import json_safe
from sniper.screening import ScreenRules, screen_solana
from supervise import atomic_json, instance_lock


def scan(runtime, *, market=None, rugcheck=None, clock=time.time, deadline_clock=time.monotonic):
    folder = runtime / "solana"
    folder.mkdir(parents=True, exist_ok=True)
    market, rugcheck = market or DexScreenerClient(timeout=8), rugcheck or RugcheckClient(timeout=8)
    rules = ScreenRules.from_file(ROOT / "config/solana-screen.toml")
    started, deadline = int(clock()), deadline_clock() + 35
    db = sqlite3.connect(folder / "screens.sqlite3", timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS runs (started_at INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS screens (token TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, result TEXT NOT NULL)")
        last = db.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
        if last is not None and started - last < 1800:
            return {"mode": "RESEARCH_ONLY", "status": "NOT_DUE", "last_scan_at": last, "next_scan_at": last + 1800}
        db.execute("INSERT INTO runs VALUES (?)", (started,))
        db.commit()
        results, error = [], None
        try:
            # Profiles are leads, not verified launches or evidence of quality.
            tokens = market.profiles(limit=25)
            for token in tokens:
                if len(results) >= 5 or deadline_clock() >= deadline:
                    break
                if db.execute("SELECT 1 FROM screens WHERE token=?", (token,)).fetchone():
                    continue
                interrupted = {"mode": "RESEARCH_ONLY", "token": token, "verdict": "SKIP",
                               "entry_eligible": False, "reason": "screen_interrupted; manual review required"}
                # Durable claim before network: preserve the article's seen-once
                # policy even after a crash, rather than retrying until a pass.
                db.execute("INSERT INTO screens VALUES (?,?,?)", (token, started, json.dumps(interrupted)))
                db.commit()
                result = screen_solana(token, rules=rules, market=market, rugcheck=rugcheck, clock=clock)
                db.execute("UPDATE screens SET result=? WHERE token=?", (json.dumps(json_safe(result), allow_nan=False), token))
                db.commit()
                results.append(result)
        except (ValueError, RuntimeError, OSError) as exc:
            error = f"profile_source_unavailable:{type(exc).__name__}"
        count = db.execute("SELECT COUNT(*) FROM screens").fetchone()[0]
        summary = {"mode": "RESEARCH_ONLY", "status": "DEGRADED" if error else "OK",
                   "updated_at": int(clock()), "scan_started_at": started, "next_scan_at": started + 1800,
                   "screened_this_run": len(results), "total_screened": count, "source_error": error,
                   "deterministic_passes_this_run": sum(r.get("deterministic_checks_passed", False) for r in results),
                   "paid_ai_enabled": False, "entry_eligible": False, "seen_policy": "once per mint, including skips",
                   "results": results}
        atomic_json(folder / "latest.json", json_safe(summary))
        return {k: v for k, v in summary.items() if k != "results"}
    finally:
        db.close()


def main():
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    try:
        with instance_lock(runtime / "solana-research.lock"):
            result = scan(runtime)
    except Exception as exc:
        print(json.dumps({"mode": "RESEARCH_ONLY", "status": "ERROR", "error": type(exc).__name__}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
