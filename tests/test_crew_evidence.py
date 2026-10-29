"""Isolated factual producers: no provider, signer, model or strategy writes."""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from sniper.crew_evidence import (EvidenceStore, Producer, PIN_SCOPE, REVISION,
    digest, enrich, ReadOnlyRpc)
from sniper.adapters.solana_execution import MAINNET_GENESIS, USDC
from test_jev import fixture as dossier_fixture
from test_rug_desk import NOW, MINT, POOL, quote

IDENTITY={'account_id':'fixture-account','config_sha256':'fixture-config'}


class UnavailableRpc:
    def __init__(self):self.calls=[]
    def rpc(self,method,params):
        self.calls.append((method,params))
        raise TimeoutError('Synthetic unavailable RPC')


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=EvidenceStore(self.root)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def capture(self,at=NOW,**kwargs):
        class FixtureProducer(Producer):
            def _holders(self,mint):
                return {'status':'VALID','value':100,'observed_at':at,'received_at':at,
                    'source':'fixture:canonical_complete_holder_coverage','source_observation_time_known':True,
                    'independently_verified':True,'context':{'scope':'isolated fixture'},'controller_independence':'UNKNOWN'}
        producer=FixtureProducer(self.root,store=self.store,rpc=UnavailableRpc(),clock=lambda:at)
        return producer.capture(IDENTITY,MINT,POOL,quote=quote(now=at),raw_report={'mint':MINT,'totalHolders':100},**kwargs)
    def blank(self,at=NOW):
        d,b=dossier_fixture(at)
        for name in ('price_then','holders_now','holders_then','independent_buyers','largest_buyer_share_pct','exit_capacity_usd','exit_price_impact_pct','exit_quote_ticket_usd'):
            d['observations'][name]=None
        d.update(window={'start':None,'end':None},venue_phase='UNKNOWN',venue_phase_source_ref=None,independence_review=None,exit_route=None)
        return d
    def test_restart_exact_window_and_no_controller_inference(self):
        self.capture(NOW-3600);current=self.capture()
        self.store.close();self.store=EvidenceStore(self.root)
        result=enrich(self.blank(),self.store,now=NOW)
        self.assertEqual(result['window'],{'start':NOW-3600,'end':NOW})
        self.assertEqual(result['observations']['holders_then']['value'],100)
        self.assertIsNone(result['independence_review'])
        self.assertIsNone(result['observations']['independent_buyers'])
        self.assertEqual(result['venue_phase'],'UNKNOWN')
        records=self.store.records_for(result)
        self.assertEqual(len(records),2)
        self.assertTrue(all('rug_desk:crew:'+digest(x) in result['source_refs'] for x in records))
        self.assertEqual(current['payload']['independence']['status'],'UNKNOWN')
    def test_nearest_hour_is_not_retimestamped_or_backfilled(self):
        self.capture(NOW-3601);self.capture()
        result=enrich(self.blank(),self.store)
        self.assertEqual(result['window'],{'start':None,'end':None})
        self.assertIsNone(result['observations']['holders_then'])
    def test_future_received_evidence_cannot_enter_earlier_decision(self):
        row=self.capture()['payload'];row['received_at']=NOW+1
        self.store.db.execute('DELETE FROM evidence');self.store.db.commit()
        self.store.put(row)
        result=enrich(self.blank(),self.store,now=NOW)
        self.assertIsNone(result['observations']['holders_now'])
    def test_wrong_pool_and_account_cannot_be_mixed(self):
        self.capture()
        d=self.blank();d['account_id']='another-account'
        self.assertIsNone(enrich(d,self.store)['observations']['holders_now'])
        d=self.blank();d['pool']=MINT
        self.assertIsNone(enrich(d,self.store)['observations']['holders_now'])
    def test_hash_tampering_and_configuration_rebinding_fail(self):
        result=self.capture();body=deepcopy(result['payload']);body['config_sha256']='different'
        with self.assertRaises(ValueError):self.store.put(body)
        body=result['payload'];body['observations']['holders_now']['value']=999
        self.store.db.execute('UPDATE evidence SET payload=?',(json.dumps(body),));self.store.db.commit()
        with self.assertRaises(ValueError):enrich(self.blank(),self.store)
    def test_duplicate_capture_is_idempotent(self):
        a=self.capture();b=self.capture()
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],1)
    def test_missing_or_boolean_holder_count_stays_unknown(self):
        for bad in ({'mint':MINT},{'mint':MINT,'totalHolders':True},{'mint':POOL,'totalHolders':100}):
            producer=Producer(self.root,store=self.store,rpc=UnavailableRpc(),clock=lambda:NOW)
            record=producer.capture(IDENTITY,MINT,POOL,quote=quote(now=NOW),raw_report=bad)['payload']
            self.assertNotIn('holders_now',record['dossier_fields']['observations'])
            self.assertIn({'part':'holders','error_type':'ValueError'},record['errors'])
    def test_provider_total_is_retained_without_inventing_source_observation_time(self):
        producer=Producer(self.root,store=self.store,rpc=UnavailableRpc(),clock=lambda:NOW)
        row=producer.capture(IDENTITY,MINT,POOL,quote=quote(now=NOW),raw_report={'mint':MINT,'totalHolders':100})['payload']
        self.assertEqual(row['observations']['reported_total_holders']['value'],100)
        self.assertIsNone(row['observations']['reported_total_holders']['observed_at'])
        self.assertNotIn('holders_now',row['dossier_fields']['observations'])
        self.assertIsNone(enrich(self.blank(),self.store)['observations']['holders_now'])
    def test_stale_quote_does_not_get_current_observation_time(self):
        record=Producer(self.root,store=self.store,rpc=UnavailableRpc(),clock=lambda:NOW).capture(
            IDENTITY,MINT,POOL,quote=quote(now=NOW-31),raw_report={'mint':MINT,'totalHolders':100})['payload']
        self.assertNotIn('price_now',record['dossier_fields']['observations'])
    def test_budget_exhaustion_retains_partial_record_without_extra_calls(self):
        class Clock:
            now=NOW
            def __call__(self):return self.now
        clock=Clock()
        class SlowRugcheck:
            def raw_report(self,mint):clock.now+=11;return {'mint':mint,'totalHolders':100}
        rpc=UnavailableRpc()
        producer=Producer(self.root,store=self.store,rpc=rpc,rugcheck_client=SlowRugcheck(),clock=clock,budget_seconds=10)
        row=producer.capture(IDENTITY,MINT,POOL,quote=quote(now=NOW))['payload']
        self.assertIn('price_now',row['dossier_fields']['observations'])
        self.assertNotIn('holders_now',row['dossier_fields']['observations'])
        self.assertEqual(rpc.calls,[])
        self.assertGreater(row['received_at'],row['dossier_fields']['observations']['price_now']['observed_at'])
    def test_no_broadcast_or_model_interface(self):
        self.assertNotIn('sendTransaction',ReadOnlyRpc.ALLOWED_RPC)
        self.assertFalse(hasattr(Producer,'sign'));self.assertFalse(hasattr(Producer,'broadcast'))


