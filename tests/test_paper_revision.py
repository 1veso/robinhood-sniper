from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.adapters.evm import CHAIN_ID, ReorgError
from sniper.adapters.evm_store import EvmStore
from sniper.confirmation_book import ConfirmationBook
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger
from sniper.models import SafetyEvidence, WalletConfirmation
from sniper.rug_screen import enhance_rug_screen
from test_exploration import worker, exploratory_candidate
from test_engine import NOW, quote
from test_evm import TOKEN, POOL, WALLET, OTHER, TX_SWAP, bh


def proof(wallet=WALLET, index=1, now=NOW, pool=POOL, token=TOKEN, tx=None):
    return {'wallet':wallet, 'tx_hash':tx or '0x'+f'{index:064x}', 'log_index':index,
            'block_number':100+index, 'block_hash':bh(100+index), 'token_address':token,
            'pool':pool, 'spent_usd':'100', 'observed_at':now, 'verified_swap':True, 'factory_verified':True}


def persist(store, p):
    event={**p, 'kind':'sampled_swap'}
    cursor=store.cursor(CHAIN_ID)
    number=max(p['block_number'], cursor['block_number'] if cursor else 0)
    store.commit_chunk(CHAIN_ID,[event],[{'number':p['block_number'],'hash':p['block_hash']}],
        {'number':number,'hash':bh(number)},expected_cursor=cursor)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'events.sqlite3'
        self.store=EvmStore(self.path)
        self.book=ConfirmationBook(self.store)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def add(self, **kw):
        p=proof(**kw); persist(self.store,p); self.book.record(str(kw),p); return p

    def recent(self):
        return self.book.recent(TOKEN,POOL,now=NOW,age=900,minimum_spend=D(10))

    def test_two_distinct_buyers_survive_restart_but_reorged_buy_does_not_count(self):
        self.add(wallet=WALLET,index=1)
        self.add(wallet=OTHER,index=2)
        self.store.close(); self.store=EvmStore(self.path); self.book=ConfirmationBook(self.store)
        self.assertEqual(len(self.recent()),2)
        self.store.rewind(CHAIN_ID,{'number':101,'hash':bh(101)},NOW)
        self.assertEqual(len(self.recent()),1)

    def test_duplicates_expired_future_other_pool_and_same_transaction_do_not_form_quorum(self):
        self.add(wallet=WALLET,index=1)
        self.add(wallet=WALLET.upper().replace('0X','0x'),index=2,tx='0x'+f'{1:064x}')
        self.add(wallet=OTHER,index=3,now=NOW-901)
        self.add(wallet=OTHER,index=4,now=NOW+1)
        self.add(wallet=OTHER,index=5,pool=OTHER)
        self.add(wallet=OTHER,index=6,tx='0x'+f'{1:064x}')
        self.assertEqual(len(self.recent()),1)

    def test_proof_without_source_event_or_verification_is_rejected(self):
        with self.assertRaises(ValueError): self.book.record('missing',proof())
        p=proof(); persist(self.store,p)
        with self.assertRaises(ValueError): self.book.record('unverified',{**p,'factory_verified':False})

    def test_valid_distinct_transaction_assignment_is_not_lost_to_greedy_deduplication(self):
        self.add(wallet=WALLET,index=1,now=NOW-1)
        self.add(wallet=WALLET,index=2)
        self.add(wallet=OTHER,index=3,now=NOW-1,tx='0x'+f'{2:064x}')
        selected=self.recent()
        self.assertEqual(len({p['wallet'] for p in selected}),2)
        self.assertEqual(len({p['tx_hash'] for p in selected}),2)

    def test_canonical_recheck_rejects_changed_confirmation(self):
        p=self.add()
        class RPC:
            def call(self,*a): return {'number':hex(p['block_number']),'hash':'0x'+'ff'*32}
        with self.assertRaises(ReorgError): self.book.confirm([p],RPC())


