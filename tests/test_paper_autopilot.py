from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import paper_autopilot as autopilot
from sniper.autopilot_trial import enrollment, SPECS
from sniper.paper_window import require_open

class WindowTests(unittest.TestCase):
    def test_bound_window_and_exact_deadline(self):
        trial={'account_id':'a','mode':'paper','started_at':100,'ends_at':604900}
        require_open(trial,'a',100)
        require_open(trial,'a',604899)
        for account,now in [('b',100),('a',99),('a',604900)]:
            with self.assertRaises(ValueError):require_open(trial,account,now)
        with self.assertRaises(ValueError):require_open(dict(trial,mode='live'),'a',100)
        with self.assertRaises(ValueError):require_open(dict(trial,ends_at=900),'a',100)

    def setup_root(self,root):
        for bot,(dbpath,table,key,view) in SPECS.items():
            p=root/dbpath;p.parent.mkdir(parents=True,exist_ok=True)
            old={'account_id':bot,'mode':'paper','started_at':10,'ends_at':604810}
            if bot=='radeon':old['rules']={'real_money_enabled':False}
            meta={'account_id':bot,'execution_mode':'paper','demo':False,'cash':'123.45',key:old}
            with closing(sqlite3.connect(p)) as db, db:
                db.execute('CREATE TABLE '+table+' (key TEXT PRIMARY KEY,value TEXT)')
                db.executemany('INSERT INTO '+table+' VALUES(?,?)',[(k,json.dumps(v)) for k,v in meta.items()])
                db.execute('CREATE TABLE positions(id TEXT)');db.execute("INSERT INTO positions VALUES('held')")
            if bot in ('xenon','neon'):(root/view).with_name('account.json').write_text(json.dumps({'account_id':bot,'mode':'paper'}))
        (root/'dev_watch/runtime').mkdir(parents=True)

    def test_enrollment_preserves_accounts_cash_positions_and_rerun_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root)
            with patch('sniper.autopilot_trial.subprocess.run') as verify:
                verify.return_value.returncode=0
                first=enrollment(root,'User requested seven-day trial',1000)
                again=enrollment(root,'Repeated',2000)
            self.assertEqual(first,again)
            self.assertEqual(first['ends_at'],605800)
            self.assertEqual(first['aliases'],{'argon':'xenon'})
            for bot,(dbpath,table,key,view) in SPECS.items():
                with closing(sqlite3.connect(root/dbpath)) as db:
                    rows=dict(db.execute('SELECT key,value FROM '+table))
                    self.assertEqual(json.loads(rows['cash']),'123.45')
                    self.assertEqual(db.execute('SELECT * FROM positions').fetchall(),[('held',)])
                    self.assertEqual(json.loads(rows[key])['account_id'],bot)
                    self.assertEqual(json.loads(rows[key])['ends_at'],605800)
                self.assertTrue((root/'runtime/desktop/trial-backup/1000'/(bot+'.sqlite3')).is_file())

    def test_failed_validation_changes_no_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root)
            with patch('sniper.autopilot_trial.subprocess.run') as verify:
                verify.return_value.returncode=1
                with self.assertRaises(ValueError):enrollment(root,'request',1000)
            with closing(sqlite3.connect(root/SPECS['radeon'][0])) as db:
                self.assertEqual(json.loads(db.execute("SELECT value FROM metadata WHERE key='trial_manifest'").fetchone()[0])['started_at'],10)

    def test_expired_supervisor_blocks_entries_but_not_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'runtime/desktop';folder.mkdir(parents=True)
            (folder/'paper-autopilot.json').write_text(json.dumps({'state':'ACTIVE','mode':'paper','real_orders_enabled':False,'started_at':1,'ends_at':604801}))
            with patch.object(autopilot.desktop,'worker',return_value=0) as worker:
                self.assertEqual(autopilot.once('krypton-scan',root),0)
                worker.assert_not_called()
                self.assertEqual(autopilot.once('krypton-watch',root),0)
                worker.assert_called_once_with('krypton-watch',root,no_send=True)

class SupervisorTests(unittest.TestCase):
    def test_status_distinguishes_trial_enrollment_from_worker_health(self):
        from sniper.autopilot_status import public_status
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'runtime/desktop';folder.mkdir(parents=True)
            trial={'started_at':100,'ends_at':604900}
            (folder/'paper-autopilot.json').write_text(json.dumps({'state':'ACTIVE','accounts':{'xenon':{'trial':trial}}}))
            self.assertFalse(public_status(root,'solana',200)['supervisor_fresh'])
            (folder/'autopilot-status.json').write_text(json.dumps({'mode':'paper','updated_at':195}))
            self.assertTrue(public_status(root,'jev',200)['supervisor_fresh'])
            self.assertFalse(public_status(root,'solana',604900)['window_open'])

    @unittest.skipUnless(__import__('os').name=='nt','Windows job ownership')
    def test_supervisor_crash_terminates_owned_child(self):
        import ctypes,subprocess,sys
        from ctypes import wintypes
        code="import sys,subprocess,time;sys.path.insert(0,'src');from sniper.process_tree import launch;p,tree=launch([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)"
        owner=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=None
        try:
            child=int(owner.stdout.readline());handle=kernel.OpenProcess(0x100000,False,child)
            self.assertTrue(handle)
            owner.kill();owner.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            if owner.poll() is None:owner.kill();owner.wait(timeout=5)
            owner.stdout.close()
            if handle:kernel.CloseHandle(handle)

if __name__=='__main__':unittest.main()
