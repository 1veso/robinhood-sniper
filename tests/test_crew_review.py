"""Isolated deterministic crew review/restart checks; no provider or model calls."""
from copy import deepcopy
from decimal import Decimal,ROUND_DOWN,ROUND_UP
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sniper.crew_review import (ROLES, REVISION, review, require_packet, validate_packet,
    persist_review, request_recheck, claim_due_rechecks, finish_recheck, recover_rechecks)
from sniper.jev_dossier import digest
from test_jev import fixture, NOW
from test_rug_desk import key


def route_facts(d,at):
    from sniper import solana_cpmm as cp
    from sniper.adapters.solana_execution import USDC
    state={'mint0':USDC,'mint1':d['mint'],'decimals0':6,'decimals1':6,
        'reserve0':1200000000000,'reserve1':1000000000000,'creator_fee_on':0,'creator_fee_rate':0,'trade_fee_rate':2500}
    ticket=Decimal(str(d['ticket_usd']));price=Decimal(str(d['observations']['price_now']['value']))
    raw=int((ticket*10**6).to_integral_value(rounding=ROUND_DOWN));buy=cp.quote_output(state,USDC,raw)
    paper=int((ticket/price*10**6).to_integral_value(rounding=ROUND_UP));covered=max(buy,paper);output=cp.quote_output(state,d['mint'],covered)
    a,b=state['reserve1'],state['reserve0'];impact=max(Decimal(0),1-Decimal(output)*a/(Decimal(covered)*b))*100
    low,high=covered,a
    for _ in range(48):
        if low>=high:break
        mid=(low+high+1)//2
        try:cp.quote_output(state,d['mint'],mid)
        except ValueError:high=mid-1
        else:low=mid
    capacity=Decimal(low)*b/a/10**6
    pins={address:{'fixture':'isolated-offline-pin'} for address in (cp.PROGRAM,cp.TOKEN,cp.ATA)}
    program_review={'scope':'CREW_READ_ONLY_PROGRAM_REVIEW','account_id':d['account_id'],
        'config_sha256':'fixture-config','review_reference':'isolated-offline-test',
        'reviewed_at':at-1,'expires_at':at+300,'program_pins':pins}
    context={'slot':123,'block_hash':'fixture-canonical','block_time':at,'commitment':'finalized','program_observations':pins}
    route={'status':'VALID','observed_at':at,'ticket_usd':str(ticket),'base_usd':'1','reference_price_usd':str(price),
        'input_usdc_raw':str(raw),'buy_quantity_raw':str(buy),'paper_quantity_upper_bound_raw':str(paper),
        'sell_quantity_raw':str(covered),'sell_output_usdc_raw':str(output),'exit_price_impact_pct':str(impact),
        'exit_capacity_usd':str(capacity),'state':state,'context':context,'review_sha256':digest(program_review),
        'program_review':deepcopy(program_review),
        'wallet_simulated':False,'network_costs_included':False,'real_money_enabled':False}
    venue={'status':'VALID','phase':'AMM','observed_at':at,'context':context,
           'review_sha256':digest(program_review),'program_review':deepcopy(program_review)}
    for k,v in (('exit_capacity_usd',capacity),('exit_price_impact_pct',impact),('exit_quote_ticket_usd',ticket)):
        d['observations'][k]['value']=float(v)
    return venue,route


def model_fixture(d,b):
    from rug_desk import config
    return {'model':config.MODEL,'verdict':'BUY','validated':True,'confidence':'.8','judged_at':d['snapshot_at'],
        'binding':{'account_id':d['account_id'],'settings_sha256':b['policy_sha256']},
        'request_id':'fixture:model:1','usage':{'cost_verified':True},
        'citations':['https://x.com/fixture/status/1'],'search_calls':['x_keyword_search']}


