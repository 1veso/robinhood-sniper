"""Bounded source maintenance: isolated journals, no providers or strategy fills."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sniper.adapters.evm import CHAIN_ID
from sniper.adapters.evm_store import EvmStore
from sniper.exploratory_source import ExploratorySource, EXPIRY_CHECKPOINT, signal_id


class ExpiryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'events.sqlite3'
        self.now = 10000
        self.store = EvmStore(self.path)
        self.source = ExploratorySource(None, self.store, None, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, n, at=8000, canonical=1):
        item = {'tx_hash': '0x'+f'{n:064x}', 'log_index': 0, 'block_hash': '0x'+'ab'*32,
                'observed_at': at}
        self.store.db.execute('INSERT INTO evm_events VALUES(?,?,?,?,?,?,?,?,?)',
            (CHAIN_ID,item['tx_hash'],0,n,item['block_hash'],'sampled_swap',json.dumps(item),at,canonical))
        return item

    def checkpoint(self):
        row = self.store.db.execute('SELECT value FROM evm_metadata WHERE key=?',(EXPIRY_CHECKPOINT,)).fetchone()
        return json.loads(row[0]) if row else None

    def restart(self):
        self.store.close()
        self.store = EvmStore(self.path)
        self.source = ExploratorySource(None,self.store,None,clock=lambda:self.now)

    def test_completed_history_is_scanned_once_in_bounded_restart_safe_batches(self):
        with self.store.transaction():
            for i in range(1,1201):
                item = self.add(i)
                if i <= 1000: self.source.finish(item, {'reason':'already_evaluated'})
        for cursor in (500,1000):
            self.assertEqual(self.source.expire_pending(),0)
            self.assertEqual(self.source.expiry_progress['scanned'],500)
            self.assertEqual(self.checkpoint()['rowid'],cursor)
            self.restart()
        self.assertEqual(self.source.expire_pending(),200)
        self.assertEqual(self.checkpoint()['rowid'],1200)
        self.assertEqual(self.source.expire_pending(),0)
        self.assertEqual(self.source.expiry_progress['scanned'],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM exploration_outcomes').fetchone()[0],1200)

    def test_fresh_row_and_late_insert_are_not_skipped_when_they_expire(self):
        old=self.add(1);fresh=self.add(2,9500);late=self.add(3)
        self.assertEqual(self.source.expire_pending(),1)
        self.assertEqual(self.checkpoint()['rowid'],1)
        self.assertEqual(self.source.expiry_progress['status'],'WAITING_FOR_EXPIRY')
        self.restart();self.now+=901
        self.assertEqual(self.source.expire_pending(),2)
        for item in (old,fresh,late):
            result=json.loads(self.store.db.execute('SELECT payload FROM exploration_outcomes WHERE event_id=?',(signal_id(item),)).fetchone()[0])
            self.assertEqual(result['observed_at'],item['observed_at'])
            self.assertEqual(result['verdict'],'UNEVALUATED')

    def test_clock_bound_commits_only_finished_prefix_and_restores_sqlite_settings(self):
        for i in range(1,21): self.add(i)
        tick=[0]
        def clock(): tick[0]+=.2;return tick[0]
        count=self.source.expire_pending(monotonic=clock)
        self.assertGreater(count,0);self.assertLess(count,20)
        self.assertEqual(self.checkpoint()['rowid'],count)
        self.assertEqual(self.source.expiry_progress['status'],'BUDGET_DEFERRED')
        self.assertEqual(self.store.db.execute('PRAGMA busy_timeout').fetchone()[0],10000)
        self.restart()
        self.assertEqual(self.source.expire_pending(),20-count)

    def test_writer_lock_defers_without_ten_second_busy_wait(self):
        self.add(1)
        other=sqlite3.connect(self.path,isolation_level=None)
        other.execute('BEGIN IMMEDIATE')
        try:
            self.assertEqual(self.source.expire_pending(),0)
            self.assertEqual(self.source.expiry_progress['status'],'DATABASE_DEFERRED')
            self.assertIsNone(self.checkpoint())
        finally:other.execute('ROLLBACK');other.close()
        self.assertEqual(self.source.expire_pending(),1)

    def test_resolver_error_rolls_back_outcomes_and_cursor_together(self):
        for i in range(1,4):self.add(i)
        calls=[]
        def fail(event_id):
            calls.append(event_id)
            if len(calls)==2:raise RuntimeError('isolated crash')
        with self.assertRaisesRegex(RuntimeError,'isolated crash'):
            self.source.expire_pending(resolve_outcome=fail)
        self.assertIsNone(self.checkpoint())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM exploration_outcomes').fetchone()[0],0)
        self.restart();self.assertEqual(self.source.expire_pending(),3)

    def test_reorg_generation_rescans_restored_rows_and_preserves_existing_outcomes(self):
        item=self.add(1,canonical=0)
        self.assertEqual(self.source.expire_pending(),0)
        self.assertEqual(self.checkpoint()['rowid'],1)
        with self.store.transaction():
            self.store.db.execute('INSERT INTO evm_reorgs(chain_id,previous_tip,rewind_to,observed_at) VALUES(?,?,?,?)',(CHAIN_ID,1,0,self.now))
            self.store.db.execute('UPDATE evm_events SET canonical=1')
        self.restart();self.assertEqual(self.source.expire_pending(),1)
        self.assertEqual(self.checkpoint()['reorg_id'],1)
        self.assertEqual(self.source.expire_pending(),0)

    def test_foreign_checkpoint_is_not_adopted(self):
        self.store.db.execute('INSERT INTO evm_metadata VALUES(?,?)',(EXPIRY_CHECKPOINT,json.dumps({'source_id':'other','rowid':10,'reorg_id':0})))
        with self.assertRaisesRegex(ValueError,'binding'):self.source.expire_pending()

    def test_trading_summary_avoids_historical_scans_and_keeps_unknown_totals_null(self):
        self.add(1);self.add(2,9500)
        calls=[];self.store.db.set_trace_callback(calls.append)
        result=self.source.pending_summary(include_history=False,budget_seconds=.25)
        self.store.db.set_trace_callback(None)
        self.assertEqual(result['fresh'],1)
        self.assertIsNone(result['expired_recorded']);self.assertIsNone(result['expired_unrecorded'])
        self.assertIn('omitted',result['summary_unavailable_reason'])
        self.assertFalse(any('json_extract' in sql for sql in calls))
        self.assertFalse(any('e.observed_at<' in sql for sql in calls))


if __name__=='__main__':unittest.main()