@unittest.skipUnless(importlib.util.find_spec('solders'),'Optional CP structural PDA dependency unavailable')
class CpEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(self.root)
        self.addCleanup(self.store.close)
        from test_solana_executor import fixture, FakeRpc, MINT as CMINT, NOW as CNOW
        self.pool,self.accounts,self.policy=fixture();self.mint=CMINT;self.at=CNOW
        class Rpc(FakeRpc):
            def rpc(inner,method,params=None):
                if method=='getGenesisHash':inner.calls.append((method,params));return MAINNET_GENESIS
                if method=='getBlock':return {'blockhash':'canonical-fixture','blockTime':inner.now}
                return super().rpc(method,params)
        self.rpc=Rpc(self.pool,self.accounts)
        self.review={'scope':PIN_SCOPE,**IDENTITY,'review_reference':'fixture:independent-review',
            'reviewed_at':self.at-1,'expires_at':self.at+100,'program_pins':self.policy['program_pins']}
        class UsdMarket:
            def quote(inner,mint,now):
                if mint!=USDC:raise AssertionError('Unexpected valuation')
                class Quote:price_usd=Decimal(1);observed_at=now
                return Quote()
        self.usd_market=UsdMarket()
    def cp_capture(self,review=True,**kwargs):
        producer=Producer(self.root,store=self.store,rpc=self.rpc,usd_market=self.usd_market,
            clock=lambda:self.at,program_review=self.review if review else None,**kwargs)
        q=quote(now=self.at);q.update(mint=self.mint,pool=self.pool)
        return producer.capture(IDENTITY,self.mint,self.pool,quote=q,raw_report={'mint':self.mint,'totalHolders':100})['payload']
    def test_supported_exact_ticket_cp_quote_is_bound_and_not_wallet_simulation(self):
        row=self.cp_capture()
        self.assertEqual(row['venue']['status'],'VALID')
        self.assertEqual(row['exit']['status'],'VALID')
        self.assertEqual(row['exit']['ticket_usd'],'25')
        self.assertGreater(Decimal(row['exit']['exit_capacity_usd']),25)
        self.assertGreaterEqual(int(row['exit']['sell_quantity_raw']),int(row['exit']['paper_quantity_upper_bound_raw']))
        self.assertGreater(int(row['exit']['sell_quantity_raw']),int(row['exit']['buy_quantity_raw']))
        self.assertFalse(row['exit']['wallet_simulated'])
        self.assertFalse(row['exit']['network_costs_included'])
        self.assertLessEqual(row['rpc_calls'],20)
        self.assertEqual(row['exit']['program_review'],self.review)
        self.assertEqual(row['exit']['review_sha256'],digest(row['exit']['program_review']))
        self.assertEqual(row['venue']['program_review'],row['exit']['program_review'])
    def test_program_review_expiry_and_review_after_canonical_time_fail_closed(self):
        self.review['expires_at']=self.at
        self.assertEqual(self.cp_capture()['exit']['status'],'UNKNOWN')
        self.review['expires_at']=self.at+100
        self.review['reviewed_at']=self.at+1;self.at+=1
        self.assertEqual(self.cp_capture()['exit']['status'],'UNKNOWN')
    def test_only_allowlisted_review_contract_is_published(self):
        self.review['unrelated_field']='not part of review contract'
        row=self.cp_capture()
        self.assertEqual(row['exit']['status'],'VALID')
        self.assertNotIn('unrelated_field',row['exit']['program_review'])
    def test_missing_or_changed_review_pins_remain_unknown(self):
        self.assertEqual(self.cp_capture(review=False)['exit']['status'],'UNKNOWN')
        self.review['program_pins']={}
        self.assertEqual(self.cp_capture()['exit']['status'],'UNKNOWN')
    def test_wrong_mint_owner_fails_closed(self):
        from sniper import solana_cpmm as cp
        self.accounts[self.mint]['owner']=cp.SYSTEM
        row=self.cp_capture();self.assertEqual(row['exit']['status'],'UNKNOWN')
    def test_route_failing_at_intended_size_remains_unknown(self):
        from sniper import solana_cpmm as cp
        from test_solana_executor import token
        state=cp.pool_state(self.accounts[self.pool],self.at)
        authority,_=cp.pda([b'vault_and_lp_mint_auth_seed'],cp.PROGRAM)
        for side in (0,1):
            self.accounts[state['vault'+str(side)]]=token(state['mint'+str(side)],authority,100_000_000)
        row=self.cp_capture()
        self.assertEqual(row['exit']['status'],'UNKNOWN')
        self.assertIsNone(row['dossier_fields']['exit_route'])
    def test_rpc_call_count_is_enforced(self):
        row=self.cp_capture(max_rpc_calls=1)
        self.assertEqual(row['rpc_calls'],1)
        self.assertEqual(row['exit']['status'],'UNKNOWN')
    def test_canonical_block_change_fails_before_verified_exit(self):
        original=self.rpc.rpc;count=0
        def changed(method,params=None):
            nonlocal count
            result=original(method,params)
            if method=='getBlock':
                count+=1
                if count>1:result['blockhash']='changed'
            return result
        self.rpc.rpc=changed
        self.assertEqual(self.cp_capture()['exit']['status'],'UNKNOWN')
    def holder_fixture(self,supply=100,amounts=(60,40),same_owner=False):
        from sniper import solana_cpmm as cp
        from test_solana_executor import token, account, WALLET
        import base64,struct
        raw=bytearray(base64.b64decode(self.accounts[self.mint]['data'][0]));raw[36:44]=struct.pack('<Q',supply)
        self.accounts[self.mint]=account(raw,cp.TOKEN)
        addresses=[cp.b58encode(bytes([21+i])*32) for i in range(len(amounts))]
        for i,(address,amount) in enumerate(zip(addresses,amounts)):
            owner=WALLET if same_owner else cp.b58encode(bytes([31+i])*32)
            self.accounts[address]=token(self.mint,owner,amount)
        original=self.rpc.rpc
        def reader(method,params=None):
            if method=='getTokenLargestAccounts':return {'context':{'slot':100},'value':[{'address':a} for a in addresses]}
            return original(method,params)
        self.rpc.rpc=reader
    def test_supply_complete_holder_accounts_count_owners_not_controllers(self):
        self.holder_fixture(same_owner=True)
        row=self.cp_capture()
        self.assertEqual(row['holder_coverage']['status'],'VALID')
        self.assertEqual(row['dossier_fields']['observations']['holders_now']['value'],1)
        self.assertEqual(row['holder_coverage']['context']['covered_balance_raw'],'100')
        self.assertEqual(row['independence']['status'],'UNKNOWN')
    def test_partial_largest_accounts_never_claim_complete_holder_count(self):
        self.holder_fixture(supply=1000)
        row=self.cp_capture()
        self.assertEqual(row['holder_coverage']['status'],'UNKNOWN')
        self.assertNotIn('holders_now',row['dossier_fields']['observations'])
        self.assertEqual(row['observations']['reported_total_holders']['value'],100)
    def test_oversized_largest_account_response_is_rejected(self):
        self.holder_fixture(supply=21,amounts=tuple([1]*21))
        row=self.cp_capture()
        self.assertEqual(row['holder_coverage']['status'],'UNKNOWN')
        self.assertNotIn('holders_now',row['dossier_fields']['observations'])


if __name__=='__main__':unittest.main()