def produced(at=NOW,full=False):
    d,b=fixture(at)
    d['observations']['holders_now']['value']=12;d['observations']['holders_then']['value']=10
    if full:
        ref=next(iter(d['source_refs']))
        for name,value,stamp in (('social_mentions_now',12,at),('social_mentions_then',10,at-3600)):
            d['observations'][name]={'value':value,'observed_at':stamp,'source_ref':ref}
    venue,route=route_facts(d,at)
    records=[]
    for observed in (at-3600,at):
        current=observed==at
        obs={}
        for name,value in d['observations'].items():
            if value is None or value['observed_at']!=observed: continue
            mapped=name[:-5]+'_now' if name.endswith('_then') else name
            obs[mapped]={k:value[k] for k in ('value','observed_at')}
        proof={'verified':True,'observed_at':observed,'mint':d['mint'],'pool':d['pool']}
        count=12 if current else 10
        rows=[{'address':key(10+i),'owner':key(40+i),'balance_raw':'100'} for i in range(count)]
        holder={'status':'VALID','value':count,'observed_at':observed,'source_observation_time_known':True,
            'context':{'slot':123,'block_hash':'fixture-canonical','block_time':observed,'commitment':'finalized',
                'accounts_examined':count,'minted_supply_raw':str(count*100),'covered_balance_raw':str(count*100),
                'covered_accounts':rows,'positive_owners':sorted(x['owner'] for x in rows)}}
        p={'schema_version':1,'producer_revision':'isolated_fixture_v1',
           'account_id':d['account_id'],'config_sha256':b['policy_sha256'],
           'mint':d['mint'],'pool':d['pool'],'observed_at':observed,'received_at':observed,
           'observations':{'holders_now':deepcopy(holder)},
           'holder_coverage':holder,
           'venue':venue if current else {'status':'UNKNOWN'},
           'exit':route if current else {'status':'UNKNOWN'},
           'independence':{'status':'VALID' if current else 'UNKNOWN'},
           'dossier_fields':{'observations':obs,'venue_phase':'AMM' if current else 'UNKNOWN',
              'exit_route':proof if current else None,'independence_review':proof if current else None}}
        ref='rug_desk:crew:'+digest(p)
        d['source_refs'][ref]={'provider':'isolated_producer','reference':ref,'observed_at':observed,'payload_sha256':digest(p)}
        for value in d['observations'].values():
            if value is not None and value['observed_at']==observed: value['source_ref']=ref
        if current:
            d['venue_phase_source_ref']=ref
            for name in ('exit_route','independence_review'): d[name]['source_ref']=ref
        records.append(p)
    return d,b,records


def replace_record(d,records,index,change):
    old=records[index];oldref='rug_desk:crew:'+digest(old)
    new=deepcopy(old);change(new);records[index]=new
    ref='rug_desk:crew:'+digest(new)
    d['source_refs'].pop(oldref)
    d['source_refs'][ref]={'provider':'isolated_producer','reference':ref,'observed_at':new['observed_at'],'payload_sha256':digest(new)}
    for value in d['observations'].values():
        if value is not None and value['source_ref']==oldref:value['source_ref']=ref
    if d['venue_phase_source_ref']==oldref:d['venue_phase_source_ref']=ref
    for name in ('exit_route','independence_review'):
        if d[name] and d[name]['source_ref']==oldref:d[name]['source_ref']=ref


