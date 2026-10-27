"""Atomic derived-history checkpoints, scoped to one wallet and frozen snapshot."""
import hashlib
import json
from pathlib import Path
import sqlite3


class HistoryProgress:
    def __init__(self, root, wallet, snapshot):
        # Change this version when reconciliation semantics change. Raw provider
        # evidence remains reusable; derived results must be recomputed.
        self.scope = hashlib.sha256(json.dumps(
            ['receipt-reconciliation-v2', wallet, snapshot], sort_keys=True).encode()).hexdigest()
        self.conn = None
        if isinstance(root, Path):
            self.conn = sqlite3.connect(root / 'history-progress.sqlite3')
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS checkpoints (
                scope TEXT NOT NULL, stage TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(scope, stage))''')
            self.conn.commit()

    def get(self, stage):
        if self.conn is None:
            return None
        row = self.conn.execute('SELECT payload FROM checkpoints WHERE scope=? AND stage=?',
                                (self.scope, stage)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, stage, payload):
        if self.conn is not None:
            with self.conn:
                self.conn.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)',
                                  (self.scope, stage, json.dumps(payload, allow_nan=False)))
        return payload

    def close(self):
        if self.conn is not None:
            self.conn.close()
