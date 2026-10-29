"""Transactional, reorganisation-aware journal for read-only EVM observations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class EvmStore:
    """Store canonical observations separately from all historical versions.

    No wallet or trading keys are stored here. Reorged observations are retained
    with canonical=0, never silently deleted. A chunk and its cursor commit in
    one transaction so a process crash cannot skip unpersisted events.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS evm_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evm_events (
                chain_id INTEGER NOT NULL,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                canonical INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chain_id, tx_hash, log_index, block_hash, kind)
            );
            CREATE INDEX IF NOT EXISTS evm_events_canonical
                ON evm_events(chain_id, canonical, block_number);
            CREATE TABLE IF NOT EXISTS evm_checkpoints (
                chain_id INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                canonical INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chain_id, block_number, block_hash)
            );
            CREATE TABLE IF NOT EXISTS evm_cursors (
                chain_id INTEGER PRIMARY KEY,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evm_reorgs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id INTEGER NOT NULL,
                previous_tip INTEGER NOT NULL,
                rewind_to INTEGER NOT NULL,
                observed_at INTEGER NOT NULL
            );
        """)
        # Atomic across independent processes opening the same new database.
        # Backups preserve this row; a newly created journal gets its own ID.
        self.db.execute(
            "INSERT OR IGNORE INTO evm_metadata(key,value) VALUES('identity',?)",
            (str(uuid.uuid4()),),
        )

    @property
    def identity(self) -> str:
        """Durable journal UUID; unchanged by restart or SQLite backup."""
        row = self.db.execute("SELECT value FROM evm_metadata WHERE key='identity'").fetchone()
        try:
            value = row["value"] if row else ""
            if str(uuid.UUID(value)) != value:
                raise ValueError("noncanonical UUID")
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("EVM journal identity is missing or invalid") from exc
        return value

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def cursor(self, chain_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT block_number, block_hash FROM evm_cursors WHERE chain_id=?", (chain_id,)
        ).fetchone()
        return dict(row) if row else None

    def checkpoints(self, chain_id: int, minimum_block: int = 0) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT block_number, block_hash FROM evm_checkpoints "
            "WHERE chain_id=? AND canonical=1 AND block_number>=? ORDER BY block_number DESC",
            (chain_id, minimum_block),
        ).fetchall()
        return [dict(row) for row in rows]

    def commit_chunk(
        self,
        chain_id: int,
        events: list[dict[str, Any]],
        headers: list[dict[str, Any]],
        tip: dict[str, Any],
        *,
        expected_cursor: dict[str, Any] | None = None,
        on_commit=None,
    ) -> int:
        inserted = 0
        with self.transaction():
            current = self.cursor(chain_id)
            if current != expected_cursor:
                raise RuntimeError("collector cursor changed concurrently; retry this chunk")
            if current and int(tip["number"]) < current["block_number"]:
                raise ValueError("refusing to move cursor backwards outside explicit reorg recovery")
            for event in events:
                params = (
                    chain_id, event["tx_hash"], event["log_index"], event["block_number"],
                    event["block_hash"], event["kind"], json.dumps(event, sort_keys=True, allow_nan=False),
                    event["observed_at"],
                )
                row = self.db.execute(
                    "SELECT canonical FROM evm_events WHERE chain_id=? AND tx_hash=? "
                    "AND log_index=? AND block_hash=? AND kind=?",
                    (chain_id, event["tx_hash"], event["log_index"], event["block_hash"], event["kind"]),
                ).fetchone()
                if row is None or not row["canonical"]:
                    inserted += 1
                self.db.execute(
                    "INSERT INTO evm_events(chain_id,tx_hash,log_index,block_number,block_hash,kind,payload,observed_at,canonical) "
                    "VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(chain_id,tx_hash,log_index,block_hash,kind) "
                    "DO UPDATE SET canonical=1,payload=excluded.payload,observed_at=excluded.observed_at",
                    params,
                )
            for h in headers + [tip]:
                self.db.execute(
                    "UPDATE evm_checkpoints SET canonical=0 WHERE chain_id=? AND block_number=? AND block_hash!=?",
                    (chain_id, h["number"], h["hash"]),
                )
                self.db.execute(
                    "INSERT INTO evm_checkpoints VALUES(?,?,?,1) "
                    "ON CONFLICT(chain_id,block_number,block_hash) DO UPDATE SET canonical=1",
                    (chain_id, h["number"], h["hash"]),
                )
            self.db.execute(
                "INSERT INTO evm_cursors VALUES(?,?,?) ON CONFLICT(chain_id) "
                "DO UPDATE SET block_number=excluded.block_number,block_hash=excluded.block_hash",
                (chain_id, tip["number"], tip["hash"]),
            )
            if on_commit is not None:
                # Optional source-owned progress belongs to the same atomic
                # observation/cursor commit. Strict collectors omit this hook.
                on_commit(inserted)
        return inserted

    def rewind(self, chain_id: int, ancestor: dict[str, Any], observed_at: int) -> int:
        """Invalidate observations after a verified common ancestor atomically."""
        with self.transaction():
            current = self.cursor(chain_id)
            if not current:
                return 0
            if ancestor["number"] > current["block_number"]:
                raise ValueError("reorg ancestor is after cursor")
            changed = self.db.execute(
                "UPDATE evm_events SET canonical=0 WHERE chain_id=? AND block_number>? AND canonical=1",
                (chain_id, ancestor["number"]),
            ).rowcount
            self.db.execute(
                "UPDATE evm_checkpoints SET canonical=0 WHERE chain_id=? AND block_number>?",
                (chain_id, ancestor["number"]),
            )
            self.db.execute(
                "UPDATE evm_cursors SET block_number=?,block_hash=? WHERE chain_id=?",
                (ancestor["number"], ancestor["hash"], chain_id),
            )
            self.db.execute(
                "INSERT INTO evm_reorgs(chain_id,previous_tip,rewind_to,observed_at) VALUES(?,?,?,?)",
                (chain_id, current["block_number"], ancestor["number"], observed_at),
            )
        return changed

    def reorgs(self, chain_id: int, after_id: int = 0) -> list[dict[str, Any]]:
        """Read durable reorg notices for an idempotent downstream acknowledgement.

        Callers should commit their invalidation and acknowledgement together in
        their own ledger. Reading never removes or acknowledges a notice here.
        """
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise ValueError("after_id must be a nonnegative integer")
        rows = self.db.execute(
            "SELECT id,chain_id,previous_tip,rewind_to,observed_at FROM evm_reorgs "
            "WHERE chain_id=? AND id>? ORDER BY id", (chain_id, after_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def events(self, chain_id: int, *, kind: str | None = None, canonical: bool | None = True) -> list[dict[str, Any]]:
        sql = "SELECT payload,canonical FROM evm_events WHERE chain_id=?"
        params: list[Any] = [chain_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if canonical is not None:
            sql += " AND canonical=?"
            params.append(int(canonical))
        sql += " ORDER BY block_number,log_index,kind"
        out = []
        for row in self.db.execute(sql, params).fetchall():
            item = json.loads(row["payload"])
            item["canonical"] = bool(row["canonical"])
            out.append(item)
        return out

    def pools(self, chain_id: int) -> dict[str, dict[str, Any]]:
        return {event["pool"]: event for event in self.events(chain_id, kind="pool_created")}

    def close(self) -> None:
        self.db.close()
