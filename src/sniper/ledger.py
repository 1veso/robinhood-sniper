"""Transactional, append-audited bookkeeping for PAPER trades only.

Amounts are stored as decimal strings. No exchange or wallet operations exist here.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

D = Decimal
ZERO = D("0")
BPS = D("10000")


def decimal(value: Any, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a monetary amount")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid monetary amount") from exc
    if not result.is_finite() or (nonnegative and result < ZERO):
        raise ValueError("Monetary amount must be finite" + (" and nonnegative" if nonnegative else ""))
    return result


def json_text(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Nonfinite decimal")
            return str(item)
        if is_dataclass(item):
            return asdict(item)
        raise TypeError(f"Unsupported audit value: {type(item).__name__}")
    return json.dumps(value, default=default, allow_nan=False, sort_keys=True, separators=(",", ":"))


def timestamp(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Timestamp must be a nonnegative integer in UTC seconds")
    return value


def impact_fraction(notional: Decimal, liquidity: Decimal) -> Decimal:
    """Explicit simulation proxy; NOT a size-specific executable V3 quote."""
    if liquidity <= ZERO:
        return D("1")
    return D("2") * notional / liquidity


def liquidation(position: dict[str, Any]) -> Decimal:
    """Conservative modeled proceeds, including sale friction and one gas charge."""
    if position.get("mark_status") != "ok":
        return ZERO
    notional = position["qty"] * position["mark_price"]
    fraction = position["mark_sell_bps"] / BPS + impact_fraction(notional, position["mark_liquidity"])
    return max(ZERO, notional * (D("1") - fraction) - position["mark_gas"])


POSITION_DECIMALS = {
    "entry_price", "reference_entry_price", "qty0", "qty", "total_entry_cost", "remaining_basis",
    "realized_pnl", "net_exit_proceeds", "total_costs", "liq0", "peak_price", "mark_price",
    "mark_liquidity", "mark_sell_bps", "mark_gas", "reserved_exit_cost",
}


class Ledger:
    """One durable paper account. Restarting never resets its starting capital.

    Each engine decision is protected by BEGIN IMMEDIATE. Independent processes
    opening the same database serialize their writes through SQLite.
    """

    def __init__(self, path: str | Path, starting_cash: Decimal):
        starting_cash = decimal(starting_cash, nonnegative=True)
        if starting_cash <= ZERO:
            raise ValueError("Starting paper cash must be positive")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, observed_at INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                event_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, observed_at INTEGER NOT NULL,
                verdict TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
                strategy TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, opened_at INTEGER NOT NULL,
                pool_id TEXT NOT NULL, entry_price TEXT NOT NULL, reference_entry_price TEXT NOT NULL,
                qty0 TEXT NOT NULL, qty TEXT NOT NULL, total_entry_cost TEXT NOT NULL,
                remaining_basis TEXT NOT NULL, realized_pnl TEXT NOT NULL, net_exit_proceeds TEXT NOT NULL,
                total_costs TEXT NOT NULL, liq0 TEXT NOT NULL, peak_price TEXT NOT NULL,
                mark_price TEXT NOT NULL, mark_liquidity TEXT NOT NULL, mark_sell_bps TEXT NOT NULL,
                mark_gas TEXT NOT NULL, mark_at INTEGER NOT NULL, mark_status TEXT NOT NULL,
                reserved_exit_cost TEXT NOT NULL, tp_done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
                position_id TEXT NOT NULL, event_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                symbol TEXT NOT NULL, strategy TEXT NOT NULL, action TEXT NOT NULL,
                observed_at INTEGER NOT NULL, quantity TEXT NOT NULL, reference_price TEXT NOT NULL,
                fill_price TEXT NOT NULL, gross_usd TEXT NOT NULL, friction_usd TEXT NOT NULL,
                gas_usd TEXT NOT NULL, cash_delta TEXT NOT NULL, pnl TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS closed_positions (
                position_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, symbol TEXT NOT NULL,
                strategy TEXT NOT NULL, opened_at INTEGER NOT NULL, closed_at INTEGER NOT NULL,
                entry_cost TEXT NOT NULL, net_exit_proceeds TEXT NOT NULL, realized_pnl TEXT NOT NULL,
                total_costs TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS closed_asset_time ON closed_positions(asset_id, closed_at);
            CREATE TABLE IF NOT EXISTS daily_equity (
                day TEXT PRIMARY KEY, opening_equity TEXT NOT NULL, observed_at INTEGER NOT NULL,
                halted_at INTEGER, halt_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at INTEGER NOT NULL,
                equity TEXT NOT NULL, cash TEXT NOT NULL, reason TEXT NOT NULL
            );
        """)
        with self.transaction():
            version = self._get("schema_version")
            if version is not None and version != "1":
                raise ValueError("Unsupported ledger schema version")
            stored = self._get("starting_cash")
            if stored is not None and decimal(stored) != starting_cash:
                raise ValueError("Existing ledger starting cash differs; use a different database for a new experiment")
            if stored is None:
                for key, value in {
                    "schema_version": "1", "execution_mode": "paper", "starting_cash": str(starting_cash),
                    "cash": str(starting_cash), "realized_pnl": "0", "fees_paid": "0",
                    "entry_halt": "", "max_equity": str(starting_cash), "max_drawdown_pct": "0",
                }.items():
                    self._set(key, value)
            if self._get("execution_mode") != "paper":
                raise ValueError("Ledger is not a paper account")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self.conn.in_transaction
            if not nested:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                if not nested:
                    self.conn.rollback()
                raise
            else:
                if not nested:
                    self.conn.commit()

    def _get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY opened_at,position_id").fetchall()
        result = []
        for row in rows:
            p = dict(row)
            for key in POSITION_DECIMALS:
                p[key] = decimal(p[key])
            p["quantity"] = p["qty"]
            p["tp_done"] = bool(p["tp_done"])
            p["liquidation_value"] = liquidation(p)
            result.append(p)
        return result

    def positions(self) -> list[dict[str, Any]]:
        with self.transaction():
            return self._positions()

    def _snapshot(self) -> dict[str, Any]:
        positions = self._positions()
        cash = decimal(self._get("cash"))
        marked = sum((p["liquidation_value"] for p in positions), ZERO)
        basis = sum((p["remaining_basis"] for p in positions), ZERO)
        stress = sum((p["remaining_basis"] + p["reserved_exit_cost"] for p in positions), ZERO)
        closed = self.conn.execute("SELECT realized_pnl FROM closed_positions").fetchall()
        profits = [decimal(row["realized_pnl"]) for row in closed]
        return {
            "mode": "PAPER", "starting_cash": decimal(self._get("starting_cash")), "cash": cash,
            "equity": cash + marked, "market_value": marked, "position_basis": basis,
            "realized_pnl": decimal(self._get("realized_pnl")), "unrealized_pnl": marked - basis,
            "fees_paid": decimal(self._get("fees_paid")), "stress_exposure": stress,
            "max_drawdown_pct": decimal(self._get("max_drawdown_pct")),
            "entry_halt": self._get("entry_halt") or None, "open_positions": len(positions),
            "unresolved_positions": sum(p["mark_status"] != "ok" for p in positions),
            "closed_positions": len(profits), "wins": sum(p > ZERO for p in profits),
            "losses": sum(p < ZERO for p in profits), "positions": positions,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.transaction():
            return self._snapshot()

    def _record_equity(self, now: int, reason: str) -> None:
        snapshot = self._snapshot()
        equity = snapshot["equity"]
        peak = max(decimal(self._get("max_equity")), equity)
        drawdown = ((peak - equity) / peak * D("100")) if peak > ZERO else ZERO
        self._set("max_equity", peak)
        self._set("max_drawdown_pct", max(decimal(self._get("max_drawdown_pct")), drawdown))
        self.conn.execute("INSERT INTO equity_history(observed_at,equity,cash,reason) VALUES(?,?,?,?)", (now, str(equity), str(snapshot["cash"]), reason))

    def set_halt(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A halt reason is required")
        with self.transaction():
            self._set("entry_halt", reason.strip())
            self.record_event("control:" + uuid.uuid4().hex, "entry_halt", {"reason": reason.strip()}, int(time.time()))

    def clear_halt(self) -> None:
        """Clears only manual entry halt; a daily-loss halt remains until the next UTC day."""
        with self.transaction():
            self._set("entry_halt", "")
            self.record_event("control:" + uuid.uuid4().hex, "entry_resume", {}, int(time.time()))

    def record_event(self, event_id: str, kind: str, payload: Any, observed_at: int) -> bool:
        if not isinstance(event_id, str) or not event_id or len(event_id) > 1024:
            raise ValueError("A bounded event ID is required")
        if not isinstance(kind, str) or not kind:
            raise ValueError("Event kind is required")
        encoded = json_text(payload)
        timestamp(observed_at)
        with self.transaction():
            cursor = self.conn.execute("INSERT OR IGNORE INTO events(event_id,kind,observed_at,payload) VALUES(?,?,?,?)", (event_id, kind, observed_at, encoded))
            return cursor.rowcount == 1

    def export_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction():
            rows = self.conn.execute("SELECT * FROM fills ORDER BY id").fetchall()
            names = [item[1] for item in self.conn.execute("PRAGMA table_info(fills)")]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["mode", *names])
            writer.writeheader()
            text_columns = {"order_id", "position_id", "event_id", "asset_id", "symbol", "strategy", "action", "reason"}
            for row in rows:
                values = dict(row)
                for key in text_columns:
                    value = str(values[key])
                    if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                        values[key] = "'" + value
                writer.writerow({"mode": "PAPER", **values})
        return path

    def close(self) -> None:
        with self._lock:
            self.conn.close()