class RevisionTests(unittest.TestCase):
    def test_thirty_thousand_boundary_and_two_wallet_entry_preserve_full_loss_limit(self):
        policy,_,_=worker.profile_settings('two-wallet-30k')
        ledger=Ledger(':memory:',D(1000)); engine=ExploratoryEngine(ledger,policy)
        try:
            one=exploratory_candidate()
            one=replace(one,safety=replace(one.safety,checks={**one.safety.checks,'rug_screen_available':True}))
            self.assertEqual(engine.consider(one,quote(liquidity_usd=D(30000)),NOW)['reason'],'insufficient_observed_wallets')
            two=replace(one,event_id='two',confirmations=[one.confirmations[0],replace(one.confirmations[0],wallet='wallet-other',cluster_id='other',tx_hash='tx-other')])
            self.assertEqual(engine.consider(two,quote(liquidity_usd=D(29999)),NOW)['reason'],'insufficient_liquidity')
            result=engine.consider(replace(two,event_id='three'),quote(liquidity_usd=D(30000)),NOW)
            self.assertEqual(result['verdict'],'PAPER_BUY')
            self.assertEqual(result['observed_wallets'],2)
            self.assertLessEqual(result['full_loss_risk_usd'],D(5))
        finally: ledger.close()

    def test_activation_preserves_identity_history_and_deadline(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t); old=runtime/'exploration'; old.mkdir()
            policy,_,_=worker.profile_settings('legacy')
            l,s,e,m=worker.open_trial(old,policy,start=True,now=NOW); l.close();s.close()
            with patch.object(worker.time,'time',return_value=NOW+10): selected=worker.activate_v2(runtime)
            self.assertEqual(selected['account_id'],m['account_id'])
            self.assertEqual(worker.active_profile(runtime),'two-wallet-30k')
            self.assertEqual(json.loads((old/'trial-before-v2.json').read_text()),m)
            self.assertEqual(json.loads((old/'trial.json').read_text())['ends_at'],m['ends_at'])
            with patch.object(worker.time,'time',return_value=NOW+20): selected2=worker.activate_v2(runtime)
            self.assertEqual(selected,selected2)
            (runtime/'active-exploration.json').unlink()
            with self.assertRaisesRegex(ValueError,'selector missing'): worker.active_profile(runtime)

    def test_revision_preserves_open_holdings_cash_and_original_exit_rules(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t); old=runtime/'exploration';old.mkdir()
            policy,_,_=worker.profile_settings('legacy')
            l,s,e,m=worker.open_trial(old,policy,start=True,now=NOW)
            e.consider(exploratory_candidate(),quote(),NOW)
            before=l.snapshot();l.close();s.close()
            worker.activate_v2(runtime)
            policy,rules,_=worker.profile_settings('two-wallet-30k')
            l,s,e,m=worker.open_trial(old,policy,rules=rules)
            try:
                self.assertEqual(l.snapshot(),before)
                result=e.watch({before['positions'][0]['asset_id']:quote(now=NOW+1,price='.5')},NOW+1)
                self.assertEqual(result[0]['reason'],'stop_loss')
                self.assertEqual(l.snapshot()['open_positions'],0)
            finally:l.close();s.close()

    def test_activation_recovers_after_database_commit_without_duplicate_revision_or_cash_reset(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t);folder=runtime/'exploration';folder.mkdir()
            policy,_,_=worker.profile_settings('legacy')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW)
            e.consider(exploratory_candidate(),quote(),NOW);cash=l.snapshot()['cash'];l.close();s.close()
            atomic=worker.atomic_json
            def interrupted(path,value):
                if path.name=='trial.json':raise OSError('fixture crash after SQLite commit')
                return atomic(path,value)
            with patch.object(worker,'atomic_json',side_effect=interrupted),self.assertRaises(OSError):worker.activate_v2(runtime)
            worker.activate_v2(runtime)
            policy,rules,_=worker.profile_settings('two-wallet-30k')
            l,s,e,m=worker.open_trial(folder,policy,rules=rules)
            try:
                self.assertEqual(l.snapshot()['cash'],cash)
                self.assertEqual(l.snapshot()['open_positions'],1)
                self.assertEqual(l.conn.execute("SELECT COUNT(*) FROM events WHERE kind='policy_revision'").fetchone()[0],1)
            finally:l.close();s.close()

    def test_entry_revision_cannot_silently_change_loss_or_exit_settings(self):
        with tempfile.TemporaryDirectory() as t:
            runtime=Path(t);folder=runtime/'exploration';folder.mkdir()
            policy,_,_=worker.profile_settings('legacy')
            l,s,e,m=worker.open_trial(folder,policy,start=True,now=NOW);l.close();s.close()
            new,rules,name=worker.profile_settings('two-wallet-30k')
            with patch.object(worker,'profile_settings',return_value=(replace(new,stop_loss_pct=D(50)),rules,name)):
                with self.assertRaisesRegex(ValueError,'existing exits'):worker.activate_v2(runtime)
            self.assertEqual(json.loads((folder/'trial.json').read_text()),m)

    def test_missing_rug_provider_and_positive_scam_flags_block_but_unknown_stays_unknown(self):
        s=SafetyEvidence('token',NOW,{'liquidity_controls':None},'unavailable')
        self.assertFalse(enhance_rug_screen(s).checks['rug_screen_available'])
        s=replace(s,source='goplus:4663:token_security',details={'raw_report':{'is_airdrop_scam':'1'}})
        result=enhance_rug_screen(s)
        self.assertFalse(result.checks['rug:is_airdrop_scam'])
        self.assertIsNone(result.checks['liquidity_controls'])
        self.assertEqual(result.details['rug_screen']['status'],'REJECT')
        s=replace(s,details={'raw_report':{'is_airdrop_scam':'0','fake_token':{'value':'1'}}})
        self.assertFalse(enhance_rug_screen(s).checks['rug:fake_token'])
        s=replace(s,details={'raw_report':{'fake_token':{'value':1}}})
        self.assertFalse(enhance_rug_screen(s).checks['rug:fake_token'])
        s=replace(s,details={'raw_report':{'token_symbol':'TEST'}})
        self.assertEqual(enhance_rug_screen(s).details['rug_screen']['status'],'INCOMPLETE')

    def test_v2_cannot_bypass_missing_rug_screen_even_when_other_unknowns_are_allowed(self):
        policy,_,_=worker.profile_settings('two-wallet-30k')
        ledger=Ledger(':memory:',D(1000)); engine=ExploratoryEngine(ledger,policy)
        try:
            self.assertEqual(engine.consider(exploratory_candidate(),quote(),NOW)['reason'],'safety_unknown:rug_screen_available')
        finally: ledger.close()

    def test_fixed_size_limits_warn_while_modifiable_limits_and_actual_failures_reject(self):
        raw={'is_anti_whale':'1','anti_whale_modifiable':'0'}
        screen=SafetyEvidence('token',NOW,{'rug:is_anti_whale':False,'liquidity_controls':None},
            'goplus:4663:token_security',{'raw_report':raw})
        revised=enhance_rug_screen(screen)
        self.assertNotIn('rug:is_anti_whale',revised.checks)
        self.assertTrue(revised.details['rug_screen']['warnings'])
        self.assertEqual(revised.details['rug_screen']['status'],'INCOMPLETE')
        raw['anti_whale_modifiable']='1'
        self.assertFalse(enhance_rug_screen(screen).checks['rug:anti_whale_modifiable'])
        self.assertFalse(enhance_rug_screen(replace(screen,checks={'sellability':False})).checks['sellability'])


