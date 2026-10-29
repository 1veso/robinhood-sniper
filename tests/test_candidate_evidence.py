import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.adapters.evm_store import EvmStore
from sniper.candidate_evidence import CandidateEvidence
from sniper.confirmation_book import ConfirmationBook
from test_paper_revision import proof, persist
from test_evm import WALLET, OTHER, POOL, bh
from test_engine import NOW


class CandidateEvidenceTests(unittest.TestCase):
    def test_failed_parallel_export_never_publishes_success_index(self):
        from sniper import candidate_evidence as module
        self.add(); self.import_rows()
        folder = self.root/'exports'
        self.journal.export(folder, self.state, [], {}, None, NOW)
        index = (folder/'wallet-evidence-index.json').read_bytes()
        atomic = module._atomic
        def fail_address(path, content):
            if Path(path).parent.name == 'addresses' and Path(path).suffix == '.json':
                raise OSError('fixture write failure')
            return atomic(path, content)
        with patch.object(module, '_atomic', side_effect=fail_address):
            with self.assertRaises(OSError):
                self.journal.export(folder, self.state, [], {}, None, NOW+1)
        self.assertEqual((folder/'wallet-evidence-index.json').read_bytes(), index)
        self.journal.export(folder, self.state, [], {}, None, NOW+2)
        self.assertEqual(json.loads((folder/'wallet-evidence-index.json').read_text())['generated_at'], NOW+2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = EvmStore(self.root/'source.sqlite3')
        self.book = ConfirmationBook(self.source)
        self.journal = CandidateEvidence(self.root/'evidence.sqlite3')
        self.state = {}

    def tearDown(self):
        self.journal.close()
        self.source.close()
        self.tmp.cleanup()

    def add(self, **changes):
        p = proof(**changes)
        persist(self.source, p)
        self.book.record(str(p['log_index']), p)
        return p

    def import_rows(self, **kw):
        return self.journal.import_confirmations(self.root/'source.sqlite3', self.state, **kw)

    def test_restart_deduplication_and_json_state_lag_recovery(self):
        p = self.add()
        first = self.import_rows(expected_source_id=self.source.identity)
        self.assertEqual(first['added_candidates'],1)
        self.assertEqual(first['imported_observations'],1)
        self.assertFalse(self.state['candidates'][p['wallet']]['qualified'])
        self.journal.close()
        self.journal = CandidateEvidence(self.root/'evidence.sqlite3')
        self.state = {}  # Process died after journal commit, before discovery.json.
        second = self.import_rows()
        self.assertEqual(second['added_candidates'],1)
        self.assertEqual(second['imported_observations'],0)
        self.assertEqual(self.state['candidates'][p['wallet']]['observed_recipient_transactions'],1)
        self.assertEqual(self.import_rows()['added_candidates'],0)

    def test_oldest_first_backlog_finishes_without_latest_window_starvation(self):
        for i in range(1,6):
            self.add(index=i,wallet='0x'+f'{i:040x}',now=NOW+i)
        first=self.import_rows(limit=2)
        self.assertEqual((first['last_rowid'],first['remaining']),(2,3))
        second=self.import_rows(limit=2)
        self.assertEqual((second['last_rowid'],second['remaining']),(4,1))
        third=self.import_rows(limit=2)
        self.assertEqual((third['last_rowid'],third['remaining']),(5,0))
        self.assertEqual(len(self.state['candidates']),5)

    def test_reorg_revokes_priority_but_keeps_orphaned_evidence(self):
        p=self.add()
        self.import_rows()
        self.source.db.execute('UPDATE evm_events SET canonical=0')
        result=self.import_rows()
        self.assertEqual(result['canonical_reconciled'],1)
        self.assertFalse(self.state['candidates'][p['wallet']]['observed_recipient'])
        self.assertEqual(self.state['candidates'][p['wallet']]['observed_recipient_transactions'],0)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
        self.assertEqual(self.journal.db.execute('SELECT canonical FROM observations').fetchone()[0],0)
        self.source.db.execute('UPDATE evm_events SET canonical=1')
        self.import_rows()
        self.assertTrue(self.state['candidates'][p['wallet']]['observed_recipient'])

    def test_roles_are_distinct_without_adding_payers_as_buyers(self):
        p=proof()
        p.update(transaction_sender=OTHER,base_payers=[OTHER,WALLET,OTHER])
        persist(self.source,p);self.book.record('roles',p)
        result=self.import_rows()
        self.assertEqual(result['imported_observations'],4)
        self.assertEqual(set(self.state['candidates']),{WALLET})
        self.assertEqual({r[0] for r in self.journal.db.execute('SELECT role FROM observations')},{'base_payer','transaction_sender','funded_recipient'})
        self.assertEqual(self.import_rows()['imported_observations'],0)

    def test_identity_and_payload_mismatch_roll_back_checkpoint_and_state(self):
        self.add(index=1)
        with self.assertRaises(ValueError):self.import_rows(expected_source_id='wrong-source')
        self.assertEqual(self.state,{})
        p=self.add(index=2,wallet=OTHER)
        wrong={**p,'block_number':999}
        self.source.db.execute('UPDATE paper_confirmations SET payload=? WHERE log_index=2',(json.dumps(wrong),))
        with self.assertRaises(ValueError):self.import_rows()
        self.assertEqual(self.state,{})
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)
        self.assertIsNone(self.journal._get('confirmation_rowid'))
        self.source.db.execute('UPDATE paper_confirmations SET payload=? WHERE log_index=2',(json.dumps(p),))
        self.import_rows()
        self.source.db.execute("UPDATE evm_metadata SET value='00000000-0000-4000-8000-000000000000' WHERE key='identity'")
        with self.assertRaises(ValueError):self.import_rows()

    def test_capacity_does_not_advance_past_an_unimported_candidate(self):
        self.journal.capacity=1
        self.add(index=1)
        self.add(index=2,wallet=OTHER)
        result=self.import_rows()
        self.assertTrue(result['capacity_blocked'])
        self.assertEqual(result['last_rowid'],1)
        self.assertEqual(result['remaining'],1)
        self.journal.capacity=2
        result=self.import_rows()
        self.assertEqual(result['added_candidates'],1)
        self.assertEqual(result['remaining'],0)

    def test_exports_distinguish_legacy_missing_history_and_private_raw_proof(self):
        p=proof();p.update(provider_url='https://private.invalid/API-SECRET',amount_raw='123',spent_raw='1000',
                          quote_address=OTHER,spend_basis='pool base input, not proven personal wallet expenditure')
        persist(self.source,p);self.book.record('one',p)
        self.import_rows()
        self.state['candidates'][OTHER]={'wallet':OTHER,'sampled_transactions':10,'example_transaction':'0x'+'f'*64,'qualified':False}
        folder=self.root/'exports'
        report={'wallet':WALLET,'assessment':'INCOMPLETE_HISTORY','history_complete':False,
                'performance_qualified':False,'net_pnl_after_follower_stress_usd':'0','qualification_blockers':['missing_history']}
        result=self.journal.export(folder,self.state,[report],{},None,NOW)
        self.assertEqual(result['address_count'],2)
        self.assertEqual(result['legacy_reference_only_count'],1)
        detail=json.loads((folder/f'addresses/{WALLET}.json').read_text())
        self.assertIsNone(detail['history']['net_pnl_after_follower_stress_usd'])
        self.assertFalse(detail['observations'][0]['raw_evidence_available'])
        self.assertEqual(detail['observations'][0]['amount_raw'],'123')
        self.assertEqual(detail['observations'][0]['spent_usd'],'100')
        self.assertEqual(detail['observations'][0]['quote_address'],OTHER)
        self.assertEqual(detail['observations'][0]['source_spend_basis'],'pool base input, not proven personal wallet expenditure')
        self.assertFalse(detail['qualified'])
        legacy=json.loads((folder/f'addresses/{OTHER}.json').read_text())
        self.assertEqual(legacy['evidence_status'],'legacy_reference_only')
        self.assertEqual(legacy['observations'],[])
        for file in folder.rglob('*'):
            if file.is_file():self.assertNotIn('API-SECRET',file.read_text())
        self.assertIn('API-SECRET',self.journal.db.execute('SELECT payload FROM proofs').fetchone()[0])
        with (folder/f'addresses/{WALLET}.csv').open(newline='') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(rows[0]['wallet'],WALLET)
        legacy_csv=folder/f'addresses/{OTHER}.csv'
        os.utime(legacy_csv,(NOW,NOW))
        previous_mtime=legacy_csv.stat().st_mtime_ns
        self.journal.export(folder,self.state,[report],{},None,NOW+1)
        self.assertEqual(legacy_csv.stat().st_mtime_ns,previous_mtime)
        self.assertEqual(json.loads((folder/f'addresses/{OTHER}.json').read_text())['generated_at'],NOW+1)

    def test_rpc_sender_capture_has_distinct_scope_and_exact_identity_checks(self):
        p=proof()
        p.update(source_id='rpc-swap-discovery-v1',role='transaction_sender',canonical_at_capture=True,
                 transaction={'hash':p['tx_hash'],'from':p['wallet'],'blockHash':p['block_hash']},
                 receipt={'transactionHash':p['tx_hash'],'blockHash':p['block_hash'],'blockNumber':hex(p['block_number']),'status':'0x1'},
                 block_header={'hash':p['block_hash'],'number':hex(p['block_number']),'timestamp':hex(p['observed_at'])})
        self.assertEqual(self.journal.add_sender_observations([p]),1)
        self.assertEqual(self.journal.add_sender_observations([p]),0)
        row=self.journal.db.execute('SELECT * FROM observations').fetchone()
        self.assertEqual(row['canonical_scope'],'rpc_verified_at_capture_only')
        self.assertEqual(row['role'],'transaction_sender')
        with self.assertRaises(ValueError):self.journal.add_sender_observations([{**p,'wallet':OTHER}])
        with self.assertRaises(ValueError):self.journal.add_sender_observations([{**p,'transaction_sender':OTHER}])
        with self.assertRaises(ValueError):self.journal.add_sender_observations([{**p,'transaction':{**p['transaction'],'blockNumber':'0x999'}}])

    def test_export_rechecks_controller_review_expiry_and_snapshot_binding(self):
        self.add();self.import_rows()
        report={'wallet':WALLET,'cutoff':NOW-100,'history_complete':True,'performance_qualified':True,
                'performance_estimate_available':True,'gas_usd':'1','closed_positions':20,
                'net_pnl_after_follower_stress_usd':'10','assessment':'COMPLETE_HISTORY'}
        review={'reviewed_at':NOW-10,'expires_at':NOW+10,'chain_id':4663,'history_cutoff':NOW-100,
                'independence_reviewed':True,'scope':'Fixture external review','cluster_id':'fixture',
                'evidence_urls':['https://example.org/fixture']}
        selection={WALLET:{'qualified':True,'cluster_id':'fixture','qualification_evidence':{
            'history_cutoff':NOW-100,'controller_review':review}}}
        folder=self.root/'exports'
        self.journal.export(folder,self.state,[report],selection,None,NOW)
        path=folder/f'addresses/{WALLET}.json'
        self.assertTrue(json.loads(path.read_text())['qualified'])
        self.journal.export(folder,self.state,[report],selection,None,NOW+11)
        self.assertFalse(json.loads(path.read_text())['qualified'])
        selection[WALLET]['qualification_evidence']['controller_review']['history_cutoff']=NOW-101
        self.journal.export(folder,self.state,[report],selection,None,NOW)
        self.assertFalse(json.loads(path.read_text())['qualified'])

    def test_complete_legacy_report_needs_available_finite_performance_metrics(self):
        self.add();self.import_rows()
        report={'wallet':WALLET,'cutoff':NOW-100,'history_complete':True,'performance_qualified':True,
                'performance_estimate_available':True,'net_pnl_after_follower_stress_usd':'10',
                'gas_usd':'1','closed_positions':20,'assessment':'COMPLETE_HISTORY'}
        review={'reviewed_at':NOW-10,'expires_at':NOW+10,'chain_id':4663,'history_cutoff':NOW-100,
                'independence_reviewed':True,'scope':'Fixture external review','cluster_id':'fixture',
                'evidence_urls':['https://example.org/fixture']}
        selection={WALLET:{'qualified':True,'cluster_id':'fixture','qualification_evidence':{
            'history_cutoff':NOW-100,'controller_review':review}}}
        folder=self.root/'exports'
        for changes in ({'performance_estimate_available':False},{'performance_estimate_available':None},
                        {'net_pnl_after_follower_stress_usd':'NaN'},{'gas_usd':'Infinity'},
                        {'gas_usd':None},{'gas_usd':True},{'gas_usd':'-1'},
                        {'closed_positions':None},{'closed_positions':True},{'closed_positions':1.5}):
            with self.subTest(changes=changes):
                result=self.journal.export(folder,self.state,[{**report,**changes}],selection,None,NOW)
                detail=json.loads((folder/f'addresses/{WALLET}.json').read_text())
                history=detail['history']
                self.assertTrue(history['history_complete'])
                self.assertTrue(history['controller_review_current'])
                self.assertFalse(history['performance_estimate_available'])
                self.assertFalse(history['performance_qualified'])
                self.assertFalse(detail['qualified'])
                self.assertEqual(result['qualified_count'],0)
                for field in ('net_pnl_after_follower_stress_usd','gas_usd','closed_positions'):
                    self.assertIsNone(history[field])

    def test_missing_or_not_yet_initialized_source_does_not_create_it(self):
        missing=self.root/'missing.sqlite3'
        self.assertFalse(self.journal.import_confirmations(missing,self.state)['source_available'])
        self.assertFalse(missing.exists())
        other=EvmStore(self.root/'empty.sqlite3')
        other.close()
        self.assertFalse(self.journal.import_confirmations(self.root/'empty.sqlite3',self.state)['source_available'])
