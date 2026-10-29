"""Isolated paper entry revision and route-failure recovery; no real providers."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch, Mock
from contextlib import ExitStack
import explore as worker
from sniper.adapters.evm import WETH, USDG, RpcError
from sniper.adapters.v3_route import RouteUnavailable
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger
from test_engine import candidate, quote, NOW
import test_live_evidence as route_fixtures
POOL=route_fixtures.POOL


def eligible(event='one', asset='eip155:4663:token1', now=NOW):
    c=candidate(event,asset,now,strategy=ExploratoryEngine.strategy_name)
    return replace(c,confirmations=c.confirmations[:2],safety=replace(c.safety,checks={**c.safety.checks,'rug_screen_available':True}))


class ReentryTests(unittest.TestCase):
    def setUp(self):
        self.policy,_,_=worker.profile_settings('two-wallet-30k-reentry')
        self.ledger=Ledger(':memory:',D(1000));self.engine=ExploratoryEngine(self.ledger,self.policy)
    def tearDown(self):self.ledger.close()
    def small_quote(self, asset='eip155:4663:token1',now=NOW,price='1'):
        return quote(asset,now,price,route_max_notional_usd=D(1),route_max_sell_quantity=D(2),route_impact_bps=D(0))
    def test_more_than_four_positions_and_six_entries_but_risk_still_binds(self):
        count=0
        for i in range(30):
            asset=f'eip155:4663:token{i}'
            result=self.engine.consider(eligible(str(i),asset),self.small_quote(asset),NOW)
            if result['verdict']=='PAPER_BUY':count+=1
            else:break
        self.assertGreater(count,6);self.assertLess(count,30)
        self.assertEqual(result['reason'],'risk_or_cash_budget_exhausted')
        snap=self.ledger.snapshot();self.assertGreater(snap['cash'],0)
        self.assertLessEqual(snap['stress_exposure'],snap['equity']*D('.02')+D('0.00000001'))
    def test_reentry_needs_post_close_quorum_and_no_duplicate_or_open_position(self):
        first=eligible();self.assertEqual(self.engine.consider(first,self.small_quote(),NOW)['verdict'],'PAPER_BUY')
        self.engine.watch({first.asset_id:self.small_quote(now=NOW+1,price='.5')},NOW+1)
        self.assertEqual(self.ledger.snapshot()['open_positions'],0)
        old=eligible('old',now=NOW+2)
        old=replace(old,confirmations=first.confirmations)
        self.assertEqual(self.engine.consider(old,self.small_quote(now=NOW+2),NOW+2)['reason'],'reentry_requires_new_buyers_after_close')
        one=eligible('one-fresh',now=NOW+3)
        one=replace(one,confirmations=[one.confirmations[0],first.confirmations[1]])
        self.assertEqual(self.engine.consider(one,self.small_quote(now=NOW+3),NOW+3)['reason'],'reentry_requires_new_buyers_after_close')
        fresh=eligible('fresh',now=NOW+4)
        self.assertEqual(self.engine.consider(fresh,self.small_quote(now=NOW+4),NOW+4)['verdict'],'PAPER_BUY')
        self.assertEqual(self.engine.consider(fresh,self.small_quote(now=NOW+4),NOW+4)['reason'],'duplicate_event')
        self.assertEqual(self.engine.consider(eligible('still-open',now=NOW+5),self.small_quote(now=NOW+5),NOW+5)['reason'],'already_open')
    def test_missing_safety_and_sell_only_quote_cannot_enter(self):
        c=eligible();c=replace(c,safety=replace(c.safety,checks={**c.safety.checks,'rug_screen_available':None}))
        self.assertEqual(self.engine.consider(c,self.small_quote(),NOW)['reason'],'safety_unknown:rug_screen_available')
        q=replace(self.small_quote(),route_side='sell')
        self.assertEqual(self.engine.consider(eligible('sell-only'),q,NOW)['reason'],'quote_not_entry_route')
        self.assertFalse(self.ledger.positions())
    def test_sell_only_quote_can_execute_original_exit(self):
        self.engine.consider(eligible(),self.small_quote(),NOW)
        q=replace(self.small_quote(now=NOW+1,price='.5'),route_side='sell')
        result=self.engine.watch({q.asset_id:q},NOW+1)
        self.assertEqual(result[0]['reason'],'stop_loss');self.assertFalse(self.ledger.positions())


class ReentryMigrationTests(unittest.TestCase):
    def test_crash_recovery_preserves_same_account_cash_holdings_deadline_and_history(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t);folder=runtime/'exploration';folder.mkdir()
            policy,rules,_=worker.profile_settings('two-wallet-30k')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW,rules=rules)
            e.consider(eligible(),quote(),NOW);before=l.snapshot();l.close();s.close()
            original=worker.atomic_json
            def fail_manifest(p,v):
                if p.name=='trial.json':raise OSError('fixture interrupted publication')
                original(p,v)
            with patch.object(worker,'atomic_json',side_effect=fail_manifest),self.assertRaises(OSError):worker.activate_reentry(runtime)
            worker.activate_reentry(runtime);worker.activate_reentry(runtime)
            policy,rules,_=worker.profile_settings('two-wallet-30k-reentry')
            l,s,e,after=worker.open_trial(folder,policy,rules=rules)
            try:
                self.assertEqual(before,l.snapshot());self.assertEqual(m['ends_at'],after['ends_at'])
                self.assertEqual(m['account_id'],after['account_id']);self.assertEqual(m['source_id'],after['source_id'])
                self.assertEqual(l.conn.execute("SELECT COUNT(*) FROM events WHERE kind='policy_revision'").fetchone()[0],1)
                self.assertEqual(worker.active_profile(runtime),'two-wallet-30k-reentry')
                result=e.watch({before['positions'][0]['asset_id']:quote(now=NOW+1,price='.5')},NOW+1)
                self.assertEqual(result[0]['reason'],'stop_loss')
            finally:l.close();s.close()
            (runtime/'active-exploration.json').unlink()
            with self.assertRaisesRegex(ValueError,'selector missing'):worker.active_profile(runtime)
    def test_monetary_or_exit_changes_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t);folder=runtime/'exploration';folder.mkdir()
            policy,rules,_=worker.profile_settings('two-wallet-30k')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW,rules=rules);l.close();s.close()
            target,rules,name=worker.profile_settings('two-wallet-30k-reentry')
            with patch.object(worker,'profile_settings',return_value=(replace(target,stop_loss_pct=D(10)),rules,name)):
                with self.assertRaisesRegex(ValueError,'monetary risk'):worker.activate_reentry(runtime)
            self.assertEqual(json.loads((folder/'trial.json').read_text()),m)


class ExitRouteTests(unittest.TestCase):
    def test_failed_buy_does_not_prevent_position_sized_sell(self):
        fixture=route_fixtures.RouteTests();fixture.setUp();client=fixture.client;original=client.rpc.transport
        def transport(req):
            if req['method']=='eth_call' and req['params'][0]['data'].startswith('0xc6a5026a'):
                src='0x'+req['params'][0]['data'][10:74][-40:]
                if src==USDG:return {'jsonrpc':'2.0','id':req['id'],'error':{'code':3,'message':'fixture revert'}}
            return original(req)
        client.rpc.transport=transport
        with self.assertRaises(RouteUnavailable) as failure:client.quote(WETH,POOL,max_notional=25)
        self.assertEqual(failure.exception.details['side'],'buy')
        q=client.quote(WETH,POOL,max_notional=25,sell_quantity=D('.05'),exit_only=True)
        self.assertEqual(q.route_side,'sell');self.assertEqual(q.route_max_sell_quantity,D('.05'))
        self.assertIsNone(client.last_evidence['buy_input_raw']);self.assertGreater(q.sell_cost_bps,100)
        self.assertEqual(fixture.swaps,[(WETH,50000000000000000)])
    def test_failed_sell_remains_unresolved_and_http_errors_not_mislabeled(self):
        fixture=route_fixtures.RouteTests();fixture.setUp();client=fixture.client
        with patch.object(client.rpc,'call',side_effect=RpcError('RPC HTTP failure (429); observation unavailable')):
            with self.assertRaises(RpcError) as failure:client.quote(WETH,POOL,max_notional=25,sell_quantity=D('.05'),exit_only=True)
        self.assertNotIsInstance(failure.exception,RouteUnavailable)
        fixture.fail_sell=True
        with self.assertRaises(RpcError):client.quote(WETH,POOL,max_notional=25,sell_quantity=D('.05'),exit_only=True)


class CycleIssueTests(unittest.TestCase):
    def test_collection_backoff_survives_restart_and_keeps_exit_quotes_running(self):
        tick=[NOW+60];source=Mock();source.collect.side_effect=[RpcError('RPC batch HTTP failure (429)'),{'sampled':0}]
        source.pending.return_value=[];route=Mock()
        route.quote.side_effect=lambda *a,**kw:quote(now=tick[0],route_side='sell',route_max_sell_quantity=kw['sell_quantity'],route_max_notional_usd=D(25),route_impact_bps=D(0))
        route.last_evidence={}
        with tempfile.TemporaryDirectory() as t,ExitStack() as patches:
            root=Path(t);folder=root/'exploration';folder.mkdir()
            policy,rules,_=worker.profile_settings('two-wallet-30k-reentry')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW,rules=rules)
            e.consider(eligible(),quote(),NOW);l.close();s.close()
            for obj,name,value in [(worker.time,'time',lambda:tick[0]),(worker,'ExploratorySource',lambda *a:source),
                    (worker,'V3RouteClient',lambda *a:route),(worker,'notify_fills',lambda *a:{}),(worker,'notify_market',lambda *a:{})]:
                patches.enter_context(patch.object(obj,name,value))
            a=worker.run_cycle(folder,root,profile='two-wallet-30k-reentry')
            self.assertEqual(a['source_retry_at'],NOW+120);self.assertEqual(a['quote_retry_at'],0)
            # Collection now runs before pending entry work; a collection 429
            # applies backoff before consuming or proving any selected event.
            self.assertEqual(len(a['errors']),1,a['errors']);source.pending.assert_not_called();source.finish.assert_not_called()
            tick[0]+=10;b=worker.run_cycle(folder,root,profile='two-wallet-30k-reentry')
            self.assertEqual(b['collection']['status'],'RATE_LIMIT_BACKOFF');self.assertEqual(b['health'],'DEGRADED')
            self.assertEqual(source.collect.call_count,1);self.assertEqual(route.quote.call_count,2)
            self.assertEqual(b['snapshot']['unresolved_positions'],0);self.assertEqual(len(b['fills']),1)
            tick[0]+=120;c=worker.run_cycle(folder,root,profile='two-wallet-30k-reentry')
            self.assertEqual(source.collect.call_count,2);self.assertEqual(c['source_retry_at'],0)
            self.assertEqual(c['errors'],[]);self.assertEqual(route.quote.call_count,3)

    def test_pending_rate_limits_preserve_candidate_and_bind_affected_provider(self):
        for stage in ('proof','quote'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as t,ExitStack() as patches:
                root=Path(t);folder=root/'exploration';folder.mkdir();source=Mock();route=Mock()
                source.collect.return_value={'sampled':0};source.pending.return_value=[{'id':1},{'id':2}]
                proof={'token_address':WETH,'pool':POOL,'spent_usd':'100','wallet':'fixture','tx_hash':'fixture','observed_at':NOW}
                source.prove.return_value=proof
                failure=RpcError('RPC batch HTTP failure (429)')
                if stage=='proof':source.prove.side_effect=failure
                route.quote.side_effect=failure
                policy,rules,_=worker.profile_settings('legacy')
                l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW,rules=rules);l.close();s.close()
                safety=eligible().safety
                for obj,name,value in [(worker.time,'time',lambda:NOW+60),(worker,'ExploratorySource',lambda *a:source),
                        (worker,'V3RouteClient',lambda *a:route),(worker,'notify_fills',lambda *a:{}),(worker,'notify_market',lambda *a:{}),
                        (worker,'signal_id',lambda swap:'fixture:'+str(swap['id'])),(worker,'GoPlusClient',lambda:Mock(screen=lambda token:safety)),
                        (worker,'pool_adjusted_screen',lambda s,*a:s),(worker,'observed_pool_screen',lambda s,*a:s),
                        (worker,'observe_creator_balance',lambda s,*a:s),(worker,'supplement_from_runtime',lambda s,*a:s)]:
                    patches.enter_context(patch.object(obj,name,value))
                result=worker.run_cycle(folder,root,profile='legacy')
                self.assertEqual(source.prove.call_count,1);source.finish.assert_not_called()
                self.assertEqual(result['fills'],[]);self.assertEqual(len(result['errors']),1)
                self.assertEqual(result['source_retry_at'],NOW+120 if stage=='proof' else 0)
                self.assertEqual(result['quote_retry_at'],NOW+120 if stage=='quote' else 0)
                self.assertEqual(route.quote.call_count,0 if stage=='proof' else 1)

    def test_repeated_watch_reuses_failure_and_retries_next_cycle_without_fake_sale(self):
        class Source:
            def __init__(self,*a):pass
            def collect(self,**kw):return {'sampled':0}
            def pending(self):return []
        calls=[];tick=[NOW+60]
        class Route:
            def __init__(self,*a):pass
            def quote(self,token,pool,**kw):
                calls.append(kw)
                raise RouteUnavailable(side='sell',token=token,pool=pool,block=100,block_hash='fixture',amount_raw=1)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);folder=root/'exploration';folder.mkdir()
            policy,rules,_=worker.profile_settings('two-wallet-30k-reentry')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW,rules=rules)
            e.consider(eligible(),quote(),NOW);before=l.positions()[0];l.close();s.close()
            with patch.object(worker.time,'time',side_effect=lambda:tick[0]),patch.object(worker,'ExploratorySource',Source),patch.object(worker,'V3RouteClient',Route),patch.object(worker,'notify_fills',return_value={}),patch.object(worker,'notify_market',return_value={}):
                a=worker.run_cycle(folder,root,profile='two-wallet-30k-reentry')
                self.assertEqual(len(calls),1);self.assertTrue(calls[0]['exit_only'])
                self.assertEqual(a['errors'],[]);self.assertEqual(a['cycle_issues'][0]['attempts'],1)
                self.assertEqual(a['health'],'DEGRADED');self.assertEqual(len(a['fills']),1)
                self.assertEqual(a['snapshot']['positions'][0]['qty'],str(before['qty']))
                tick[0]+=120;b=worker.run_cycle(folder,root,profile='two-wallet-30k-reentry')
                self.assertEqual(len(calls),2);self.assertEqual(len(b['fills']),1)
                self.assertEqual(b['snapshot']['unresolved_positions'],1)

if __name__=='__main__':unittest.main()
