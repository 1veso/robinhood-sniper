"""Radon financial-accounting, provenance, isolation, and integration acceptance tests."""
import contextlib
import copy
from decimal import Decimal as D
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper.radon.book import Book, bound_config
from sniper.radon.config import load, encode, policy_hash
from sniper.radon.demo import address, bundle, event, quote, evidence
from sniper.radon.model import Model
from sniper.radon.policy import buy_plan
from sniper.radon.replay import replay, research
from sniper.radon.source import validate_bundle, read, SourceUnavailable
from sniper.radon.view import view, snapshot
from sniper.radon import service

ROOT = Path(__file__).resolve().parents[1]


class RadonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.now = 1800000000
        self.cfg = load(ROOT/'radon_bot')
        (self.root/'radon_bot').mkdir()
        (self.root/'radon_bot/config.json').write_text(encode(self.cfg))
        self.book = Book(self.root, self.cfg, initialize=True, demo=True, clock=lambda: self.now)
        self.addCleanup(lambda: self.book.close())
        self.a, self.b, self.mint = address(1), address(2), address(20)
        self.rows = []
        self.source = bundle(self.now)
        self.book.publish_shortlist([self.a, self.b], self.now-90, 'test-research')

    def add(self, id, leader=None, side='buy', qty='100', at=None, mint=None):
        leader = leader or self.a
        seq = 1+sum(r['leader']==leader for r in self.rows)
        row = event(id, leader, seq, self.now-5 if at is None else at, side, qty, mint)
        self.rows.append(row)
        self.source.update(events=self.rows, through=self.now)
        self.book.ingest(self.source, self.now)
        return row

    def buy(self, row, price='1'):
        q = quote(row['mint'], self.now, price)
        return self.book.buy(row['id'], q, self.now, evidence(self.book, row, q, self.now))

    def test_three_fixed_tickets_across_leaders_and_fourth_rejected(self):
        for id, leader in [('a1',self.a),('a2',self.a),('b1',self.b)]:
            self.buy(self.add(id, leader))
        row = self.add('b2', self.b)
        with self.assertRaisesRegex(ValueError, 'cap'):
            self.buy(row)
        self.book.skip(row['id'], 'cap', self.now)
        self.assertEqual(self.book.get('turnover'), '30')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM fills').fetchone()[0], 3)
        self.assertEqual(D(self.book.db.execute('SELECT quantity FROM inventory WHERE leader=? AND mint=?',(self.b,self.mint)).fetchone()[0]), 200)

    def test_partial_sell_only_affects_originating_leader_and_skipped_buys_count(self):
        self.buy(self.add('a1')); self.buy(self.add('a2')); self.buy(self.add('b1',self.b))
        skipped=self.add('b2',self.b); self.book.skip(skipped['id'],'cap',self.now)
        before_a=D(self.book.position(self.a,self.mint)['quantity'])
        before_b=D(self.book.position(self.b,self.mint)['quantity'])
        sell=self.add('b-half',self.b,'sell','100')
        self.book.sell(sell['id'],quote(self.mint,self.now,'2'),self.now)
        self.assertEqual(D(self.book.position(self.a,self.mint)['quantity']),before_a)
        self.assertEqual(D(self.book.position(self.b,self.mint)['quantity']),before_b/2)
        self.assertEqual(self.book.db.execute('SELECT spent FROM episodes').fetchone()[0],'30')

    def test_cap_resets_only_after_entire_mint_episode_closes(self):
        self.buy(self.add('a1')); self.buy(self.add('a2')); self.buy(self.add('b1',self.b))
        self.book.sell(self.add('a-out',side='sell',qty='200')['id'],quote(self.mint,self.now),self.now)
        self.assertEqual(self.book.db.execute('SELECT spent FROM episodes').fetchone()[0],'30')
        self.book.sell(self.add('b-out',self.b,'sell','100')['id'],quote(self.mint,self.now),self.now)
        self.assertEqual(self.book.db.execute('SELECT spent FROM episodes').fetchone()[0],'0')
        self.buy(self.add('a-new'))
        self.assertEqual(self.book.get('turnover'),'40')

    def test_duplicate_and_overlapping_pages_survive_restart(self):
        row=self.add('buy'); self.buy(row)
        self.source['events']=[row,row]
        self.book.ingest(self.source,self.now)
        other=Book(self.root,self.cfg,demo=True)
        try:
            other.ingest(self.source,self.now)
            with self.assertRaises(ValueError):other.buy(row['id'],quote(self.mint,self.now),self.now,{})
            self.assertEqual(other.db.execute('SELECT count(*) FROM fills').fetchone()[0],1)
        finally:other.close()

    def test_conflicting_duplicate_is_rejected(self):
        row=self.add('buy'); self.buy(row)
        self.source['events']=[dict(row,quantity='999')]
        with self.assertRaises(ValueError):self.book.ingest(self.source,self.now)
        self.assertEqual(self.book.get('turnover'),'10')

    def test_unknown_opening_and_sequence_gaps_never_create_sell_fraction(self):
        self.source['openings'][self.a]['verified']=False
        with self.assertRaises(SourceUnavailable):self.book.ingest(self.source,self.now)
        self.source['openings'][self.a]['verified']=True
        self.source['events']=[event('gap',self.a,2,self.now-5,'sell','100')]
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.book.ingest(self.source,self.now)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM events').fetchone()[0],0)

    def test_opening_inventory_changes_sell_proportion(self):
        self.source['openings'][self.a]['balances']={self.mint:'300'}
        row=self.add('buy');self.buy(row)
        before=D(self.book.position(self.a,self.mint)['quantity'])
        sell=self.add('sell',side='sell',qty='100')
        self.book.sell(sell['id'],quote(self.mint,self.now),self.now)
        self.assertEqual(D(self.book.position(self.a,self.mint)['quantity']),before*D('.75'))

    def test_oversell_is_not_assumed_full_exit(self):
        self.buy(self.add('buy'))
        with self.assertRaisesRegex(ValueError,'Unknown leader inventory'):self.add('sell',side='sell',qty='200')
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM fills WHERE side='sell'").fetchone()[0],0)

    def test_dropped_expired_and_model_disabled_do_not_block_exit(self):
        self.buy(self.add('buy'))
        self.book.publish_shortlist([],self.now,'drop')
        self.book.db.execute('UPDATE shortlists SET expires_at=?',(self.now,))
        self.book.set('model_gate',{'enabled':False})
        row=self.add('sell',side='sell',qty='50')
        self.book.sell(row['id'],quote(self.mint,self.now),self.now)
        self.assertGreater(D(self.book.position(self.a,self.mint)['quantity']),0)
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM fills WHERE side='sell'").fetchone()[0],1)

    def test_missing_or_stale_quote_keeps_exit_pending(self):
        self.buy(self.add('buy')); sell=self.add('sell',side='sell',qty='100')
        for q in ({},quote(self.mint,self.now-11)):
            with self.assertRaises((ValueError,KeyError)):self.book.sell(sell['id'],q,self.now)
        self.assertEqual(self.book.db.execute('SELECT state FROM events WHERE id=?',(sell['id'],)).fetchone()[0],'EXIT_PENDING')
        s=snapshot(self.book.db,self.now+100)
        self.assertIsNone(s['summary']['equity'])
        self.assertEqual(s['pending_exits'],1)

    def test_cash_costs_and_realized_pnl_reconcile(self):
        self.buy(self.add('buy'))
        sell=self.add('sell',side='sell',qty='100')
        self.book.sell(sell['id'],quote(self.mint,self.now,'2'),self.now)
        s=snapshot(self.book.db,self.now)['summary']
        self.assertEqual(D(s['equity']),D(s['starting_cash'])+D(s['realized_pnl']))
        self.assertEqual(D(s['cash'])+D(s['rent_reserved']),D(s['equity']))
        self.assertGreater(D(s['network_costs']),0)
        self.assertGreater(D(s['embedded_friction']),0)

    def test_model_costs_reduce_available_cash_and_equity(self):
        self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',('cost',encode({'usage':{'billed_usd':'5'}})))
        self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',('unknown',encode({'reservation_usd':'2'})))
        s=snapshot(self.book.db,self.now)['summary']
        self.assertEqual(s['cash'],'993');self.assertEqual(s['equity'],'993')
        self.assertEqual(self.book.available_cash(),D(993));self.assertIsNone(s['net_account_return'])

    def test_cash_and_exposure_are_shared_across_mints(self):
        self.book.set('cash','60')
        with self.assertRaisesRegex(ValueError,'cash'):self.buy(self.add('buy'))
        self.book.set('cash','1000');self.book.set('rent_reserved','295')
        with self.assertRaisesRegex(ValueError,'exposure'):self.buy(self.rows[0])

    def test_pre_activation_and_expired_signals_are_not_copied(self):
        row=self.add('old',at=self.now-95)
        with self.assertRaisesRegex(ValueError,'Pre-activation'):self.buy(row)
        self.book.skip('old','old',self.now)
        row=self.add('expired',at=self.now-61)
        with self.assertRaisesRegex(ValueError,'expiry'):self.buy(row)

    def test_leader_sell_during_research_cancels_buy(self):
        row=self.add('buy')
        self.add('sell',side='sell',qty='100')
        with self.assertRaisesRegex(ValueError,'reduced'):self.buy(row)

    def test_unsupported_route_is_visible_and_does_not_fill(self):
        row=event('bad-route',self.a,1,self.now-5);row['route']='unknown-router'
        self.source['events']=[row];self.book.ingest(self.source,self.now)
        with self.assertRaisesRegex(ValueError,'Unsupported'):self.buy(row)

    def test_stale_or_unavailable_source_blocks_entries(self):
        row=self.add('buy');self.book.set('source',{'state':'UNAVAILABLE'})
        with self.assertRaisesRegex(ValueError,'Source unavailable'):self.buy(row)

    def test_copied_foreign_ledger_and_runtime_symlink_refused(self):
        with tempfile.TemporaryDirectory() as other:
            root=Path(other);folder=root/'radon_bot/runtime';folder.mkdir(parents=True)
            with sqlite3.connect(folder/'book.sqlite3') as db:
                db.execute('CREATE TABLE meta(key TEXT,value TEXT)');db.execute('INSERT INTO meta VALUES(?,?)',('bot_id','"helium"'))
            (folder/'account.json').write_text('{"bot_id":"helium"}')
            before=(folder/'book.sqlite3').read_bytes()
            with self.assertRaises(ValueError):Book(root,self.cfg,initialize=True)
            self.assertEqual(before,(folder/'book.sqlite3').read_bytes())
        if hasattr(Path,'symlink_to'):
            with tempfile.TemporaryDirectory() as other:
                root=Path(other);(root/'radon_bot').mkdir();(root/'radon_bot/runtime').symlink_to(self.book.folder,target_is_directory=True)
                with self.assertRaisesRegex(ValueError,'redirect'):Book(root,self.cfg)

    def test_configuration_binding_prevents_reinterpretation(self):
        changed=dict(self.cfg,ticket_usd='20')
        with self.assertRaisesRegex(ValueError,'binding'):Book(self.root,changed,demo=True)
        self.assertEqual(bound_config(self.root),self.cfg)

    def test_synthetic_export_cannot_enter_production_account(self):
        with self.assertRaisesRegex(ValueError,'Synthetic'):validate_bundle(self.source)
        self.assertEqual(snapshot(self.book.db,self.now)['evidence_label'],'synthetic')

    def test_paid_gate_does_not_read_key_when_disabled(self):
        (self.book.folder/'paid-ai.json').write_text('{"enabled":false,"operator_authorized":false}')
        model=Model(self.root,self.book)
        with patch.object(model,'key',side_effect=AssertionError('must not read key')):
            self.assertFalse(model.gate()['enabled'])

    def test_other_account_paid_authorization_is_not_inherited(self):
        (self.book.folder/'paid-ai.json').write_text(encode({'enabled':True,'operator_authorized':True,'approval_reference':'other','binding':{'account_id':'helium:other'}}))
        model=Model(self.root,self.book,clock=lambda:self.now)
        with patch('rug_desk.model_gate.status',return_value={'enabled':True}),patch.object(model,'key',return_value='fixture'):
            self.assertFalse(model.gate()['enabled'])

    def test_status_is_read_only_and_does_not_initialize(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(view(root)['initialized']);self.assertEqual(list(Path(root).iterdir()),[])

    def test_watch_continues_pending_exits_when_feed_is_missing(self):
        self.buy(self.add('buy'));self.add('sell',side='sell',qty='100')
        class Provider:
            def quote(inner,mint):return quote(mint,self.now)
        errors=service.watch(self.book,Provider(),clock=lambda:self.now)
        self.assertTrue(errors)
        self.assertEqual(D(self.book.position(self.a,self.mint)['quantity']),0)

    def test_entry_loop_model_outage_does_not_fill(self):
        self.add('buy');(self.book.folder/'source.json').write_text(encode(self.source))
        book=self.book;now=self.now
        class Provider:
            def quote(self,mint,pool=None):return quote(mint,now)
            def evidence(self,mint,q):
                result=evidence(book,{'id':'unused'},q,now);result.pop('x');return result
        class Disabled:
            def gate(self):return {'enabled':False,'blockers':['test outage']}
            def ask(self,*args):raise AssertionError('model must not be called')
        self.assertEqual(service.entries(book,Provider(),Disabled(),clock=lambda:now),[])
        self.assertEqual(book.db.execute('SELECT count(*) FROM fills').fetchone()[0],0)

    def test_policy_and_signal_are_rechecked_after_model_completion(self):
        for change in ('policy','sell','expired'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'radon_bot').mkdir();(root/'radon_bot/config.json').write_text(encode(self.cfg))
                clock=[self.now]
                book=Book(root,self.cfg,initialize=True,demo=True,clock=lambda:clock[0])
                try:
                    row=event('buy',self.a,1,clock[0]-5)
                    feed=bundle(clock[0],[row]);(book.folder/'source.json').write_text(encode(feed))
                    book.publish_shortlist([self.a],clock[0]-20,'fixture')
                    class Provider:
                        def quote(inner,mint,pool=None):return quote(mint,clock[0])
                        def evidence(inner,mint,q):
                            result=evidence(book,row,q,clock[0]);result.pop('x');return result
                    class Researcher:
                        def gate(inner):return {'enabled':True}
                        def ask(inner,event_id,base):
                            result=evidence(book,row,base['quote'],clock[0])['x']
                            if change=='policy':
                                (root/'radon_bot/config.json').write_text(encode(dict(self.cfg,ticket_usd='20')))
                            elif change=='sell':
                                feed['events'].append(event('exit',self.a,2,clock[0],'sell','100'))
                            else:
                                clock[0]+=61
                            feed['through']=clock[0];(book.folder/'source.json').write_text(encode(feed))
                            return result
                    service.entries(book,Provider(),Researcher(),clock=lambda:clock[0])
                    self.assertEqual(book.db.execute('SELECT count(*) FROM fills').fetchone()[0],0)
                    self.assertEqual(book.db.execute("SELECT state FROM events WHERE id='buy'").fetchone()[0],'SKIPPED')
                finally:book.close()

    def test_concurrent_buy_attempts_cannot_exceed_shared_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        self.buy(self.add('a1'));self.buy(self.add('a2'))
        rows=[self.add('b1',self.b),self.add('b2',self.b)]
        for row in rows:evidence(self.book,row,quote(self.mint,self.now),self.now)
        def attempt(row):
            book=Book(self.root,self.cfg,demo=True)
            try:
                raw=book.db.execute('SELECT payload FROM api_requests WHERE id=?',('synthetic:'+row['id'],)).fetchone()[0]
                q=quote(self.mint,self.now)
                base={'quote':q,'facts':{'mint':self.mint,'observed_at':self.now,'mint_disabled':True,'freeze_disabled':True,
                    'complete_owner_coverage':True,'max_single_holder':'0.01','top10_share':'0.10'},
                    'lp':{'mint':self.mint,'pool':q['pool'],'observed_at':self.now,'state':'PASS'},'x':json.loads(raw)['result']}
                book.buy(row['id'],q,self.now,base)
                return True
            except ValueError:return False
            finally:book.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(attempt,rows))
        self.assertEqual(results.count(True),1);self.assertEqual(self.book.get('turnover'),'30')

    def test_read_only_view_accounts_for_partial_exit_basis(self):
        self.buy(self.add('buy'));before=D(self.book.position(self.a,self.mint)['basis'])
        row=self.add('half',side='sell',qty='50');self.book.sell(row['id'],quote(self.mint,self.now),self.now)
        s=view(self.root,self.now)
        self.assertEqual(D(s['positions'][0]['basis']),before/2)
        self.assertIn('historical', ' '.join(s['limits']).lower())


