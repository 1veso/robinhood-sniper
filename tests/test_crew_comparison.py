"""Isolated prospective portfolio checks. No provider, model or strategy writes."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rug_desk.book import Book, encode
from sniper.crew_comparison import Comparison, initialize, make_baseline_packet, validate_baseline
from sniper.jev import POLICY, validate_response
from sniper.jev_dossier import digest
from test_jev import fixture, response
from test_rug_desk import NOW, MINT, POOL, key, quote, BUY


def full_quote(now=NOW, **kwargs):
    return {**quote(now=now, **kwargs), 'sol_usd': '200', 'sol_observed_at': now}


class CrewComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source' / 'rug_desk'
        self.source = Book(self.root, initialize=True)
        self.source.enable_costs(NOW - 1)
        self.addCleanup(self.source.close)
        self.folder = Path(self.temp.name) / 'comparison'
        self.binding = patch('sniper.crew_comparison.code_binding', return_value={'isolated_fixture': 'a' * 64})
        self.binding.start(); self.addCleanup(self.binding.stop)
        self.account = self.source.identity['account_id']

    def trial(self, start=NOW - 1, end=NOW + 10000):
        out = {'account_id': self.account, 'started_at': start, 'ends_at': end}
        with self.source.transaction():
            self.source.db.execute("INSERT OR REPLACE INTO metadata VALUES('trial',?)", (encode(out),))
        return out

    def comparison(self, started=True):
        if started: self.trial()
        initialize(self.folder, self.root, now=NOW)
        out = Comparison(self.folder, source_account_id=self.account)
        self.addCleanup(out.close)
        return out

    def payload(self, at=NOW, mint=MINT, price='1', paid=True):
        d, b = fixture(at)
        previous = d['account_id']
        d['account_id'] = self.account
        d['candidate_id'] = d['candidate_id'].replace(previous, self.account)
        d['mint'] = mint
        for field in ('independence_review', 'exit_route'): d[field]['mint'] = mint
        b.update(account_id=self.account, candidate_id=d['candidate_id'],
                 policy_sha256=self.source.identity['config_sha256'], prospective=True, verdict='BUY', eligible=True)
        a = {'id': digest({'dossier': d, 'policy': POLICY}), 'account_id': self.account,
             'candidate_id': d['candidate_id'], 'snapshot_at': at, 'started': at, 'finished': at,
             'status': 'COMPLETE', 'attempted': 1, 'dossier': d, 'baseline': b,
             'result': {'policy': POLICY, 'proposed': 'BUY', 'response': validate_response(response()), 'estimated_api_usd': '.001'}}
        judgment = deepcopy(BUY)
        judgment.update(judged_at=at, request_id='fixture:' + mint + ':' + str(at),
                        binding={'account_id': self.account, 'settings_sha256': self.source.identity['config_sha256']},
                        usage={'cost_verified': True, 'billed_usd': '.01'})
        with patch('sniper.crew_comparison.paid_authority', return_value={'observed_at': at, 'grok_enabled': paid, 'jev_enabled': paid}):
            packet = make_baseline_packet(self.root, a, judgment, at)
        return {'result': {'mint': mint, 'pool': full_quote(at, mint=mint, price=price), 'stage': 'entry', 'judgment': judgment},
                'baseline_packet': packet, 'crew_packet': None, 'producer_records': []}

    def full_payload(self, at=NOW, price='1.2'):
        from test_crew_review import produced, replace_record, model_fixture
        from sniper.crew_review import review
        p = self.payload(at, price=price)
        d,b,records = produced(at, full=True)
        d['candidate_id'] = d['candidate_id'].replace(d['account_id'], self.account)
        d['account_id'] = self.account
        b.update(account_id=self.account, candidate_id=d['candidate_id'], policy_sha256=self.source.identity['config_sha256'],
                 prospective=True, eligible=True, verdict='BUY')
        def bind_record(record):
            record.update(account_id=self.account, config_sha256=self.source.identity['config_sha256'])
            for component in ('venue', 'exit'):
                proof = record.get(component) or {}
                if proof.get('program_review'):
                    proof['program_review'].update(account_id=self.account, config_sha256=self.source.identity['config_sha256'])
                    proof['review_sha256'] = digest(proof['program_review'])
        for i in range(len(records)):
            replace_record(d, records, i, bind_record)
        a = p['baseline_packet']['assessment']; a.update(dossier=d, baseline=b, candidate_id=d['candidate_id'], id=digest({'dossier':d,'policy':POLICY}))
        j=model_fixture(d,b);j['usage']['billed_usd']='.01'
        p['baseline_packet']['judgment']=j;p['result']['judgment']=j
        p['baseline_packet']['packet_id']=digest({k:v for k,v in p['baseline_packet'].items() if k!='packet_id'})
        p['producer_records']=records
        p['crew_packet']=review(d,b,now=at,producer_records=records,model_evidence=j)
        return p

    def capture(self, c, key_, kind, at, data):
        return c.capture(key_, kind, at, data, captured_at=at + .5)

    def decision(self, c, variant='baseline'):
        return json.loads(c.portfolios()[variant].db.execute('SELECT payload FROM crew_decisions ORDER BY seq DESC LIMIT 1').fetchone()[0])

    def test_no_invented_bankroll_or_clock_source_untouched(self):
        c = self.comparison(False)
        self.capture(c, 'one', 'candidate', NOW, self.payload())
        c.process_pending()
        report = c.report(NOW)
        self.assertEqual(self.source.fills(), [])
        self.assertEqual(len({self.account, *c.manifest['accounts'].values()}), 3)
        for v in report['variants'].values():
            self.assertEqual(v['state'], 'SETUP_BLOCKED'); self.assertEqual(v['new_fills'], 0)
            for k in ('cash', 'equity', 'return_pct', 'starting_cash'): self.assertIsNone(v[k])
        self.assertEqual(c.manifest['trial'], None)
        self.assertFalse((self.folder / 'baseline/runtime/notifications.sqlite3').exists())

    def test_no_reset_or_manufactured_trial(self):
        with self.assertRaises(ValueError):
            initialize(self.folder, self.root, now=NOW, trial={'started_at': NOW, 'ends_at': NOW + 1})
        c = self.comparison(False)
        with self.assertRaises(ValueError): initialize(self.folder, self.root, now=NOW)
        self.assertIsNone(c.refresh_trial(NOW))

    def test_valid_baseline_reapplies_original_book_full_crew_missing_blocks(self):
        c = self.comparison()
        p = self.payload()
        validate_baseline(p['baseline_packet'], p['result']['pool'], c.manifest, NOW)
        self.capture(c, 'candidate', 'candidate', NOW, p); c.process_pending()
        self.assertTrue(self.decision(c)['entry'])
        self.assertFalse(self.decision(c, 'crew')['entry'])
        self.assertEqual(len(c.portfolios()['baseline'].fills()), 1)
        self.assertEqual(self.source.fills(), [])

    def test_paid_gate_cannot_be_bypassed_by_model_buy(self):
        c = self.comparison(); self.capture(c, 'candidate', 'candidate', NOW, self.payload(paid=False)); c.process()
        self.assertIn('paid-model', self.decision(c)['reason'])
        self.assertEqual(c.report(NOW)['variants']['baseline']['new_fills'], 0)

    def test_future_or_wrong_account_baseline_packet_rejected(self):
        c = self.comparison(); p = self.payload(); p['baseline_packet']['decided_at'] = NOW + 1
        body = {k:v for k,v in p['baseline_packet'].items() if k != 'packet_id'}
        p['baseline_packet']['packet_id'] = digest(body)
        self.capture(c, 'candidate', 'candidate', NOW, p); c.process()
        self.assertIn('binding', self.decision(c)['reason'])

    def test_immutable_dedup_and_original_capture_time(self):
        c = self.comparison(False); p = self.payload()
        seq = self.capture(c, 'stable', 'candidate', NOW, p)
        self.assertEqual(c.capture('stable', 'candidate', NOW, p, captured_at=NOW + 500), seq)
        self.assertEqual(c.db.execute('SELECT captured_at FROM observations').fetchone()[0], NOW + .5)
        p['result']['stage'] = 'different'
        with self.assertRaises(ValueError): self.capture(c, 'stable', 'candidate', NOW, p)
        for at, captured in ((NOW-1,NOW),(NOW+2,NOW+1),(NOW,float('nan'))):
            with self.assertRaises(ValueError): c.capture('bad', 'gap', at, {}, captured_at=captured)

    def test_ordered_tape_and_digest_corruption_fail_closed(self):
        c = self.comparison(); self.capture(c, 'late', 'gap', NOW + 1, {'reason':'fixture'})
        with self.assertRaises(ValueError): self.capture(c, 'earlier', 'gap', NOW, {})
        c.db.execute("UPDATE observations SET payload='{}'"); c.db.commit()
        with self.assertRaises(ValueError): c.process()
        self.assertEqual(c.portfolios()['baseline'].fills(), [])

    def test_cursor_fill_rollback_and_restart_no_duplicate(self):
        c = self.comparison(); self.capture(c, 'candidate', 'candidate', NOW, self.payload())
        original = c._financial
        with patch.object(c, '_financial', side_effect=RuntimeError('isolated interrupted commit')):
            with self.assertRaises(RuntimeError): c.process()
        b = c.portfolios()['baseline']
        self.assertEqual(b.fills(), []); self.assertFalse(b.checked(MINT))
        self.assertEqual(b.db.execute("SELECT value FROM metadata WHERE key='crew_cursor'").fetchone()[0], '0')
        c.process(); self.assertEqual(len(b.fills()), 1)
        self.assertEqual(c.process(), {'baseline':0,'crew':0})
        reopened = Comparison(self.folder, source_account_id=self.account)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.process(), {'baseline':0,'crew':0})
        self.assertEqual(len(reopened.portfolios()['baseline'].fills()), 1)

    def test_seen_once_rejection_cannot_reenter_after_late_evidence(self):
        c = self.comparison(); self.capture(c, 'first', 'candidate', NOW, self.payload(paid=False))
        self.capture(c, 'later', 'candidate', NOW+1, self.payload(NOW+1)); c.process()
        self.assertIn('already checked', self.decision(c)['reason'])
        self.assertEqual(c.portfolios()['baseline'].fills(), [])

    def test_seed_positions_keep_original_exits_and_seed_fills_excluded(self):
        self.source.open_paper(full_quote(NOW-1), BUY, NOW-1)
        c = self.comparison()
        self.capture(c, 'stop', 'watch', NOW+1, {'quotes':[full_quote(NOW+1, price='.5')]}); c.process()
        for v in c.report(NOW+1)['variants'].values():
            self.assertEqual(v['new_entries'], 0); self.assertEqual(v['new_fills'], 1); self.assertEqual(v['open_positions'], 0)
        self.assertEqual(len(self.source.positions()), 1); self.assertEqual(len(self.source.fills()), 1)

    def test_original_deadline_stops_entries_but_not_exits(self):
        self.source.open_paper(full_quote(NOW-1), BUY, NOW-1); self.trial(end=NOW+1)
        initialize(self.folder, self.root, now=NOW)
        c = Comparison(self.folder, source_account_id=self.account); self.addCleanup(c.close)
        self.capture(c, 'late', 'candidate', NOW+2, self.payload(NOW+2, mint=key(4)))
        self.capture(c, 'exit', 'watch', NOW+3, {'quotes':[full_quote(NOW+3, price='.5')]}); c.process()
        self.assertIn('entry period', self.decision(c)['reason'])
        for v in c.report(NOW+3)['variants'].values(): self.assertEqual((v['state'],v['new_entries'],v['new_fills']),('EXITS_ONLY',0,1))

    def test_source_stream_change_blocks_import(self):
        c = self.comparison()
        with self.source.transaction(): self.source.db.execute("UPDATE metadata SET value='different' WHERE key='observation_stream_id'")
        with self.assertRaises(ValueError): self.capture(c, 'bad', 'gap', NOW, {})

    def test_trial_adoption_only_from_existing_source(self):
        c = self.comparison(False)
        with self.assertRaises(ValueError): self.capture(c, 'fake', 'trial', NOW, {'trial': {'started_at': NOW}})
        real = self.trial(); c.refresh_trial(NOW); c.process()
        self.assertEqual(c.refresh_trial(NOW+1),1)
        self.assertEqual(c.report(NOW)['variants']['baseline']['original_trial'], real)
        changed = {**real, 'ends_at': NOW+20000}
        with self.source.transaction(): self.source.db.execute("UPDATE metadata SET value=? WHERE key='trial'", (encode(changed),))
        c.refresh_trial(NOW+1)
        with self.assertRaises(ValueError): c.process()

    def test_unknown_model_invoice_never_becomes_zero(self):
        c = self.comparison(); self.capture(c, 'candidate', 'candidate', NOW, self.payload()); c.process()
        v = c.report(NOW)['variants']['baseline']
        self.assertEqual(v['model_api_requests'], 2); self.assertFalse(v['api_costs_known'])
        self.assertIsNone(v['net_pnl']); self.assertIsNone(v['period_net_pnl']); self.assertIsNone(v['observed_max_drawdown_usd'])
        self.assertEqual(v['api_cost_usd'], '0.01')

    def test_enabled_failed_model_stage_is_ambiguous_not_free(self):
        c=self.comparison(False)
        p=self.payload();p['baseline_packet']=None;p['result'].pop('judgment');p['result']['stage']='judge'
        p.update(ambiguous_model_attempt=True,paid_authority={'grok_enabled':True},source_finished_at=NOW)
        self.capture(c,'ambiguous','candidate',NOW,p);c.process()
        v=c.report(NOW)['variants']['baseline']
        self.assertEqual(v['ambiguous_model_api_attempts'],1);self.assertFalse(v['api_costs_known'])
        self.assertIsNone(v['net_pnl']);self.assertEqual(v['api_cost_usd'],'0')

    def test_disabled_model_stage_does_not_invent_an_attempt(self):
        c=self.comparison(False)
        p={'result':{'mint':MINT,'stage':'judge','pool':full_quote()},'ambiguous_model_attempt':True,
           'paid_authority':{'grok_enabled':False},'source_finished_at':NOW}
        self.capture(c,'disabled','candidate',NOW,p);c.process()
        v=c.report(NOW)['variants']['baseline']
        self.assertEqual(v['ambiguous_model_api_attempts'],0);self.assertTrue(v['api_costs_known'])

    def test_rejected_before_jev_keeps_bound_grok_cost(self):
        c=self.comparison(False);p=self.payload();p['baseline_packet']=None
        self.capture(c,'rejected','candidate',NOW,p);c.process()
        v=c.report(NOW)['variants']['baseline']
        self.assertEqual(v['model_api_requests'],1);self.assertEqual(v['api_cost_usd'],'0.01')
        self.assertEqual(v['new_fills'],0)

    def test_first_post_start_candidate_adopts_existing_source_clock_atomically(self):
        c=self.comparison(False);real=self.trial(start=NOW+1,end=NOW+1000)
        self.capture(c,'candidate','candidate',NOW+2,self.payload(NOW+2));c.process()
        self.assertTrue(self.decision(c)['entry'])
        self.assertEqual(c.report(NOW+2)['variants']['baseline']['original_trial'],real)
        self.assertEqual(self.source.fills(),[])

    def test_cost_attribution_and_explicit_invoice_reconciliation(self):
        c = self.comparison(False)
        unknown = {'request_id':'one','scope':'crew','billed_usd':None,'estimated_usd':'.02'}
        self.capture(c, 'request', 'cost', NOW, unknown); c.process()
        r=c.report(NOW)['variants']; self.assertTrue(r['baseline']['api_costs_known']); self.assertFalse(r['crew']['api_costs_known'])
        self.capture(c, 'invoice', 'cost', NOW+1, {**unknown,'billed_usd':'.03','invoice_reference':'isolated:invoice'})
        c.process(); r=c.report(NOW+1)['variants']
        self.assertEqual(r['baseline']['model_api_requests'],0); self.assertEqual(r['crew']['net_pnl'],'-0.03')

    def test_missing_and_wrong_pool_quotes_never_make_fictional_exit(self):
        self.source.open_paper(full_quote(NOW-1), BUY, NOW-1); c = self.comparison()
        bad = full_quote(NOW+1, price='.1'); bad['pool']=key(5)
        self.capture(c, 'wrong', 'watch', NOW+1, {'quotes':[bad]}); c.process()
        for v in c.report(NOW+1)['variants'].values(): self.assertEqual((v['open_positions'],v['new_fills'],v['unresolved_positions']), (1,0,1))
        self.capture(c, 'failure', 'watch', NOW+2, {'supplemental':True,'quotes':[], 'failures':[{'mint':MINT,'pool':POOL,'reason':'unavailable'}]}); c.process()
        self.assertTrue(c.portfolios()['baseline'].positions()[0]['feed_error'])

    def test_rejected_later_outcomes_same_pool_no_net_or_winner_invented(self):
        c = self.comparison(False); self.capture(c, 'candidate', 'candidate', NOW, self.payload())
        wrong=full_quote(NOW+1,price='5');wrong['pool']=key(5)
        self.capture(c,'wrong','watch',NOW+1,{'quotes':[wrong]})
        q=full_quote(NOW+2,price='2')
        self.capture(c,'later','watch',NOW+2,{'quotes':[q]})
        self.capture(c,'duplicate','watch',NOW+3,{'quotes':[q]}); c.process()
        out=c.report(NOW+3)['variants']['baseline']['later_rejected_outcomes'][0]
        self.assertEqual(len(out['samples']),1);self.assertEqual(out['samples'][0]['price_return_pct'],'100')
        self.assertIsNone(out['samples'][0]['net_hypothetical_pnl']);self.assertEqual(self.source.fills(),[])

    def test_rejected_net_requires_exact_quantity_size_evidence(self):
        c=self.comparison(False);self.capture(c,'candidate','candidate',NOW,self.payload());c.process()
        basis=self.decision(c)['basis_scenario'];q=full_quote(NOW+1,price='2')
        q['exit_evidence']={'executable':True,'side':'sell','mint':MINT,'pool':POOL,'observed_at':NOW+1,
                            'source_ref':'isolated:receipt','input_quantity':str(D(basis['quantity'])/2),'net_output_usd':'40'}
        self.capture(c,'undersized','watch',NOW+1,{'quotes':[q]});c.process()
        out=c.report(NOW+1)['variants']['baseline']['later_rejected_outcomes'][0]['samples'][0]
        self.assertIsNone(out['net_hypothetical_pnl'])

    def test_gap_and_bounded_quote_rotation_are_explicit(self):
        c=self.comparison(False)
        self.capture(c,'one','candidate',NOW,self.payload())
        self.capture(c,'two','candidate',NOW+1,self.payload(NOW+1,mint=key(3)))
        self.capture(c,'gap','gap',NOW+2,{'reason':'source_history_gap','skipped':5});c.process_pending(limit=1)
        self.assertEqual(c.report(NOW+2)['variants']['baseline']['backlog'],2)
        c.process_pending();a=c.candidate_targets(limit=1,now=NOW+2);b=c.candidate_targets(limit=1,now=NOW+2)
        self.assertNotEqual(a,b);self.assertEqual(len(c.report(NOW+2)['gaps']),1)

    def test_full_crew_admission_requires_complete_roles(self):
        c=self.comparison();p=self.payload()
        p['crew_packet']={'verdict':'PASS','reviewed_at':NOW,'crew_complete':False,'packet_id':'isolated'}
        self.capture(c,'candidate','candidate',NOW,p)
        # Review's own evidence tests cover its full reconstruction. This tests the
        # comparison's extra completeness condition after an independently valid PASS.
        with patch('sniper.crew_review.validate_packet',return_value='PASS'):c.process()
        self.assertIn('Full coordinated crew incomplete',self.decision(c,'crew')['reason'])

    def test_real_complete_packet_admits_both_with_original_costs_and_exits(self):
        c=self.comparison();p=self.full_payload()
        self.assertTrue(p['crew_packet']['crew_complete']);self.assertEqual(p['crew_packet']['verdict'],'PASS')
        self.capture(c,'candidate','candidate',NOW,p);c.process()
        for name,book in c.portfolios().items():
            self.assertTrue(self.decision(c,name)['entry'],self.decision(c,name)['reason'])
            self.assertEqual(len(book.fills()),1);self.assertIsNotNone(book.fills()[0]['execution_costs'])
        self.capture(c,'stop','watch',NOW+1,{'quotes':[full_quote(NOW+1,price='.5')]});c.process()
        for v in c.report(NOW+1)['variants'].values():self.assertEqual((v['new_entries'],v['new_fills'],v['open_positions']),(1,2,0))

    def test_refreshed_quote_cannot_exceed_reviewed_exit_quantity(self):
        c=self.comparison();p=self.full_payload(price='.8')
        self.capture(c,'candidate','candidate',NOW,p);c.process()
        self.assertTrue(self.decision(c)['entry'])
        self.assertIn('exceeds independently reviewed exit size',self.decision(c,'crew')['reason'])
        self.assertEqual(c.portfolios()['crew'].fills(),[])

    def test_report_html_escapes_unknown_and_hostile_gap_text(self):
        c=self.comparison(False);self.capture(c,'gap','gap',NOW,{'reason':'<script>alert(1)</script>'});c.process()
        c.publish(NOW);document=(self.folder/'report.html').read_text(encoding='utf-8')
        self.assertIn('UNKNOWN',document);self.assertIn('&lt;script&gt;',document);self.assertNotIn('<script>',document)


if __name__ == '__main__': unittest.main()
