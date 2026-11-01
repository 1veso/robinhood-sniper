import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('restore_runtime',Path(__file__).resolve().parents[1]/'scripts/restore_runtime.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeRestoreTests(unittest.TestCase):
    def test_wal_and_local_credential_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';source.mkdir()
            dest=Path(folder)/'destination';dest.mkdir()
            (source/'xai-api-key.txt').write_text('old')
            (dest/'xai-api-key.txt').write_text('current')
            (source/'worker.lock').write_text('old-process')
            conn=sqlite3.connect(source/'ledger.sqlite3')
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('CREATE TABLE fills (id INTEGER PRIMARY KEY, amount TEXT)')
                conn.execute("INSERT INTO fills VALUES (1, '25')")
                conn.commit()
                before=(source/'ledger.sqlite3').read_bytes()
                MODULE.restore(source,dest)
                with sqlite3.connect(dest/'ledger.sqlite3') as restored:
                    self.assertEqual(restored.execute('SELECT amount FROM fills').fetchone()[0],'25')
                restored.close()
                self.assertEqual((dest/'xai-api-key.txt').read_text(),'current')
                self.assertEqual((source/'ledger.sqlite3').read_bytes(),before)
                self.assertFalse((dest/'worker.lock').exists())
            finally:
                conn.close()

    def test_conflicting_account_file_stops_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';source.mkdir()
            dest=Path(folder)/'destination';dest.mkdir()
            (source/'account.json').write_text('old-account')
            (dest/'account.json').write_text('new-account')
            with self.assertRaises(FileExistsError):MODULE.restore(source,dest)
            self.assertEqual((dest/'account.json').read_text(),'new-account')