class RadonReplayTests(unittest.TestCase):
    def fixture(self):
        now=1800000000;a,b,mint=address(1),address(2),address(20)
        value=bundle(now)
        value['opening_at']=now-200
        value['events']=[event('a1',a,1,now-190),event('a2',a,2,now-180),event('b1',b,1,now-170),event('b2',b,2,now-160),
            event('a-out',a,3,now-60,'sell','200'),event('b-out',b,3,now-50,'sell','200')]
        value['quotes']=[quote(mint,t,'2' if t>=now-60 else '1') for t in sorted({r['at']+5 for r in value['events']}|{now})]
        return value,load(ROOT/'radon_bot')

    def test_combined_portfolio_uses_shared_cap_not_sum_of_traders(self):
        value,cfg=self.fixture()
        combined=replay(value,cfg)
        separate=[replay(value,cfg,[leader]) for leader in value['openings']]
        self.assertEqual(combined['summary']['buying_turnover'],'30')
        self.assertEqual(sum(D(r['summary']['buying_turnover']) for r in separate),D(40))
        self.assertEqual(combined['buy_fills'],3)
        self.assertEqual(combined['pending_exits'],0)

    def test_delayed_archived_quotes_required_no_historical_leader_fill(self):
        value,cfg=self.fixture();value['quotes']=[]
        result=replay(value,cfg)
        self.assertEqual(result['buy_fills'],0)
        self.assertIn('historical_conditional',result['kind'])

    def test_in_sample_research_and_kol_identity_are_explicit(self):
        value,cfg=self.fixture();cfg=dict(cfg,min_history_buys=1)
        leader=address(1);value['openings'][leader]['is_kol']=True
        result=research(value,cfg)
        row=next(r for r in result['ranking'] if r['leader']==leader)
        self.assertFalse(row['eligible']);self.assertEqual(row['kol_identity'],'unknown_or_mismatch')
        self.assertEqual(result['kind'],'historical_conditional_research')

    def test_same_policy_replay_and_ledger_fill_accounting(self):
        value,cfg=self.fixture();a=address(1)
        result=replay(value,cfg,[a])
        with tempfile.TemporaryDirectory() as tmp:
            book=Book(tmp,cfg,initialize=True,demo=True,clock=lambda:value['opening_at'])
            try:
                book.publish_shortlist([a],value['opening_at'],'test')
                subset=copy.deepcopy(value);subset['events']=[]
                for row in [r for r in value['events'] if r['leader']==a]:
                    subset['events'].append(row);subset['through']=row['at']+5
                    book.ingest(subset,subset['through'])
                    q=next(q for q in value['quotes'] if q['observed_at']==subset['through'])
                    if row['side']=='buy':book.buy(row['id'],q,subset['through'],{},replay=True)
                    else:book.sell(row['id'],q,subset['through'])
                actual=snapshot(book.db,value['through'])['summary']
                for key in ('cash','equity','buying_turnover','realized_pnl','network_costs','rent_reserved'):
                    self.assertEqual(actual[key],result['summary'][key],key)
            finally:book.close()


