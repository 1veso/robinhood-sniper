"""Isolated paper accounts only; no provider, notification or live calls."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from sniper.console import make_server
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger, json_text
from sniper.policy import RiskPolicy
from sniper.radeon_controls import KEY, read_controls, update
from sniper.radeon_exits import RUNNER, HARVEST, activate, binding, revision
from sniper.radeon_portfolios import account, matched
import test_radeon_exits as exit_fixture
import test_parallel_entry_engine as entry_fixture
from test_engine import NOW, candidate, quote
from test_entry_strategies import make_proof, OTHER, TX2


class HarvestTests(unittest.TestCase):
    setUp=exit_fixture.RadeonExitTests.setUp
    tearDown=exit_fixture.RadeonExitTests.tearDown
    buy=exit_fixture.RadeonExitTests.buy
    tick=exit_fixture.RadeonExitTests.tick

    def activate(self, spec, **kw):
        return activate(self.l,self.policy,spec,now=kw.pop('now',NOW),expected_account=self.account,
            expected_source='source',expected_entry_revision='entry',review_reference='isolated audit',**kw)

    def test_successor_keeps_financial_rows_all_bindings_and_event_history(self):
        self.activate(RUNNER);p=self.buy();self.tick(p,45,NOW+1)
        tables=('fills','positions','closed_positions','daily_equity','equity_history','radeon_exit_positions')
        before={t:[tuple(r) for r in self.l.conn.execute('SELECT * FROM '+t)] for t in tables}
        events=[tuple(r) for r in self.l.conn.execute('SELECT * FROM events ORDER BY rowid')]
        with self.assertRaises(ValueError):self.activate(HARVEST,now=NOW+2)
        with self.assertRaises(ValueError):self.activate(HARVEST,now=NOW+2,expected_previous_revision='wrong')
        m=self.activate(HARVEST,now=NOW+2,expected_previous_revision=revision(RUNNER))
        self.assertEqual(m['ends_at'],self.manifest['ends_at'])
        for t in tables:self.assertEqual(before[t],[tuple(r) for r in self.l.conn.execute('SELECT * FROM '+t)])
        after=[tuple(r) for r in self.l.conn.execute('SELECT * FROM events ORDER BY rowid')]
        self.assertEqual(after[:-1],events)
        self.assertEqual(binding(self.l.conn,p['position_id'])['policy'],RUNNER)
        self.assertEqual(self.activate(HARVEST,now=NOW+3,expected_previous_revision=revision(RUNNER)),m)
        self.assertEqual(len(after),self.l.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0])
        self.assertEqual(self.tick(p,0,NOW+4)[0]['reason'],'break_even_protection')

    def test_harvest_sells_original_fractions_with_no_same_quote_cascade(self):
        self.activate(HARVEST);p=self.buy()
        for i,gain in enumerate((40,80,160),1):
            result=self.tick(p,gain,NOW+i)
            self.assertEqual(result[0]['quantity'],p['qty0']*D(('.50','.25','.15')[i-1]))
            self.assertEqual(self.tick(p,gain,NOW+i),[])
        self.assertEqual(self.l._positions()[0]['qty'],p['qty0']*D('.10'))
        self.assertEqual(self.tick(p,100,NOW+4)[0]['reason'],'trailing_stop')

    def test_harvest_arms_at_twenty_and_costs_can_still_make_close_negative(self):
        self.activate(HARVEST);p=self.buy();self.assertEqual(self.tick(p,20,NOW+1),[])
        self.assertIsNotNone(binding(self.l.conn,p['position_id'])['armed_at'])
        result=self.tick(p,0,NOW+2)[0]
        self.assertEqual(result['reason'],'break_even_protection');self.assertLess(result['pnl'],0)


class EntryPathTests(unittest.TestCase):
    def setUp(self):self.f=entry_fixture.ParallelEntryEngineTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def controls(self, **changes):
        v=read_controls(self.f.ledger);v.update(revision=1,updated_at=NOW,**changes)
        self.f.ledger._set(KEY,json_text(v))

    def test_pause_after_quote_blocks_final_admission(self):
        c,q=self.f.candidate(),self.f.quote()
        self.controls(enabled=False)
        self.assertEqual(self.f.engine.consider(c,q,self.f.now)['reason'],'operator_entries_paused')
        self.assertEqual(self.f.ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0],0)

    def test_disabled_whale_selects_qualified_two_wallet_path_once(self):
        c=self.f.candidate([make_proof(),make_proof(wallet=OTHER,tx=TX2)])
        self.controls(paths={'whale_follow':False,'two_wallet_v3':True})
        r=self.f.engine.consider(c,self.f.quote(),self.f.now)
        self.assertEqual(r['verdict'],'PAPER_BUY');self.assertEqual(r['initiating_strategy'],'two_wallet_v3')
        self.assertEqual(self.f.ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0],1)

    def test_all_paths_disabled_never_falls_back_to_a_paused_path(self):
        c=self.f.candidate()
        self.controls(paths={'whale_follow':False,'two_wallet_v3':False})
        self.assertIn('operator_entry_path_paused',self.f.engine.consider(c,self.f.quote(),self.f.now)['reason'])


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.folder=self.root/'runtime/exploration'
        self.l=Ledger(self.folder/'ledger.sqlite3',D(1000));self.policy=RiskPolicy();self.e=ExploratoryEngine(self.l,self.policy)
        self.manifest={'account_id':self.l._get('account_id'),'source_id':'fixture','started_at':NOW-100,'ends_at':NOW+10000}
        self.l._set('trial_manifest',json_text(self.manifest));(self.folder/'trial.json').write_text(json_text(self.manifest))
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def request(self, **kw):
        old=read_controls(self.l)
        return dict(key='primary',account_id=old['account_id'],revision=old['revision'],enabled=old['enabled'],paths=deepcopy(old['paths']),**kw)
    def c(self, event='first',now=NOW):return candidate(event=event,now=now,strategy=self.e.strategy_name)

    def test_switch_cas_identity_scope_and_noop(self):
        r=self.request();r['enabled']=False
        before=self.l._get('cash');v=update(self.root,r,now=NOW)
        self.assertFalse(v['enabled']);self.assertEqual(self.l._get('cash'),before)
        with self.assertRaises(ValueError):update(self.root,r,now=NOW)
        r=self.request();self.assertEqual(update(self.root,r,now=NOW),v)
        for bad in ({**r,'account_id':'other'},{**r,'key':'../elsewhere'},{**r,'enabled':1},{**r,'risk':'100'}):
            with self.assertRaises(ValueError):update(self.root,bad,now=NOW)
        self.assertEqual(self.l.conn.execute("SELECT COUNT(*) FROM events WHERE kind='operator_entry_controls'").fetchone()[0],1)

    def test_paused_exits_continue_and_resume_does_not_replay_candidates(self):
        self.assertEqual(self.e.consider(self.c(),quote(),NOW)['verdict'],'PAPER_BUY')
        r=self.request();r['enabled']=False;update(self.root,r,now=NOW+1)
        out=self.e.watch({quote().asset_id:quote(now=NOW+2,price='.4')},NOW+2)
        self.assertEqual(out[0]['reason'],'stop_loss')
        r=self.request();r['enabled']=True;update(self.root,r,now=NOW+3)
        self.assertEqual(self.e.consider(self.c('old',NOW+1),quote(now=NOW+4),NOW+4)['reason'],'candidate_predates_entry_resume')
        self.assertEqual(read_controls(self.l)['resume_after'],NOW+3)

    def test_bad_control_metadata_cannot_disable_exits_or_admit_entries(self):
        self.e.consider(self.c(),quote(),NOW);self.l._set(KEY,'{}')
        with self.assertRaises((ValueError,KeyError)):self.e.consider(self.c('next',NOW+1),quote(now=NOW+1),NOW+1)
        self.assertEqual(self.e.watch({quote().asset_id:quote(now=NOW+2,price='.4')},NOW+2)[0]['reason'],'stop_loss')

    def test_report_reconciles_closed_and_unavailable_marks_without_writes(self):
        self.e.consider(self.c(),quote(),NOW)
        before=self.l.conn.total_changes
        view=account(self.root,'primary',NOW+40)
        self.assertEqual(view['cohort']['entries'],1);self.assertEqual(view['cohort']['unresolved'],1)
        self.assertEqual(view['cohort']['realized_pnl'],0)
        self.assertEqual(self.l.conn.total_changes,before)
        self.assertEqual(len(view['trades'][0]['fills']),1)
        self.assertEqual(matched([view])['matched_closed'],0)

    def test_copied_seed_trade_is_visible_but_excluded_from_new_cohort(self):
        from sniper.paper_comparison import initialize
        self.e.consider(self.c(),quote(),NOW)
        self.e.watch({quote().asset_id:quote(now=NOW+1,price='.4')},NOW+1)
        initialize(self.root/'runtime/paper-comparison',self.folder/'ledger.sqlite3',
                   self.policy,self.manifest,now=NOW+2)
        view=account(self.root,'exit_baseline',NOW+3)
        self.assertEqual(view['excluded_seed_trades'],1)
        self.assertEqual(view['cohort']['entries'],0)
        self.assertEqual(view['cohort']['realized_pnl'],0)
        self.assertEqual(view['account_total']['closed'],1)
        self.assertEqual(matched([view])['matched_closed'],0)

    def test_http_requires_same_origin_token_and_fixed_account_identity(self):
        server=make_server(self.root,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            conn.request('GET','/api/radeon-entry-controls');r=conn.getresponse();token=json.loads(r.read())['token']
            body=json_text({**self.request(),'enabled':False});origin=f'http://127.0.0.1:{server.server_port}'
            for headers in ({},{'Origin':origin},{'Origin':'https://outside.example','X-Radeon-Control-Token':token},
                            {'Origin':origin,'X-Radeon-Control-Token':'\u00e9'}):
                conn.request('POST','/api/radeon-entry-controls',body,{'Content-Type':'application/json',**headers})
                r=conn.getresponse();r.read();self.assertEqual(r.status,403)
            conn.request('POST','/api/radeon-entry-controls',body,{'Content-Type':'application/json','Origin':origin,'X-Radeon-Control-Token':token})
            r=conn.getresponse();self.assertEqual(r.status,200);self.assertTrue(json.loads(r.read())['exits_continue'])
            self.assertFalse(read_controls(self.l)['enabled']);conn.close()
        finally:server.shutdown();server.server_close();thread.join()
