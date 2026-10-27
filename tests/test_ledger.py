import csv
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from decimal import Decimal as D
from pathlib import Path

from sniper.ledger import Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.sqlite"
        self.ledger = Ledger(self.path, D("1000"))

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_restart_preserves_cash_and_halt(self):
        with self.ledger.transaction():
            self.ledger._set("cash", "997.25")
        self.ledger.set_halt("review requested")
        other = Ledger(self.path, D("1000"))
        try:
            self.assertEqual(other.snapshot()["cash"], D("997.25"))
            self.assertEqual(other.snapshot()["entry_halt"], "review requested")
        finally:
            other.close()

    def test_cannot_reset_existing_starting_cash(self):
        failed_connection = sqlite3.connect(self.path, isolation_level=None)
        with patch("sniper.ledger.sqlite3.connect", return_value=failed_connection):
            with self.assertRaisesRegex(ValueError, "starting cash differs"):
                Ledger(self.path, D("10000"))
        with self.assertRaises(sqlite3.ProgrammingError):
            failed_connection.execute("SELECT 1")
        self.assertEqual(self.ledger.snapshot()["starting_cash"], D("1000"))

    def test_events_are_unique_and_durable(self):
        self.assertTrue(self.ledger.record_event("tx:0:1", "swap", {"cost": D("1.2")}, 100))
        self.assertFalse(self.ledger.record_event("tx:0:1", "swap", {"cost": D("999")}, 101))
        row = self.ledger.conn.execute("SELECT payload FROM events").fetchone()
        self.assertIn('"1.2"', row[0])

    def test_transaction_rolls_back_all_changes(self):
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction():
                self.ledger._set("cash", "5")
                self.ledger.record_event("will-rollback", "test", {}, 100)
                raise RuntimeError("crash before commit")
        self.assertEqual(self.ledger.snapshot()["cash"], D("1000"))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_rejects_nonfinite_cash_and_audit_payloads(self):
        for value in [D("NaN"), D("Infinity"), D("-1"), True]:
            with self.assertRaises(ValueError):
                Ledger(":memory:", value)
        with self.assertRaises(ValueError):
            self.ledger.record_event("bad", "test", {"bad": float("nan")}, 100)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_concurrent_connections_have_one_event_winner(self):
        other = Ledger(self.path, D("1000"))
        barrier = threading.Barrier(2)
        result = []
        def insert(ledger):
            barrier.wait()
            result.append(ledger.record_event("same", "swap", {}, 100))
        threads = [threading.Thread(target=insert, args=(ledger,)) for ledger in (self.ledger, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        other.close()
        self.assertEqual(sorted(result), [False, True])

    def test_export_empty_has_paper_schema(self):
        path = self.ledger.export_csv(Path(self.tmp.name) / "export.csv")
        with path.open() as f:
            reader = csv.DictReader(f)
            self.assertIn("mode", reader.fieldnames)
            self.assertIn("cash_delta", reader.fieldnames)
            self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()