class WorkerQuorumTests(unittest.TestCase):
    def exercise(self, outage):
        from sniper.exploratory_source import ExploratorySource
        tick=[NOW]
        data=[proof(index=1),proof(wallet=OTHER,index=2,now=NOW+60),proof(wallet='0x'+'44'*20,index=3,now=NOW+120)]
        asset='eip155:4663/erc20:'+TOKEN
        class Source(ExploratorySource):
            def __init__(self,rpc,store,market):super().__init__(rpc,store,market,clock=lambda:tick[0])
            def collect(self, **kwargs):
                p=data[min(2,(tick[0]-NOW)//60)]
                persist(self.store,p)
                return {'sampled':1}
            def prove(self,event,*,fallback_rpc=None): return {k:v for k,v in event.items() if k not in ('kind','canonical')}
        class RPC:
            def __init__(self,*a,**kw):pass
            def budget(self,*a):
                from contextlib import nullcontext
                return nullcontext()
            def call(self,method,params):
                number=int(params[0],16)
                return {'number':hex(number),'hash':bh(number)}
        class Route:
            last_evidence={'scope':'offline fixture only'}
            def __init__(self,*a):pass
            def quote(self,*a,**kw):
                amount=D(kw['max_notional'])
                self.last_evidence={'scope':'offline fixture only','buy_input_usd':str(amount)}
                covered=max(amount,kw.get('sell_quantity',D(0)))
                return quote(asset=asset,now=tick[0],pool_id=POOL,liquidity_usd=D(30000),
                             route_max_notional_usd=covered,route_max_sell_quantity=covered,route_impact_bps=D(0))
        def screen(token):
            if outage and tick[0]==NOW+60:raise RuntimeError('fixture provider outage')
            return SafetyEvidence(asset,tick[0],{'contract_controls':True,'sellability':True,'holder_distribution':True,
                'liquidity_controls':None,'deployer_history':None},'goplus:4663:token_security',{'raw_report':{'is_airdrop_scam':'0'}})
        with tempfile.TemporaryDirectory() as t, patch.object(worker.time,'time',side_effect=lambda:tick[0]), \
             patch.object(worker,'ExploratorySource',Source), patch.object(worker,'RpcClient',RPC), \
             patch.object(worker,'V3RouteClient',Route), patch.object(worker.GoPlusClient,'screen',side_effect=screen):
            runtime=Path(t);folder=runtime/'exploration-v2';folder.mkdir()
            first=worker.run_cycle(folder,runtime,start=True,profile='two-wallet-30k')
            self.assertEqual(first['decisions'][0]['reason'],'waiting_for_distinct_buyers')
            self.assertEqual(first['fills'],[])
            tick[0]+=60
            second=worker.run_cycle(folder,runtime,profile='two-wallet-30k')
            if outage:
                self.assertEqual(second['fills'],[])
                self.assertEqual(second['decisions'],[])
                self.assertGreater(second['entry_work']['deferred'],0)
                tick[0]+=60
                second=worker.run_cycle(folder,runtime,profile='two-wallet-30k')
            self.assertEqual(len(second['fills']),1)
            self.assertEqual(second['decisions'][0]['observed_wallets'],2)
            self.assertEqual(second['health'],'OK')
            restarted=worker.run_cycle(folder,runtime,profile='two-wallet-30k')
            self.assertEqual(len(restarted['fills']),1)

    def test_two_buyers_across_worker_restarts_create_exactly_one_fill(self):self.exercise(False)

    def test_provider_outage_blocks_entry_without_poisoning_future_valid_screens(self):self.exercise(True)