class CrewReviewTests(unittest.TestCase):
    def test_ten_responsibilities_unknown_models_and_optional_social_are_honest(self):
        d,b,r=produced();p=review(d,b,now=NOW,producer_records=r)
        self.assertEqual(p['verdict'],'PASS')
        self.assertEqual({x['role'] for x in p['roles']},set(ROLES))
        self.assertEqual(len(p['roles']),10)
        states={x['role']:x['status'] for x in p['roles']}
        self.assertEqual(states['Berlin'],'UNKNOWN');self.assertEqual(states['Denver'],'UNKNOWN')
        self.assertFalse(p['crew_complete']);self.assertFalse(p['execution_authority']);self.assertEqual(p['model_calls'],0)
        self.assertEqual(validate_packet(p,d,b,now=NOW,producer_records=r),'PASS')
        self.assertIs(require_packet(p,d,b,now=NOW,producer_records=r),p)

    def test_disabled_models_explicit_without_changing_evidence_thresholds(self):
        d,b,r=produced()
        p=review(d,b,now=NOW,producer_records=r,role_inputs={'Berlin':{'status':'BLOCKED','reason':'Paid models disabled'}})
        self.assertEqual(next(x for x in p['roles'] if x['role']=='Berlin')['status'],'BLOCKED')
        self.assertEqual(p['verdict'],'PASS');self.assertFalse(p['crew_complete'])
        self.assertEqual(validate_packet(p,d,b,now=NOW,producer_records=r),'PASS')

    def test_complete_crew_requires_actual_bound_model_not_role_flags(self):
        d,b,r=produced(full=True)
        forged=review(d,b,now=NOW,producer_records=r,role_inputs={'Berlin':{'status':'COMPLETE'}})
        self.assertFalse(forged['crew_complete'])
        actual=review(d,b,now=NOW,producer_records=r,model_evidence=model_fixture(d,b))
        self.assertTrue(actual['crew_complete']);self.assertEqual(actual['verdict'],'PASS')
        self.assertEqual(validate_packet(actual,d,b,now=NOW,producer_records=r),'PASS')
        for change in ({'validated':False},{'judged_at':NOW+1},{'usage':{'cost_verified':False}},
                       {'binding':{'account_id':'other','settings_sha256':b['policy_sha256']}}):
            j=model_fixture(d,b);j.update(change)
            self.assertFalse(review(d,b,now=NOW,producer_records=r,model_evidence=j)['crew_complete'])

    def test_holder_arithmetic_is_independent_of_producer_valid_flag(self):
        d,b,r=produced()
        replace_record(d,r,1,lambda p:p['holder_coverage']['context']['covered_accounts'][0].update(balance_raw='1'))
        p=review(d,b,now=NOW,producer_records=r)
        self.assertIn('holder_coverage_arithmetic',{f['criterion'] for f in p['findings']})

    def test_full_crew_social_claims_need_scoped_producer_evidence(self):
        d,b,r=produced(full=True)
        unscoped=next(ref for ref in d['source_refs'] if not ref.startswith('rug_desk:crew:'))
        d['observations']['social_mentions_now']['source_ref']=unscoped
        packet=review(d,b,now=NOW,producer_records=r,model_evidence=model_fixture(d,b))
        self.assertEqual(packet['verdict'],'VETO');self.assertFalse(packet['crew_complete'])
        finding=next(f for f in packet['findings'] if f['criterion']=='provenance_social_mentions_now')
        self.assertEqual(finding['owner'],'Denver')

    def test_program_review_expiry_before_final_decision_invalidates_fresh_packet(self):
        d,b,r=produced()
        def short(p):
            for component in ('venue','exit'):
                p[component]['program_review']['expires_at']=NOW+5
                p[component]['review_sha256']=digest(p[component]['program_review'])
        replace_record(d,r,1,short)
        packet=review(d,b,now=NOW,producer_records=r)
        self.assertEqual(packet['verdict'],'PASS')
        with self.assertRaises(ValueError):validate_packet(packet,d,b,now=NOW+5,producer_records=r)
        self.assertEqual(review(d,b,now=NOW+5,producer_records=r)['verdict'],'VETO')

    def test_program_review_wrong_scope_identity_pins_or_post_capture_is_veto(self):
        for change in ({'scope':'EXECUTION_APPROVAL'},{'account_id':'other'},
                       {'config_sha256':'other'},{'reviewed_at':NOW+1},
                       {'program_pins':{'unreviewed':'program'}}):
            with self.subTest(change=change):
                d,b,r=produced()
                def modified(p):
                    for component in ('venue','exit'):
                        p[component]['program_review'].update(change)
                        p[component]['review_sha256']=digest(p[component]['program_review'])
                replace_record(d,r,1,modified)
                self.assertEqual(review(d,b,now=NOW,producer_records=r)['verdict'],'VETO')

    def test_exit_arithmetic_is_independent_of_producer_valid_flag(self):
        d,b,r=produced();replace_record(d,r,1,lambda p:p['exit'].update(sell_output_usdc_raw='999999999999'))
        p=review(d,b,now=NOW,producer_records=r)
        self.assertIn('exit_quote_arithmetic',{f['criterion'] for f in p['findings']})

    def test_original_hard_rejection_cannot_be_overridden(self):
        d,b,r=produced();b['eligible']=False
        p=review(d,b,now=NOW,producer_records=r,role_inputs={'Palermo':{'status':'COMPLETE'}})
        self.assertEqual(p['verdict'],'VETO')
        self.assertEqual(next(x for x in p['roles'] if x['role']=='Palermo')['status'],'VETO')
        with self.assertRaises(ValueError):require_packet(p,d,b,now=NOW,producer_records=r)

    def test_provider_boolean_cannot_establish_independence_or_exit(self):
        d,b=fixture();p=review(d,b,now=NOW)
        codes={f['criterion'] for f in p['findings']}
        self.assertIn('provenance_independence_review',codes);self.assertIn('provenance_exit_route',codes)
        self.assertEqual(p['verdict'],'VETO')

    def test_missing_payload_is_not_a_verified_hash_claim(self):
        d,b,r=produced();p=review(d,b,now=NOW)
        self.assertIn('producer_provenance',{f['criterion'] for f in p['findings']})
        self.assertEqual(p['verdict'],'VETO')

    def test_received_after_snapshot_is_lookahead_even_if_observed_earlier(self):
        d,b,r=produced();replace_record(d,r,1,lambda p:p.update(received_at=NOW+1))
        p=review(d,b,now=NOW+2,producer_records=r)
        self.assertIn('producer_provenance_time',{f['criterion'] for f in p['findings']})

    def test_wrong_account_pool_or_config_rejected(self):
        for key in ('account_id','pool','config_sha256'):
            with self.subTest(key=key):
                d,b,r=produced();replace_record(d,r,1,lambda p:p.update({key:'different'}))
                p=review(d,b,now=NOW,producer_records=r)
                self.assertIn('producer_provenance_binding',{f['criterion'] for f in p['findings']})

    def test_claim_contradicts_immutable_measurement(self):
        d,b,r=produced();d['observations']['holders_now']['value']=121
        p=review(d,b,now=NOW,producer_records=r)
        f=next(x for x in p['findings'] if x['criterion']=='provenance_holders_now')
        self.assertEqual(f['status'],'CONTRADICTORY');self.assertEqual(f['owner'],'Lisbon');self.assertTrue(f['evidence_refs'])

    def test_unknown_component_never_passes_as_verified(self):
        d,b,r=produced();replace_record(d,r,1,lambda p:p['exit'].update(status='UNKNOWN'))
        p=review(d,b,now=NOW,producer_records=r)
        self.assertIn('provenance_exit_route',{f['criterion'] for f in p['findings']})

    def test_provider_capture_time_is_not_canonical_holder_window(self):
        for field in ('time','coverage'):
            d,b,r=produced()
            def change(payload):
                if field=='time':payload['observations']['holders_now']['source_observation_time_known']=False
                else:payload['holder_coverage']['status']='UNKNOWN'
            replace_record(d,r,1,change)
            p=review(d,b,now=NOW,producer_records=r)
            self.assertIn('holder_observation_time_unknown',{f['criterion'] for f in p['findings']})

    def test_unchanged_ticket_and_window_rules_remain_authoritative(self):
        d,b,r=produced();d['observations']['exit_quote_ticket_usd']['value']=1
        self.assertEqual(review(d,b,now=NOW,producer_records=r)['verdict'],'VETO')
        d,b,r=produced();d['window']['start']+=1
        self.assertEqual(review(d,b,now=NOW,producer_records=r)['verdict'],'VETO')

    def test_final_guard_rejects_stale_or_changed_binding(self):
        d,b,r=produced();p=review(d,b,now=NOW,producer_records=r)
        with self.assertRaises(ValueError):require_packet(p,d,b,now=NOW+301,producer_records=r)
        changed=deepcopy(b);changed['eligible']=False
        with self.assertRaises(ValueError):require_packet(p,d,changed,now=NOW,producer_records=r)
        with self.assertRaises(ValueError):require_packet(p,d,b,now=NOW-1,producer_records=r)

    def test_rehashed_forged_pass_or_roles_cannot_bypass_independent_review(self):
        d,b,r=produced();b['eligible']=False;p=review(d,b,now=NOW,producer_records=r)
        p['verdict']='PASS';p['findings']=[];p['packet_id']=digest({k:v for k,v in p.items() if k!='packet_id'})
        with self.assertRaises(ValueError):require_packet(p,d,b,now=NOW,producer_records=r)
        d,b,r=produced();p=review(d,b,now=NOW,producer_records=r)
        p['roles'][0]['status']='VETO';p['packet_id']=digest({k:v for k,v in p.items() if k!='packet_id'})
        with self.assertRaises(ValueError):validate_packet(p,d,b,now=NOW,producer_records=r)

    def test_neon_account_not_implicitly_promoted(self):
        d,b,r=produced();b['source']='neon'
        self.assertEqual(review(d,b,now=NOW,producer_records=r)['verdict'],'VETO')


class CrewRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'existing-assessments.sqlite3'
        self.db=sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE existing_account(value TEXT)');self.db.execute("INSERT INTO existing_account VALUES('unchanged')");self.db.commit()
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def missing(self,at=NOW):
        d,b,r=produced(at);d['independence_review']=None
        return review(d,b,now=at,producer_records=r)

    def test_immutable_packets_idempotent_and_do_not_touch_account(self):
        p=self.missing();self.assertEqual(persist_review(self.db,p),p);persist_review(self.db,p)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM crew_packets').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT value FROM existing_account').fetchone()[0],'unchanged')
        p['verdict']='PASS'
        with self.assertRaises(ValueError):persist_review(self.db,p)

    def test_due_work_survives_restart_and_claim_is_once(self):
        p=self.missing();persist_review(self.db,p);key=request_recheck(self.db,p['packet_id'],now=NOW)
        self.assertEqual(claim_due_rechecks(self.db,now=NOW+1799),[])
        self.db.close();self.db=sqlite3.connect(self.path)
        jobs=claim_due_rechecks(self.db,now=NOW+1800)
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['request_id'],key);self.assertIn('Lisbon',jobs[0]['owners'])
        self.assertEqual(claim_due_rechecks(self.db,now=NOW+1801),[])
        self.assertEqual(self.db.execute('SELECT attempts FROM crew_rechecks').fetchone()[0],1)

    def test_new_snapshot_cannot_reset_attempt_cap(self):
        p=self.missing();persist_review(self.db,p);key=request_recheck(self.db,p['packet_id'],now=NOW)
        claim_due_rechecks(self.db,now=NOW+1800)
        newer=self.missing(NOW+1800);persist_review(self.db,newer)
        self.assertEqual(request_recheck(self.db,newer['packet_id'],now=NOW+1800),key)
        finish_recheck(self.db,key,newer['packet_id'],now=NOW+1800)
        self.assertEqual(claim_due_rechecks(self.db,now=NOW+10000),[])
        self.assertEqual(self.db.execute('SELECT state FROM crew_rechecks').fetchone()[0],'COMPLETE')

    def test_old_or_before_due_snapshot_cannot_complete_recheck(self):
        p=self.missing();persist_review(self.db,p);key=request_recheck(self.db,p['packet_id'],now=NOW)
        claim_due_rechecks(self.db,now=NOW+1800)
        for result in (p,self.missing(NOW+1799)):
            persist_review(self.db,result)
            with self.assertRaises(ValueError):finish_recheck(self.db,key,result['packet_id'],now=NOW+1800)

    def test_crashed_claim_is_recorded_and_not_reissued(self):
        p=self.missing();persist_review(self.db,p);request_recheck(self.db,p['packet_id'],now=NOW)
        claim_due_rechecks(self.db,now=NOW+1800)
        self.db.close();self.db=sqlite3.connect(self.path)
        self.assertEqual(recover_rechecks(self.db,now=NOW+2100),1)
        self.assertEqual(claim_due_rechecks(self.db,now=NOW+2200),[])
        self.assertEqual(self.db.execute('SELECT state FROM crew_rechecks').fetchone()[0],'INTERRUPTED')

    def test_financial_transaction_not_committed_by_research_helper(self):
        self.db.execute("UPDATE existing_account SET value='uncommitted'")
        with self.assertRaises(ValueError):persist_review(self.db,self.missing())
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT value FROM existing_account').fetchone()[0],'unchanged')


if __name__=='__main__':unittest.main()
