"""Real DuckDB and SQLite/WAL integration; no provider or execution requests."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper import desk_analytics as analytics


@unittest.skipUnless(importlib.util.find_spec('duckdb'), 'Optional analytics dependency not installed')
class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def source(self, name='radeon', account='paper-account', mode='paper', fixture=None):
        path = self.root / analytics.SOURCES[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE fills(id INTEGER PRIMARY KEY,at INTEGER,payload TEXT);
            CREATE TABLE positions(id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE equity_history(id INTEGER PRIMARY KEY,observed_at INTEGER,equity TEXT);
            CREATE TABLE api_requests(id TEXT PRIMARY KEY,payload TEXT);''')
        values = {'account_id':account, 'execution_mode':mode, 'api_key':'DO_NOT_COPY'}
        if fixture is not None:
            values['fixture'] = json.dumps(fixture)
        db.executemany('INSERT INTO metadata VALUES(?,?)', values.items())
        db.execute('INSERT INTO fills VALUES(1,100,?)', [json.dumps({'pnl':'-2.000000000000000001'})])
        db.execute('INSERT INTO positions VALUES(?,?)', ['held', '{"quantity":"0.123456789123456789"}'])
        db.execute("INSERT INTO equity_history VALUES(1,100,'997.999999999999999999')")
        db.execute("INSERT INTO api_requests VALUES('secret','DO_NOT_COPY')")
        db.commit()
        self.addCleanup(db.close)
        return path, db

    def test_read_committed_wal_without_source_mutation_or_secret_copy(self):
        path, writer = self.source()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        wal_before = hashlib.sha256(Path(str(path)+'-wal').read_bytes()).hexdigest()
        writer.execute("INSERT INTO fills VALUES(2,200,'uncommitted')")
        result = analytics.collect(self.root)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256(Path(str(path)+'-wal').read_bytes()).hexdigest(), wal_before)
        source = next(s for s in result['sources'] if s['source']=='radeon')
        self.assertEqual(source['rows']['fills'], 1)
        self.assertEqual(source['equity_history_rows'], 1)
        self.assertIsNone(source['fixture'])
        self.assertFalse(result['execution_authority'])
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            raw = db.execute('SELECT table_name,data FROM latest_records').fetchall()
            self.assertNotIn('DO_NOT_COPY', repr(raw))
            self.assertIn('-2.000000000000000001', repr(raw))
            self.assertIn('0.123456789123456789', repr(raw))
        writer.rollback()

    def test_deduplicates_snapshots_but_preserves_updates_deletes_and_latest_missing(self):
        path, writer = self.source()
        first = analytics.collect(self.root)
        analytics.collect(self.root)
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM collections').fetchone()[0], 2)
        writer.execute("UPDATE positions SET payload='closed'")
        writer.execute('DELETE FROM fills')
        writer.commit()
        analytics.collect(self.root)
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM latest_records WHERE table_name='fills'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE table_name='fills'").fetchone()[0], 1)
        writer.close()
        path.unlink()
        missing = analytics.collect(self.root)
        self.assertEqual(next(s for s in missing['sources'] if s['source']=='radeon')['status'], 'MISSING')
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM latest_records').fetchone()[0], 0)
        self.assertNotEqual(first['collection_id'], missing['collection_id'])

    def test_accounts_and_fixture_provenance_are_kept_separate(self):
        self.source(account='account-a')
        self.source('experiment-neon', account='account-b', mode='experimental_paper', fixture=True)
        report = analytics.collect(self.root)
        sources = {s['source']:s for s in report['sources']}
        self.assertEqual(sources['radeon']['account_id'], 'account-a')
        self.assertEqual(sources['experiment-neon']['account_id'], 'account-b')
        self.assertIs(sources['experiment-neon']['fixture'], True)
        self.assertEqual(report['aliases'], {'argon':'xenon'})
        self.assertIn('oxygen-original', report['unsupported'])

    def test_native_ledger_neon_helium_and_experiment_schemas(self):
        from decimal import Decimal
        from sniper.ledger import Ledger
        from sniper.neon import NeonBook
        from sniper.helium.config import load
        from sniper.helium.paper import Paper
        from sniper.experimental_paper import Account
        ledger = Ledger(self.root/analytics.SOURCES['radeon'], Decimal('1000'))
        self.addCleanup(ledger.close)
        neon = NeonBook(self.root/'neon_bot', initialize=True)
        self.addCleanup(neon.close)
        cfg = load(Path(__file__).resolve().parents[1]/'helium_bot')
        helium = Paper(self.root/'helium_bot/runtime', cfg, initialize=True, demo=True)
        self.addCleanup(helium.close)
        experiment = Account(self.root, 'xenon', initialize=True,
                             reference='Synthetic analytics acceptance', now=100, fixture=True)
        self.addCleanup(experiment.close)
        report = analytics.collect(self.root)
        sources = {s['source']:s for s in report['sources']}
        for name in ('radeon','neon','helium','experiment-xenon'):
            self.assertEqual(sources[name]['status'], 'OK', sources[name])
        self.assertIs(sources['helium']['fixture'], True)
        self.assertIs(sources['experiment-xenon']['fixture'], True)

    def test_duplicate_accounts_are_not_presented_as_independent_sources(self):
        self.source(account='same-account')
        self.source('neon',account='same-account')
        report = analytics.collect(self.root)
        sources = {s['source']:s for s in report['sources']}
        self.assertEqual(sources['radeon']['status'], 'OK')
        self.assertEqual(sources['neon']['status'], 'ERROR')
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertEqual(db.execute('SELECT DISTINCT source FROM latest_records').fetchall(), [('radeon',)])

    def test_incompatible_archive_schema_is_not_rewritten(self):
        self.source()
        analytics.collect(self.root)
        with closing(analytics.open_archive(self.root)) as db:
            db.execute("UPDATE analytics_meta SET schema='future-incompatible'")
        before = (analytics.paths(self.root)/'desk.duckdb').read_bytes()
        with self.assertRaisesRegex(ValueError,'Unsupported analytics schema'):
            analytics.collect(self.root)
        self.assertEqual((analytics.paths(self.root)/'desk.duckdb').read_bytes(),before)

    def test_interrupted_collection_rolls_back_and_preserves_publication(self):
        self.source()
        first = analytics.collect(self.root)
        with patch.object(analytics, 'capture', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                analytics.collect(self.root)
        self.assertEqual(analytics.report(self.root)['collection_id'],first['collection_id'])
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM collections').fetchone()[0],1)

    def test_missing_does_not_create_accounts_and_live_or_redirected_sources_fail_closed(self):
        report = analytics.collect(self.root)
        self.assertTrue(all(s['status']=='MISSING' for s in report['sources']))
        self.assertFalse((self.root/'rug_desk').exists())
        path, writer = self.source(mode='live')
        result = analytics.collect(self.root)
        self.assertEqual(next(s for s in result['sources'] if s['source']=='radeon')['status'], 'ERROR')
        writer.close()
        path.unlink()
        external = self.root/'other.sqlite3'
        with closing(sqlite3.connect(external)) as db:
            db.execute('CREATE TABLE untouched(n INTEGER)')
        try:
            path.symlink_to(external)
        except OSError:
            return  # Windows test users may not have symlink creation privileges.
        with self.assertRaises(ValueError):
            analytics.capture(self.root, 'radeon', analytics.SOURCES['radeon'])

    def test_limits_do_not_import_partial_source_or_overwrite_previous_collection(self):
        self.source()
        with self.assertRaises(ValueError):
            analytics.capture(self.root, 'radeon', analytics.SOURCES['radeon'], max_rows=1)
        with self.assertRaises(ValueError):
            analytics.capture(self.root, 'radeon', analytics.SOURCES['radeon'], max_bytes=2)
        first = analytics.collect(self.root)
        with patch.object(analytics, 'report_connection', side_effect=RuntimeError('publication failed')):
            with self.assertRaises(RuntimeError):
                analytics.collect(self.root)
        self.assertEqual(json.loads((analytics.paths(self.root)/'status.json').read_text())['collection_id'], first['collection_id'])

    def test_report_is_read_only_and_external_access_disabled(self):
        self.source()
        analytics.collect(self.root)
        dbpath = analytics.paths(self.root)/'desk.duckdb'
        before = hashlib.sha256(dbpath.read_bytes()).hexdigest()
        analytics.report(self.root)
        self.assertEqual(hashlib.sha256(dbpath.read_bytes()).hexdigest(), before)
        with closing(analytics.open_archive(self.root, readonly=True)) as db:
            self.assertFalse(db.execute("SELECT current_setting('enable_external_access')").fetchone()[0])
            with self.assertRaises(Exception):
                db.execute('DELETE FROM snapshots')


class AnalyticsSupervisorTests(unittest.TestCase):
    def test_job_is_opt_in_and_runs_outside_entry_window(self):
        import paper_autopilot as autopilot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.object(autopilot.desktop, 'child') as child:
                self.assertEqual(autopilot.once('desk-analytics',root), 0)
                child.assert_not_called()
            with patch.object(analytics, 'duckdb_module'):
                analytics.configure(root, True)
            folder = root/'runtime/desktop'
            folder.mkdir(parents=True)
            (folder/'paper-autopilot.json').write_text(json.dumps({
                'state':'ACTIVE','mode':'paper','real_orders_enabled':False,'started_at':0,'ends_at':604800}))
            with patch.object(autopilot.desktop, 'child', return_value=0) as child:
                self.assertEqual(autopilot.once('desk-analytics',root),0)
                child.assert_called_once_with(['desk_analytics.py','collect'],90,root)
            self.assertNotIn('desk-analytics',autopilot.WINDOW_JOBS)
            self.assertNotIn('desk-analytics',autopilot.ENTRY_JOBS)
            analytics.configure(root, False)
            self.assertFalse(autopilot.job_enabled('desk-analytics',root))

    def test_optional_module_can_be_imported_without_duckdb(self):
        with patch.dict('sys.modules', {'duckdb':None}):
            with self.assertRaisesRegex(RuntimeError, 'requirements-analytics'):
                analytics.duckdb_module()


if __name__ == '__main__':
    unittest.main()
