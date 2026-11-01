"""NITROGEN pipeline, exact TypeSafe gate, approval and accounting acceptance."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from sniper.nitrogen import judge,dossier,service
from sniper.nitrogen.book import Book,bound_config
from sniper.nitrogen.config import load,encode,digest
from sniper.nitrogen.demo import address,purchase,evidence,response,SyntheticModel
from sniper.nitrogen.model import Model,BudgetBlocked
from sniper.nitrogen.report import report
from sniper.nitrogen.view import snapshot,view

ROOT=Path(__file__).resolve().parents[1]


class GateTests(unittest.TestCase):
    def test_exact_article_boundaries(self):
        for kw,expected in [({},'BUY'),({'probability':.7,'organic':.65,'late':.499999,'score':2},'BUY'),
            ({'probability':.699999},'PASS'),({'organic':.649999},'PASS'),({'late':.5},'PASS'),({'late':.499999},'BUY'),
            ({'score':2.000001},'PASS'),({'action':'pass'},'PASS')]:
            with self.subTest(kw=kw):self.assertEqual(judge.gate(response(**kw))[0],expected)
    def test_confidence_does_not_replace_buy_probability(self):
        raw=response(probability=.69);raw['answers']['action']['confidence']=.99
        self.assertEqual(judge.gate(raw)[0],'PASS')
        raw=response(probability=.71);raw['answers']['action']['confidence']=.01
        self.assertEqual(judge.gate(raw)[0],'BUY')
    def test_watch_only_once(self):
        self.assertEqual(judge.gate(response('watch'),0)[0],'WATCH')
        self.assertEqual(judge.gate(response('watch'),1)[0],'PASS')
    def test_raw_score_and_full_distributions_retained(self):
        raw=response(score=1.7);out=judge.validate(raw)
        self.assertEqual(out,raw);self.assertEqual(out['answers']['exit_risk']['score']+1,2.7)
    def test_no_shared_policy_or_question_mutation(self):
        from sniper.jev import POLICY,QUESTIONS
        self.assertEqual(POLICY['late_yes_max'],.35)
        self.assertNotEqual(QUESTIONS,judge.QUESTIONS)
        self.assertEqual(set(judge.QUESTIONS),{'organic','late','exit_risk','action'})
        self.assertEqual(len(judge.LEVELS),5)
    def test_malformed_distributions_and_model_rejected(self):
        for mutate in [lambda r:r.update(model='unpinned'),lambda r:r['answers'].pop('late'),
            lambda r:r['answers']['action']['probabilities'].update(buy=float('nan')),
            lambda r:r['answers']['exit_risk'].update(score=2.1),lambda r:r['answers']['organic'].update(noul=True),
            lambda r:r['answers']['exit_risk']['legend'].update({'0':'different'})]:
            r=response();mutate(r)
            with self.assertRaises(ValueError):judge.validate(r)


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=1800000000;self.cfg=load(ROOT);self.cfg['tracked_wallets']=[address(1)]
        (self.root/'nitrogen_bot').mkdir();(self.root/'nitrogen_bot/config.json').write_text(encode(self.cfg))
        self.book=Book(self.root,self.cfg,initialize=True,demo=True,clock=lambda:self.now);self.addCleanup(lambda:self.book.close())
        self.id=self.book.discover_purchase(purchase(self.now),self.now);self.lead=self.book.lead(self.id)
        self.e=evidence(self.book,self.lead,self.now);self.model=SyntheticModel(self.book)
    def assess(self,model=None,e=None):return self.book.assess(self.id,e or self.e,model or self.model,self.now)
    def staged(self):
        self.assertEqual(self.assess(),'BUY');return self.book.pending()[0]
    def approve(self):
        order=self.staged();self.assertEqual(self.book.approve(order['id'],self.e,self.now),'FILLED');return order
    def test_staging_has_no_fill_and_approval_is_idempotent(self):
        o=self.staged();self.assertEqual(self.book.positions(),[]);self.assertEqual(self.book.db.execute('SELECT count(*) FROM fills').fetchone()[0],0)
        self.assertEqual(self.book.approve(o['id'],self.e,self.now),'FILLED')
        self.book.close();self.book=Book(self.root,self.cfg,demo=True,clock=lambda:self.now)
        self.assertEqual(self.book.approve(o['id'],{},self.now),'FILLED')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM fills').fetchone()[0],1)
    def test_generic_baseline_entry_disabled(self):
        with self.assertRaisesRegex(ValueError,'approve'):self.book.enter(None)
    def test_hard_failure_prevents_model_call(self):
        self.e['facts']['freeze_disabled']=False
        self.assertEqual(self.assess(),'RISK_REJECTED');self.assertEqual(self.model.calls,0)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM assessments').fetchone()[0],0)
    def test_missing_whale_or_transfer_prevents_admission(self):
        self.e['purchases'][0]['action']='TRANSFER'
        self.assertEqual(self.assess(),'WHALE_BLOCKED');self.assertEqual(self.model.calls,0)
    def test_missing_fields_explicit_and_no_invented_answer(self):
        self.e['supplement']['observations'].pop('holders_then')
        self.assertEqual(self.assess(),'EVIDENCE_BLOCKED');self.assertEqual(self.model.calls,0)
        d=json.loads(self.book.lead(self.id)['last_dossier']);self.assertEqual(d['observations']['holders_then'],'missing')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM assessments').fetchone()[0],0)
    def test_sourced_facts_and_nonpersuasive_state(self):
        d=dossier.build(self.book,self.lead,self.e,self.book.ticket(self.e['quote'],self.now),self.now)
        v=dossier.validate(d,self.cfg,self.now);self.assertEqual(v['missing'],[])
        state=dossier.state(d,v)
        for o in state['observations'].values():self.assertIn(o['source_ref'],state['source_refs'])
        self.assertNotIn('reason',state['risk']['checks'][0]);self.assertNotIn('analyst',state)
        self.e['supplement']['recommendation']='BUY NOW'
        with self.assertRaises(ValueError):self.assess()
    def test_missing_controller_review_and_exit_capacity_block(self):
        self.e['supplement']['independence_review']='missing'
        self.assertEqual(self.assess(),'EVIDENCE_BLOCKED');self.assertEqual(self.model.calls,0)
    def test_mismatched_windows_block(self):
        self.e['supplement']['window']['start']+=1
        self.assertEqual(self.assess(),'EVIDENCE_BLOCKED')
    def test_watch_survives_restart_and_new_external_id_cannot_reset(self):
        model=SyntheticModel(self.book,[response('watch'),response('watch')]);self.assertEqual(self.assess(model),'WATCH')
        due=self.book.lead(self.id)['due_at'];self.assertEqual(due,self.now+1800)
        self.assertEqual(self.assess(model),'WATCH_NOT_DUE');self.assertEqual(model.calls,1)
        self.book.close();self.book=Book(self.root,self.cfg,demo=True,clock=lambda:self.now)
        self.now=due;self.book.discover_purchase(purchase(self.now),self.now)
        self.assertEqual(self.book.lead(self.id)['watch_count'],1)
        model=SyntheticModel(self.book,[response('watch')]);fresh=evidence(self.book,self.book.lead(self.id),self.now)
        self.assertEqual(self.assess(model,fresh),'PASS');self.assertEqual(self.assess(model,fresh),'PASS');self.assertEqual(model.calls,1)
        with self.assertRaisesRegex(ValueError,'cycle'):self.book.discover_purchase(purchase(self.now,pool=address(22)),self.now)
    def test_watch_refresh_rechecks_hard_risk(self):
        self.assess(SyntheticModel(self.book,[response('watch')]));self.now+=1800
        fresh=evidence(self.book,self.book.lead(self.id),self.now);fresh['lp']['state']='FAIL';model=SyntheticModel(self.book)
        self.assertEqual(self.assess(model,fresh),'RISK_REJECTED');self.assertEqual(model.calls,0)
    def test_watch_cannot_use_old_dossier(self):
        self.assess(SyntheticModel(self.book,[response('watch')]));self.now+=1800
        fresh=evidence(self.book,self.book.lead(self.id),self.now);fresh['supplement']=self.e['supplement']
        model=SyntheticModel(self.book);self.assertEqual(self.assess(model,fresh),'EVIDENCE_BLOCKED');self.assertEqual(model.calls,0)
    def test_identical_confidence_different_probability_same_size(self):
        self.assess(SyntheticModel(self.book,[response(probability=.71)]));first=self.book.pending()[0]['payload']['ticket_usd']
        p=purchase(self.now,mint=address(30),pool=address(31));id=self.book.discover_purchase(p,self.now);e=evidence(self.book,self.book.lead(id),self.now)
        self.book.assess(id,e,SyntheticModel(self.book,[response(probability=.92)]),self.now)
        self.assertEqual(first,self.book.pending()[1]['payload']['ticket_usd'])
    def test_expired_order_and_stale_price_never_fill(self):
        o=self.staged();bad=deepcopy(self.e);bad['quote']['observed_at']-=61
        with self.assertRaisesRegex(ValueError,'Stale'):self.book.approve(o['id'],bad,self.now)
        self.now=o['expires'];self.assertEqual(self.book.approve(o['id'],{},self.now),'EXPIRED');self.assertEqual(self.book.positions(),[])
    def test_approval_rechecks_risk_whale_and_price_move(self):
        o=self.staged()
        for key in ('risk','whale','price'):
            e=deepcopy(self.e)
            if key=='risk':e['facts']['mint_disabled']=False
            if key=='whale':e['purchases']=[]
            if key=='price':e['quote']['price']='2'
            with self.assertRaises(ValueError):self.book.approve(o['id'],e,self.now)
        self.assertEqual(self.book.positions(),[])
    def test_approval_fresh_size_evidence_is_required(self):
        o=self.staged();self.e['supplement']['observations']['exit_ticket_usd']['value']=999
        with self.assertRaisesRegex(ValueError,'size'):self.book.approve(o['id'],self.e,self.now)
    def test_order_account_binding_and_foreign_evidence(self):
        o=self.staged();p=o['payload'];p['account_id']='helium'
        self.book.db.execute('UPDATE orders SET payload=? WHERE id=?',(encode(p),o['id']))
        with self.assertRaisesRegex(ValueError,'binding'):self.book.approve(o['id'],self.e,self.now)
    def test_cash_limit_and_paused_entries(self):
        o=self.staged();self.book.set('entries_paused',True)
        with self.assertRaisesRegex(ValueError,'paused'):self.book.approve(o['id'],self.e,self.now)
        self.book.set('entries_paused',False);self.book.set('cash','1')
        with self.assertRaises(ValueError):self.book.approve(o['id'],self.e,self.now)
    def test_duplicate_concurrent_approval_only_one_fill(self):
        o=self.staged();barrier=threading.Barrier(2);results=[];errors=[]
        def worker():
            try:
                with closing(Book(self.root,self.cfg,demo=True,clock=lambda:self.now)) as b:
                    barrier.wait(timeout=10);results.append(b.approve(o['id'],self.e,self.now))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(15)
        self.assertEqual(errors,[]);self.assertEqual(results,['FILLED','FILLED']);self.assertEqual(len(self.book.positions()),1)
    def test_reject_is_terminal(self):
        o=self.staged();self.assertEqual(self.book.reject_order(o['id'],self.now),'REJECTED')
        self.assertEqual(self.book.approve(o['id'],{},self.now),'REJECTED')
    def test_missing_price_no_sale_and_exit_independent_of_model(self):
        self.approve();self.book.set('entries_paused',True);self.book.set('model_gate',{'enabled':False})
        old=dict(self.e['quote'],observed_at=self.now-61)
        with self.assertRaises(ValueError):self.book.watch(old,self.now)
        self.assertEqual(len(self.book.positions()),1)
        self.assertEqual(self.book.watch(dict(self.e['quote'],price='.5'),self.now),'stop_loss');self.assertEqual(self.book.positions(),[])
    def test_partial_cost_basis_trailing_and_reconciliation(self):
        self.approve();p=self.book.positions()[0];basis=D(p['basis']);original=D(p['quantity'])
        self.assertEqual(self.book.watch(dict(self.e['quote'],price='2.2'),self.now),'partial_profit_1')
        p=self.book.positions()[0];self.assertLess(abs(D(p['quantity'])-original/2),D('1e-24'));self.assertLess(abs(D(p['basis'])-basis/2),D('1e-24'))
        self.assertEqual(self.book.watch(dict(self.e['quote'],price='1.4'),self.now),'trailing_exit')
        s=snapshot(self.book.db,self.now)['summary'];self.assertEqual(s['closed_trades'],1)
        self.assertEqual(D(s['equity']),D(s['starting_cash'])+D(s['realized_pnl']))
        self.assertEqual(D(s['cash'])+D(s['rent_reserved']),D(s['equity']))
    def test_missing_marks_net_unknown_not_fake_zero_sale(self):
        self.approve();s=snapshot(self.book.db,self.now+61)['summary'];self.assertIsNone(s['equity']);self.assertIsNone(s['net_pnl'])
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0],0)
    def test_timeout_no_retry_and_no_fabricated_answer(self):
        with patch.object(self.model,'ask',side_effect=TimeoutError):self.assess()
        row=dict(self.book.db.execute('SELECT * FROM assessments').fetchone());self.assertEqual(row['status'],'TIMEOUT');self.assertIsNone(row['response'])
        self.assertEqual(self.assess(),'PASS');self.assertEqual(self.model.calls,0)
    def test_unavailable_model_distinct_status(self):
        with patch.object(self.model,'ask',side_effect=PermissionError):self.assess()
        self.assertEqual(self.book.db.execute('SELECT status FROM assessments').fetchone()[0],'UNAUTHORIZED')
    def test_budget_failure_distinct_status(self):
        with patch.object(self.model,'ask',side_effect=BudgetBlocked):self.assess()
        self.assertEqual(self.book.db.execute('SELECT status FROM assessments').fetchone()[0],'BUDGET_BLOCKED')
    def test_model_cost_reserve_conservatively_reduces_cash(self):
        self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',('test',encode({'reservation_usd':'.01'})))
        s=snapshot(self.book.db,self.now)['summary'];self.assertEqual(s['cash'],'499.99');self.assertIsNone(s['net_pnl'])
    def test_report_reconciles_and_buckets_count_positions_not_partial_sales(self):
        self.approve();self.book.watch(dict(self.e['quote'],price='2.2'),self.now);self.book.watch(dict(self.e['quote'],price='1.4'),self.now)
        r=report(self.book,self.now);b=r['buy_probability_outcomes']['buckets'][1]
        self.assertEqual((b['assessed'],b['approved'],b['executed'],b['closed'],b['wins']),(1,1,1,1,1))
        self.assertEqual(b['net_trading_pnl'],r['account']['realized_pnl']);self.assertEqual(r['judge']['BUY'],1)
    def test_rejected_outcome_unknown_without_timely_mark(self):
        self.assess(SyntheticModel(self.book,[response('pass')]));self.now+=86400+301
        service.follow_outcomes(self.book,None)
        r=report(self.book,self.now);self.assertEqual(r['rejected_24h']['unknown'],1);self.assertIsNone(r['rejected_24h']['confirmed_rugs'])
    def test_rejected_outcome_hypothetical_not_trade(self):
        self.assess(SyntheticModel(self.book,[response('pass')]));self.now+=86400
        class P:
            def mark(_,mint,pool):return dict(self.e['quote'],observed_at=self.now,sol_observed_at=self.now,price='2')
        service.follow_outcomes(self.book,P());r=report(self.book,self.now)
        self.assertEqual(r['rejected_24h']['hypothetical_green'],1);self.assertEqual(r['account']['fill_count'],0)
    def test_foreign_database_and_config_changes_rejected(self):
        changed=dict(self.cfg,max_position_pct='.03')
        with self.assertRaises(ValueError):Book(self.root,changed,demo=True)
        self.assertEqual(bound_config(self.root),self.cfg)
        with tempfile.TemporaryDirectory() as other:
            folder=Path(other)/'nitrogen_bot/runtime';folder.mkdir(parents=True)
            with closing(sqlite3.connect(folder/'book.sqlite3')) as db:
                with db:db.execute('CREATE TABLE meta(key TEXT,value TEXT)');db.execute('INSERT INTO meta VALUES(?,?)',('bot_id','"radon"'))
            before=(folder/'book.sqlite3').read_bytes()
            with self.assertRaises(ValueError):Book(other,self.cfg,initialize=True)
            self.assertEqual((folder/'book.sqlite3').read_bytes(),before)
    def test_status_missing_account_is_read_only(self):
        with tempfile.TemporaryDirectory() as other:self.assertFalse(view(other)['initialized']);self.assertEqual(list(Path(other).iterdir()),[])
    def test_synthetic_cannot_be_imported_to_real_account(self):
        with tempfile.TemporaryDirectory() as other:
            with closing(Book(other,self.cfg,initialize=True)) as b:
                e=deepcopy(self.e['supplement']);e['account_id']=b.identity
                with self.assertRaisesRegex(ValueError,'label'):b.import_evidence(e)
    def test_own_paid_authorization_required_without_reading_key(self):
        model=Model(self.root,self.book);self.assertFalse(model.gate()['enabled'])
        with self.assertRaises(PermissionError):model.ask('test',{})
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM api_requests').fetchone()[0],0)
    def test_slot_registration_and_radon_unchanged(self):
        html=(ROOT/'console/index.html').read_text();self.assertIn('data-bot="nitrogen" data-slot="7"',html);self.assertIn('data-bot="radon" data-slot="8"',html)
        from sniper.console import ConsoleData
        # Account views expose distinct names and identities without creating other accounts.
        self.assertEqual(view(self.root)['slot'],7)
        from sniper.radon.view import view as radon_view
        self.assertEqual(radon_view(self.root)['slot'],8);self.assertFalse(radon_view(self.root)['initialized'])


class SchedulingTests(unittest.TestCase):
    def test_evening_berlin_schedule_once_with_sunday_cohorts(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as tmp:
            now=[int(datetime(2026,11,1,20,59,tzinfo=ZoneInfo('Europe/Berlin')).timestamp())]
            with closing(Book(tmp,load(ROOT),initialize=True,demo=True,clock=lambda:now[0])) as b:
                self.assertEqual(service.head(b,providers=object(),scheduled=True)['state'],'NOT_DUE')
                now[0]+=60;r=service.head(b,providers=object(),scheduled=True)
                self.assertEqual(r['date'],'2026-11-01');self.assertTrue(r['buy_probability_outcomes']['scheduled_sunday'])
                self.assertEqual(service.head(b,providers=object(),scheduled=True)['state'],'NOT_DUE')
                self.assertEqual(b.db.execute('SELECT count(*) FROM reports').fetchone()[0],1)
    def test_workers_skip_missing_account_and_package_excludes_private_runtime(self):
        import desktop
        import importlib.util
        with tempfile.TemporaryDirectory() as tmp,patch.object(desktop,'child') as child:
            for name in ('nitrogen-scan','nitrogen-watch','nitrogen-head'):self.assertEqual(desktop.worker(name,Path(tmp)),0)
            child.assert_not_called();self.assertFalse((Path(tmp)/'nitrogen_bot').exists())
        spec=importlib.util.spec_from_file_location('nitrogen_packaging_test',ROOT/'packaging/build_windows.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        names={p.relative_to(ROOT).as_posix() for p in m.source_files(ROOT)}
        self.assertIn('nitrogen_bot/config.json',names);self.assertIn('nitrogen_bot/README.md',names);self.assertIn('src/sniper/nitrogen/book.py',names)
        self.assertFalse(any('runtime/' in n or n.endswith('.env') for n in names))


class ProviderTests(unittest.TestCase):
    def test_only_finalized_actual_paid_swap_is_a_lead(self):
        from test_neon import receipt,key,NOW,SIG
        from sniper.nitrogen.providers import Providers
        with tempfile.TemporaryDirectory() as tmp:
            cfg=load(ROOT);cfg['tracked_wallets']=[key(77)]
            with closing(Book(tmp,cfg,initialize=True,demo=True,clock=lambda:NOW)) as b:
                provider=Providers(tmp,b,clock=lambda:NOW);tx=receipt()
                def rpc(method,args):
                    if method=='getSignaturesForAddress':return [{'signature':SIG,'err':None,'confirmationStatus':'finalized','blockTime':NOW}]
                    self.assertEqual(method,'getTransaction');return tx
                with patch.object(provider,'rpc',side_effect=rpc):
                    found=provider.search();self.assertEqual(len(found),1);self.assertEqual(found[0]['action'],'BUY');self.assertEqual(found[0]['wallet'],key(77))
                    tx['transaction']['message']['instructions']=[]
                    self.assertEqual(provider.search(),[])
                self.assertEqual(b.positions(),[])