class RadonIntegrationTests(unittest.TestCase):
    def test_cli_init_disabled_paid_and_no_other_accounts(self):
        import radon_bot
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);(root/'radon_bot').mkdir();shutil.copy(ROOT/'radon_bot/config.json',root/'radon_bot/config.json')
            self.assertEqual(radon_bot.main(['init','--root',tmp]),0)
            marker=(root/'radon_bot/runtime/account.json').read_bytes()
            self.assertEqual(radon_bot.main(['init','--root',tmp]),0)
            self.assertEqual(marker,(root/'radon_bot/runtime/account.json').read_bytes())
            self.assertFalse(json.loads((root/'radon_bot/runtime/paid-ai.json').read_text())['enabled'])
            self.assertEqual({p.name for p in root.iterdir()},{'radon_bot'})

    def test_live_mode_rejected_before_files(self):
        import radon_bot
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):radon_bot.main(['init','--root',tmp,'--mode','live'])
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_slot_seven_reserved_console_and_workers_are_additive(self):
        import desktop
        html=(ROOT/'console/index.html').read_text()
        self.assertIn('Slot 7 reserved',html);self.assertIn('data-slot="8"',html)
        self.assertNotIn('data-bot="reserved"',html)
        for job in ('research','entries','watch','report'):self.assertIn('radon-'+job,desktop.JOBS)
        self.assertEqual(desktop.JOBS['neon-watch'][3],['neon_bot.py','watch'])
        self.assertEqual(desktop.JOBS['helium-watch'][3],['helium_bot.py','watch'])

    def test_uninitialized_desktop_workers_do_not_create_account(self):
        import desktop
        with tempfile.TemporaryDirectory() as tmp,patch.object(desktop,'child') as child:
            for job in ('radon-research','radon-entries','radon-watch','radon-report'):self.assertEqual(desktop.worker(job,Path(tmp)),0)
            child.assert_not_called();self.assertFalse((Path(tmp)/'radon_bot').exists())

    def test_packaging_includes_public_radon_excludes_private_inputs(self):
        spec=importlib.util.spec_from_file_location('build_radon',ROOT/'packaging/build_windows.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('radon_bot.py','radon_bot/config.json','radon_bot/README.md','radon_bot/runtime/source.json','radon_bot/runtime/paid-ai.json','radon_bot/.env'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('sentinel')
            self.assertEqual({p.relative_to(root).as_posix() for p in module.source_files(root)}, {'radon_bot.py','radon_bot/config.json','radon_bot/README.md'})


if __name__=='__main__':unittest.main()
