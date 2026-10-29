"""Offline capture isolation, transaction boundaries and honest capture time."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from rug_desk.book import Book, encode
from rug_desk.watch import apply_quote
from sniper.crew_capture import CachedEvidence, configuration, record
from sniper.jev_admission import assessment, paper_guard
from sniper.paper_events import market
from test_rug_desk import NOW, MINT, POOL, BUY, quote, raw_report, normalize, key


class CrewCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book = Book(self.root / 'rug_desk', initialize=True)
        self.settings_path = self.root / 'runtime/crew/settings.json'
        self.settings = {'schema': 'sniper_crew_v1', 'mode': 'COMPARISON_ONLY',
            'enabled': True, 'source_account_id': self.book.identity['account_id'],
            'source_config_sha256': self.book.identity['config_sha256'],
            'comparison_id': str(uuid.uuid4())}

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def configure(self, value=None):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(encode(self.settings if value is None else value), encoding='utf-8')

    def events(self, kind=None):
        rows = self.book.db.execute("SELECT kind,observed_at,payload FROM observation_events WHERE kind LIKE 'crew_%' ORDER BY seq")
        return [{'kind': r[0], 'at': r[1], 'data': json.loads(r[2])}
                for r in rows if kind is None or r[0] == kind]

    def capture(self, key='fixture', kind='market', payload=None, at=NOW):
        with self.book.transaction():
            record(self.book, key, kind, at, quote() if payload is None else payload)

    def test_absent_and_explicit_disabled_do_not_initialize_or_capture(self):
        self.capture()
        self.assertFalse(self.settings_path.parent.exists())
        self.configure({**self.settings, 'enabled': False})
        self.capture('disabled')
        self.assertEqual(self.events(), [])
        self.assertFalse((self.root / 'runtime/crew/evidence.sqlite3').exists())
        self.assertFalse((self.root / 'runtime/crew-comparison').exists())

    def test_failed_read_only_evidence_open_releases_windows_file_handle(self):
        self.configure()
        path = self.settings_path.parent / 'evidence.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE wrong_schema(value TEXT)')
            db.commit()
        with self.assertRaises(sqlite3.Error):
            CachedEvidence(self.root)
        renamed = path.with_suffix('.failed')
        path.rename(renamed)
        renamed.unlink()
        self.assertFalse(path.exists())

    def test_wrong_account_is_ignored_but_matching_wrong_config_is_gap(self):
        self.configure({**self.settings, 'source_account_id': str(uuid.uuid4())})
        self.capture('other-account')
        self.assertEqual(self.events(), [])
        self.configure({**self.settings, 'source_config_sha256': 'wrong'})
        self.capture('wrong-config')
        self.assertEqual([e['kind'] for e in self.events()], ['crew_capture_gap'])
        self.assertEqual(self.events()[0]['data']['reason'], 'comparison_settings_unreadable')

    def test_full_market_quote_cost_fields_survive_atomic_source_capture(self):
        self.configure()
        q = quote()
        q.update(network_fee_usd='0.000042', base_usd='198.12',
                 rent_reserve_usd='0.41', provider={'at': NOW, 'scope': 'fixture'})
        self.book.record_quote(q, NOW, 'fixture')
        item = self.events('crew_market_input')[0]['data']
        self.assertEqual(item['input'], q)
        self.assertEqual(item['source_account_id'], self.book.identity['account_id'])
        self.assertEqual(item['source_config_sha256'], self.book.identity['config_sha256'])
        self.assertEqual(item['comparison_id'], self.settings['comparison_id'])
        compact = json.loads(self.book.db.execute("SELECT payload FROM observation_events WHERE kind='market_observation'").fetchone()[0])
        self.assertNotIn('network_fee_usd', compact)
        self.assertEqual(self.book.fills(), [])

    def test_capture_and_source_observation_rollback_together(self):
        self.configure()
        before = self.book.db.execute('SELECT COUNT(*) FROM observation_events').fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.book.transaction():
                market(self.book, quote(), NOW, 'fixture')
                raise RuntimeError('rollback')
        self.assertEqual(self.book.db.execute('SELECT COUNT(*) FROM observation_events').fetchone()[0], before)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.book.fills(), [])

    def test_stable_source_key_is_deduplicated_after_restart(self):
        self.configure()
        self.book.record_quote(quote(), NOW, 'fixture')
        first = self.events()
        self.book.close()
        self.book = Book(self.root / 'rug_desk')
        self.book.record_quote(quote(), NOW, 'fixture')
        self.assertEqual(self.events(), first)
        self.assertEqual(len(first), 1)

    def test_malformed_settings_record_gap_and_keep_original_stop_exit(self):
        self.book.open_paper(quote(), BUY, NOW)
        position = self.book.positions()[0]['id']
        self.configure([])
        self.assertEqual(apply_quote(self.book, position, quote(price='.5', now=NOW+1), NOW+1), 'stop')
        self.assertEqual(len(self.book.fills()), 2)
        self.assertEqual(self.book.positions(), [])
        self.assertEqual(len(self.events('crew_capture_gap')), 1)
        self.assertEqual(self.events('crew_market_input'), [])

    def test_settings_missing_comparison_id_is_gap_without_source_write_failure(self):
        invalid = deepcopy(self.settings)
        del invalid['comparison_id']
        self.configure(invalid)
        self.book.record_quote(quote(), NOW, 'fixture')
        self.assertEqual(len(self.events('crew_capture_gap')), 1)
        self.assertEqual(self.book.db.execute("SELECT COUNT(*) FROM observation_events WHERE kind='market_observation'").fetchone()[0], 1)

    def test_research_capture_uses_actual_decision_time_and_has_no_baseline_authority(self):
        self.configure()
        result = {'mint': MINT, 'facts': normalize(MINT, raw_report(), NOW),
            'pool': quote(), 'stage': 'facts', 'reason': 'fixture', 'entry': False, 'started_at': NOW-20}
        with patch('sniper.crew_capture.time.time', return_value=NOW+17):
            self.capture('candidate-fixture', 'candidate', result)
        item = self.events('crew_candidate_input')[0]
        self.assertEqual(item['at'], NOW+17)
        packet = item['data']['input']
        self.assertEqual(packet['original_check_at'], NOW-20)
        self.assertEqual(packet['source_finished_at'], NOW)
        self.assertEqual(packet['decision_at'], NOW+17)
        self.assertEqual(packet['dossier']['snapshot_at'], NOW+17)
        self.assertIsNone(packet['baseline_packet'])
        self.assertFalse(packet['baseline']['eligible'])
        self.assertFalse(packet['baseline']['prospective'])
        self.assertFalse(packet['crew_packet']['crew_complete'])
        self.assertEqual(packet['crew_packet']['model_calls'], 0)
        self.assertEqual(self.book.fills(), [])

    def test_failed_candidate_capture_records_unknown_without_retroactive_admission(self):
        self.configure()
        with patch('sniper.crew_capture.candidate_packet', side_effect=ValueError('fixture binding mismatch')):
            with patch('sniper.crew_capture.time.time', return_value=NOW+20):
                self.capture('bad-candidate', 'candidate', {'mint': MINT})
        self.assertEqual(len(self.events('crew_capture_gap')), 1)
        item = self.events('crew_candidate_input')[0]
        self.assertEqual(item['at'], NOW+20)
        self.assertIsNone(item['data']['input']['crew_packet'])
        self.assertIsNone(item['data']['input']['baseline_packet'])
        self.assertIsNone(item['data']['input']['original_check_at'])
        self.assertEqual(item['data']['input']['source_finished_at'], NOW)

    def test_invalid_start_time_stays_unknown_on_failed_capture(self):
        self.configure()
        for i, started in enumerate(('unknown', NOW+1, True, -1)):
            with patch('sniper.crew_capture.candidate_packet', side_effect=ValueError('fixture')):
                with patch('sniper.crew_capture.time.time', return_value=NOW+20):
                    self.capture('unknown-start:'+str(i), 'candidate', {'mint': MINT, 'started_at': started})
        for item in self.events('crew_candidate_input'):
            self.assertIsNone(item['data']['input']['original_check_at'])
            self.assertEqual(item['data']['input']['source_finished_at'], NOW)

    def test_failed_judge_preserves_ambiguous_cost_only_with_recorded_paid_authority(self):
        self.configure()
        result = {'mint': MINT, 'facts': normalize(MINT, raw_report(), NOW),
                  'pool': quote(), 'stage': 'judge', 'entry': False, 'started_at': NOW-1}
        for enabled in (False, True):
            authority = {'grok_enabled': enabled, 'jev_enabled': False}
            with patch('sniper.crew_comparison.paid_authority', return_value=authority):
                with patch('sniper.crew_capture.time.time', return_value=NOW):
                    self.capture('ambiguous:'+str(enabled), 'candidate', result)
            item = self.events('crew_candidate_input')[-1]['data']['input']
            self.assertEqual(item['paid_authority'], authority)
            self.assertEqual(item['ambiguous_model_attempt'], enabled)
            self.assertNotIn('billed_usd', item)
            self.assertIsNone(item['baseline_packet'])
        self.assertEqual(self.book.fills(), [])

    def test_assessment_from_another_pool_cannot_be_captured_as_this_candidate(self):
        self.configure()
        result = {'mint': MINT, 'pool': quote(), 'jev': {'assessment_id': 'fixture'}}
        wrong = {'account_id': self.book.identity['account_id'],
                 'dossier': {'mint': MINT, 'pool': key(99)}, 'baseline': {}}
        with patch('sniper.jev_review.detail', return_value=wrong):
            self.capture('wrong-assessment-pool', 'candidate', result)
        self.assertEqual(len(self.events('crew_capture_gap')), 1)
        self.assertIsNone(self.events('crew_candidate_input')[0]['data']['input']['crew_packet'])

    def test_configured_admission_reads_only_existing_bound_evidence(self):
        self.configure()
        evidence_path = self.settings_path.parent / 'evidence.sqlite3'
        evidence_path.touch()
        store = Mock()
        journal = Mock()
        journal.assess.return_value = {'status': 'INSUFFICIENT_EVIDENCE'}
        facts = normalize(MINT, raw_report(), NOW)
        with patch('sniper.crew_capture.CachedEvidence', return_value=store) as opened:
            with patch('sniper.crew_evidence.enrich', side_effect=lambda d, s, **kw: d) as enriched:
                with patch('sniper.jev_admission.Journal', return_value=journal):
                    assessment(self.root, self.book.identity, facts, quote(), NOW)
        opened.assert_called_once_with(self.root)
        self.assertEqual(enriched.call_count, 1)
        store.close.assert_called_once()
        self.assertIsNone(journal.assess.call_args.kwargs['client'])

    def test_disabled_or_other_account_admission_never_opens_evidence(self):
        self.configure()
        (self.settings_path.parent / 'evidence.sqlite3').touch()
        journal = Mock()
        journal.assess.return_value = {'status': 'INSUFFICIENT_EVIDENCE'}
        for changed in ({'enabled': False}, {'source_account_id': str(uuid.uuid4())}):
            self.configure({**self.settings, **changed})
            with patch('sniper.crew_capture.CachedEvidence', side_effect=AssertionError('must not initialize')):
                with patch('sniper.jev_admission.Journal', return_value=journal):
                    assessment(self.root, self.book.identity, normalize(MINT, raw_report(), NOW), quote(), NOW)
        self.assertEqual(self.book.fills(), [])

    def test_bound_bad_config_fails_closed_before_jev_admission(self):
        self.configure({**self.settings, 'source_config_sha256': 'wrong'})
        (self.settings_path.parent / 'evidence.sqlite3').touch()
        with patch('sniper.jev_admission.Journal', side_effect=AssertionError('must not assess')):
            with self.assertRaisesRegex(ValueError, 'binding'):
                assessment(self.root, self.book.identity, normalize(MINT, raw_report(), NOW), quote(), NOW)
        self.assertEqual(self.book.fills(), [])

    def test_primary_final_guard_requires_fresh_quote_for_recorded_exit_proof(self):
        reviewed = {'dossier': {'exit_route': {'source_ref': 'rug_desk:crew:fixture'}}}
        with patch('sniper.jev_admission.enabled_for', return_value=True), \
             patch('sniper.jev_admission.settings', return_value={'enabled': True}), \
             patch('sniper.jev_admission.require_paper_acceptance'), \
             patch('sniper.jev_admission.require_assessment', return_value=reviewed), \
             patch('sniper.crew_capture.CachedEvidence', side_effect=AssertionError('no quote, no read')):
            with self.assertRaisesRegex(ValueError, 'Fresh quote'):
                paper_guard(self.book, 'fixture', MINT, POOL, NOW)

    def test_primary_final_guard_rechecks_actual_quote_and_propagates_insufficient_coverage(self):
        reviewed = {'dossier': {'exit_route': {'source_ref': 'rug_desk:crew:fixture'}}}
        cached = Mock()
        cached.records_for.return_value = [{'fixture': 'immutable'}]
        fresh = quote(now=NOW+1)
        with patch('sniper.jev_admission.enabled_for', return_value=True), \
             patch('sniper.jev_admission.settings', return_value={'enabled': True}), \
             patch('sniper.jev_admission.require_paper_acceptance'), \
             patch('sniper.jev_admission.require_assessment', return_value=reviewed), \
             patch('sniper.crew_capture.CachedEvidence', return_value=cached), \
             patch('sniper.crew_comparison.require_quantity_coverage', side_effect=ValueError('quantity exceeds coverage')) as bounded:
            with self.assertRaisesRegex(ValueError, 'exceeds coverage'):
                paper_guard(self.book, 'fixture', MINT, POOL, NOW+1, quote=fresh)
        bounded.assert_called_once_with(reviewed['dossier'], cached.records_for.return_value, fresh, self.book.cost_policy(), NOW+1)
        cached.close.assert_called_once()
        self.assertEqual(self.book.fills(), [])


if __name__ == '__main__':
    unittest.main()
