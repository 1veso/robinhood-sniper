"""Prospective shared-account acceptance, isolated from all running strategies."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock,patch

from test_jev import fixture,response,Book,check,normalize,raw_report,quote,MINT,POOL,BUY,NOW
from sniper.jev_dossier import encode,digest
from sniper.jev_review import Journal,DEFAULT_SETTINGS,detail,comparison
from sniper.jev_lifecycle import current,record,code_binding,CODE_FILES,PAPER_CHECKS
from sniper.jev_admission import live_guard,live_binding,live_cost_rows
from sniper.jev_readiness import report
from rug_desk.watch import watch,apply_quote

SOURCE=Path(__file__).resolve().parents[1]


def app_fixture(root,now=NOW):
    for rel in CODE_FILES:
        path=root/rel;path.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(SOURCE/rel,path)
    book=Book(root/'rug_desk',initialize=True);book.enable_costs(now)
    with closing(Journal(root)):pass
    return book


def acceptance(root,account,now=NOW):
    return {'scope':'JEV_PAPER_INTEGRATION_ACCEPTANCE','account_id':account,
        'tested_at':now,'code_binding':code_binding(root),'checks':dict.fromkeys(PAPER_CHECKS,'PASS')}


def enrich(d):
    full,_=fixture(d['snapshot_at'])
    for key in ('venue_phase','venue_phase_source_ref','window','observations','independence_review','exit_route'):
        d[key]=full[key]
    # Bind fixture-only provenance to this exact input, not another candidate.
    ref=next(iter(d['source_refs']))
    d['venue_phase_source_ref']=ref
    for value in d['observations'].values():
        if value is not None:value['source_ref']=ref
    for key in ('independence_review','exit_route'):d[key]['source_ref']=ref
    return d


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.book=app_fixture(self.root)
        self.market=Mock();self.market.quote.return_value={**quote(),'sol_usd':'200','sol_observed_at':NOW}
        self.rug=Mock(report=Mock(return_value=normalize(MINT,raw_report(),NOW)))
        self.judge=Mock(ask=Mock(return_value=BUY));self.client=Mock(ask=Mock(return_value=response()))
    def tearDown(self):self.book.close();self.tmp.cleanup()
    def promote(self):
        evidence=acceptance(self.root,self.book.identity['account_id'])
        (self.root/'runtime/jev-review/paper-acceptance.json').write_text(encode(evidence))
        return record(self.root,'ACTIVE_PAPER',evidence,'isolated user authority fixture',
            paper_account_id=self.book.identity['account_id'],now=NOW)
    def run_candidate(self,**kw):
        return check(self.book,MINT,market=self.market,rugcheck=self.rug,judge=self.judge,
            clock=lambda:NOW,jev_client=self.client,jev_enrich=enrich,**kw)

    def test_prospective_entry_and_duplicate_no_extra_order(self):
        self.promote();out=self.run_candidate()
        self.assertTrue(out['entry'],out)
        a=detail(self.root,out['jev']['assessment_id']);position=self.book.positions()[0]
        self.assertLessEqual(a['finished'],position['opened_at']);self.assertTrue(a['baseline']['prospective'])
        self.assertEqual(position['judgment']['jev_assessment_id'],a['id'])
        self.assertEqual(len(self.book.fills()),1)
        self.assertEqual(self.run_candidate()['stage'],'seen_once')
        self.assertEqual(self.client.ask.call_count,1);self.assertEqual(len(self.book.fills()),1)
        self.assertNotIn('judgment',encode(self.client.ask.call_args.args[0]))
        self.assertNotIn('verdict',encode(self.client.ask.call_args.args[0]))
        position.update(closed_at=NOW+100,realized_pnl='1.20')
        self.assertEqual(comparison([a],[('rug_desk',self.book.identity['account_id'],position)])['records'][0]['filtered_net_pnl'],'1.20')

    def test_timeout_failure_closed(self):
        self.promote();self.client.ask.side_effect=TimeoutError('private error')
        out=self.run_candidate();self.assertFalse(out['entry']);self.assertEqual(out['jev']['status'],'TIMEOUT')
        self.assertEqual(self.book.fills(),[]);self.assertNotIn('private',encode(out))

    def select_fixture_leader(self):
        from rug_desk.trader_acceptance import trader,ReplayJudge
        from rug_desk.traders import Selections
        with self.book.transaction():
            self.book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
        selected=Selections(self.book).select([trader(NOW)],judge=ReplayJudge(),clock=lambda:NOW)
        self.assertTrue(selected['entry_enabled'])

    def test_leader_buy_obeys_jev_and_restart_dedup_then_original_exit(self):
        from rug_desk.trader_acceptance import event
        from rug_desk.leader_signals import process_event
        from rug_desk.traders import Selections
        from sniper.jev_admission import paper_review
        self.promote();self.select_fixture_leader();signal=event(NOW,mint=MINT)
        def injected_review(*args,**kwargs):
            # Supply only offline evidence/client fixtures; real admission and
            # final Book transaction guards still run unchanged.
            kwargs.update(client=self.client,enrich=enrich)
            return paper_review(*args,**kwargs)
        deps=dict(clock=lambda:NOW,market=self.market,rugcheck=self.rug,judge=self.judge)
        with patch('sniper.jev_admission.paper_review',side_effect=injected_review):
            out=process_event(self.book,signal,**deps)
            self.assertTrue(out['entry'],out)
            self.assertEqual(self.book.positions()[0]['judgment']['jev_assessment_id'],out['jev']['assessment_id'])
            self.book.close();self.book=Book(self.root/'rug_desk')
            self.assertFalse(process_event(self.book,signal,**deps)['entry'])
        self.assertEqual(len(self.book.fills()),1);self.client.ask.assert_called_once()
        self.assertTrue(Selections(self.book).select([],clock=lambda:NOW)['empty_selection'])
        self.market.quote.side_effect=TimeoutError()
        watch(self.book,market=self.market,clock=lambda:NOW+1)
        self.assertEqual(len(self.book.fills()),1)
        p=self.book.positions()[0]
        apply_quote(self.book,p['id'],{**quote(price='.4',now=NOW+2),'sol_usd':'200','sol_observed_at':NOW+2},NOW+2)
        self.assertEqual(len(self.book.fills()),2);self.assertFalse(self.book.positions())

    def test_leader_buy_cannot_bypass_disabled_jev_calls(self):
        from rug_desk.trader_acceptance import event
        from rug_desk.leader_signals import process_event
        from sniper.jev_admission import paper_review
        self.promote();self.select_fixture_leader()
        def evidence_only(*args,**kwargs):
            kwargs.update(enrich=enrich)
            return paper_review(*args,**kwargs)
        with patch('sniper.jev_admission.paper_review',side_effect=evidence_only):
            out=process_event(self.book,event(NOW,mint=MINT),clock=lambda:NOW,
                market=self.market,rugcheck=self.rug,judge=self.judge)
        self.assertFalse(out['entry'],out);self.assertEqual(out['jev']['status'],'DISABLED')
        self.assertEqual(self.book.fills(),[]);self.client.ask.assert_not_called()

    def test_invalid_response_failure_closed(self):
        self.promote();self.client.ask.return_value={'action':'BUY','orders':['override risk']}
        out=self.run_candidate();self.assertFalse(out['entry']);self.assertEqual(out['jev']['status'],'MALFORMED_RESPONSE')
        self.assertEqual(self.book.fills(),[])

    def test_missing_data_failure_closed_before_api(self):
        self.promote()
        out=check(self.book,MINT,market=self.market,rugcheck=self.rug,judge=self.judge,clock=lambda:NOW,jev_client=self.client)
        self.assertFalse(out['entry']);self.assertEqual(out['jev']['status'],'INSUFFICIENT_EVIDENCE');self.client.ask.assert_not_called()

    def test_real_disabled_calls_do_not_inherit_mock_buy_authority(self):
        self.promote()
        out=check(self.book,MINT,market=self.market,rugcheck=self.rug,judge=self.judge,clock=lambda:NOW,jev_enrich=enrich)
        self.assertFalse(out['entry']);self.assertEqual(out['jev']['status'],'DISABLED');self.assertEqual(self.book.fills(),[])

    def test_hard_rejection_precedes_jev(self):
        self.promote();self.rug.report.return_value['failure']='authority_revoked: rejected'
        out=self.run_candidate();self.assertFalse(out['entry']);self.client.ask.assert_not_called();self.judge.ask.assert_not_called()

    def test_direct_book_call_cannot_bypass_filter(self):
        self.promote()
        with self.assertRaises(ValueError):self.book.open_paper(self.market.quote.return_value,BUY,NOW)
        self.assertEqual(self.book.fills(),[])

    def test_changed_code_blocks_new_entry_without_resetting_promotion(self):
        self.promote();path=self.root/'src/sniper/jev.py';path.write_text(path.read_text()+'\n# changed\n')
        out=self.run_candidate();self.assertFalse(out['entry']);self.client.ask.assert_not_called()
        self.assertEqual(current(self.root)['stage'],'ACTIVE_PAPER')

    def test_trial_clock_not_started_while_full_strategy_inputs_blocked(self):
        self.promote()
        from rug_desk.report import start_trial
        with self.assertRaisesRegex(ValueError,'Complete prospective Jev'):start_trial(self.book,NOW)
        self.assertFalse((self.book.runtime/'trial.json').exists())

    def test_final_transaction_recheck_prevents_changed_authority(self):
        self.promote()
        def disable():
            (self.root/'runtime/jev-review/settings.json').write_text(encode({**DEFAULT_SETTINGS,'enabled':False}))
        out=self.run_candidate(entry_guard=disable)
        self.assertFalse(out['entry']);self.assertEqual(self.book.fills(),[]);self.assertEqual(self.client.ask.call_count,1)

    def test_refresh_still_rejects_bad_pool_after_jev_buy(self):
        self.promote();initial=self.market.quote.return_value
        self.market.quote.side_effect=[initial,{**initial,'liquidity':'1'}]
        out=self.run_candidate();self.assertFalse(out['entry']);self.assertEqual(out['stage'],'refresh');self.assertEqual(self.book.fills(),[])

    def test_original_exits_continue_with_disabled_jev_and_missing_quote_never_fills(self):
        self.promote();out=self.run_candidate();self.assertTrue(out['entry'],out)
        (self.root/'runtime/jev-review/settings.json').write_text(encode({**DEFAULT_SETTINGS,'enabled':False}))
        self.market.quote.side_effect=TimeoutError()
        watch(self.book,market=self.market,clock=lambda:NOW+1)
        self.assertEqual(len(self.book.fills()),1)
        self.market.quote.side_effect=None
        p=self.book.positions()[0]
        exit_quote={**quote(price='.4',now=NOW+2),'sol_usd':'200','sol_observed_at':NOW+2}
        apply_quote(self.book,p['id'],exit_quote,NOW+2)
        self.assertEqual(len(self.book.fills()),2);self.assertFalse(self.book.positions())

    def test_disabled_shadow_preserves_baseline(self):
        (self.root/'runtime/jev-review/settings.json').write_text(encode({**DEFAULT_SETTINGS,'enabled':False}))
        out=self.run_candidate();self.assertTrue(out['entry']);self.client.ask.assert_not_called()

    def test_neon_identity_not_promoted_and_no_automatic_promotion(self):
        self.promote()
        other=Book(self.root/'neon_bot',initialize=True)
        try:
            from sniper.jev_admission import paper_review
            self.assertIsNone(paper_review(other,{}, {},NOW))
        finally:other.close()
        state=current(self.root)
        self.assertEqual(state['stage'],'ACTIVE_PAPER');self.assertFalse(state['automatic_promotion'])
        with self.assertRaises(ValueError):record(self.root,'LIVE',{},'profit increased',live_account_id='different',now=NOW+999999)
        with self.assertRaises(ValueError):record(self.root,'LIVE_READY',{},'confidence high',live_account_id='different',now=NOW)
        self.assertEqual(current(self.root)['stage'],'ACTIVE_PAPER')

    def test_paper_identity_code_and_separate_live_binding(self):
        e=acceptance(self.root,self.book.identity['account_id'])
        for changes in ({'account_id':'other'},{'code_binding':{}},{'scope':'UNSIGNED_SIMULATION'},{'tested_at':NOW-86401}):
            with self.assertRaises(ValueError):record(self.root,'ACTIVE_PAPER',{**e,**changes},'test',paper_account_id=self.book.identity['account_id'],now=NOW)
        self.assertEqual(current(self.root)['stage'],'SHADOW')
        from sniper.jev import POLICY
        p={'strategy_id':'pump_jev_v1','jev_policy_sha256':digest(POLICY),'account_id':'live','paper_account_id':self.book.identity['account_id']}
        live_binding(p)
        with self.assertRaises(ValueError):live_binding({**p,'account_id':p['paper_account_id']})
        with self.assertRaises(ValueError):live_guard(self.root,p,{},NOW)

    def test_changed_live_capital_cannot_reuse_initial_review(self):
        from sniper.adapters.solana_execution import MAINNET_GENESIS,USDC
        from sniper.jev_deployment import operating_digest
        from sniper.jev_admission import reviewed_live_binding
        from sniper.jev import POLICY
        p={'account_id':'separate-live-fixture','chain_id':'solana:mainnet','genesis_hash':MAINNET_GENESIS,
            'wallet':MINT,'base_token':USDC,'base_decimals':6,'execution_target':'automatic','capital_raw':1000000000,
            'max_order_raw':25000000,'max_total_exposure_raw':100000000,'daily_loss_raw':50000000,
            'max_gas_wei_day':1000000,'max_tx_gas_wei':50000,'deployment_authorized':False,'acceptance_mode':False}
        p.update(strategy_id='pump_jev_v1',paper_account_id=self.book.identity['account_id'],jev_policy_sha256=digest(POLICY))
        state={'live_account_id':p['account_id'],'paper_account_id':p['paper_account_id'],'reviewed_operating_sha256':operating_digest(p)}
        reviewed_live_binding(p,state)
        changed={**p,'capital_raw':int(p['capital_raw'])+1}
        with self.assertRaises(ValueError):reviewed_live_binding(changed,state)

    def test_evm_paper_or_unsigned_evidence_never_proves_solana_ready(self):
        self.promote();folder=self.root/'runtime/jev-review'
        for scope in ('EVM_SIGNED_ACCEPTANCE','PAPER_FILL','UNSIGNED_SIMULATION'):
            (folder/'solana-offline-acceptance.json').write_text(encode({'scope':scope,'code_binding':code_binding(self.root),'failed':0,'skipped':0,'tests':99}))
            out=report(self.root,now=NOW)
            self.assertEqual(next(r for r in out['requirements'] if r['id']=='solana_instruction_signing_recovery_exits')['status'],'NOT VERIFIED')
            self.assertFalse(out['live_execution_ready']);self.assertEqual(out['minimum_shadow_wait_seconds'],0)

    def test_actual_jev_bills_account_bound_unknown_and_conflicting_rejected(self):
        self.promote();out=self.run_candidate();a=detail(self.root,out['jev']['assessment_id'])
        account=self.book.identity['account_id']
        rows=live_cost_rows(self.root,account);self.assertIsNone(rows[0][2]);self.assertEqual(live_cost_rows(self.root,'other'),[])
        from rug_desk.report import summary
        usage=summary(self.book,NOW)['api_usage']
        self.assertEqual(usage[0]['provider'],'typesafe');self.assertIsNone(usage[0]['billed_usd'])
        self.assertIsNone(summary(self.book,NOW)['live_readiness']['net_including_open_positions_usd'])
        folder=self.root/'runtime/jev-review/billing';folder.mkdir()
        path=folder/(a['id']+'.json')
        bill={'scope':'TYPESAFE_PROVIDER_INVOICE','account_id':account,'assessment_id':a['id'],
            'provider_invoice_reference':'OFFLINE FIXTURE','billed_usd':'0.0001'}
        path.write_text(encode(bill));self.assertEqual(live_cost_rows(self.root,account)[0][2],'0.0001')
        path.write_text(encode({**bill,'account_id':'other'}))
        with self.assertRaises(ValueError):live_cost_rows(self.root,account)

    def test_readonly_wallet_preparation_does_not_need_live_activation(self):
        from solana_executor import wallet_validation
        from rug_desk.model_gate import strategy_binding
        from sniper.adapters.solana_execution import MAINNET_GENESIS,USDC
        p={'account_id':'draft','chain_id':'solana:mainnet','genesis_hash':MAINNET_GENESIS,'wallet':MINT,
            'base_token':USDC,'base_decimals':6,'execution_target':'automatic','capital_raw':1000000000,
            'max_order_raw':25000000,'max_total_exposure_raw':100000000,'daily_loss_raw':50000000,
            'max_gas_wei_day':1000000,'max_tx_gas_wei':50000,'deployment_authorized':False,'acceptance_mode':False,
            'strategy_binding':strategy_binding(self.book.root)}
        (self.root/'runtime/solana-trading-address.txt').write_text(MINT)
        with patch('solana_executor.reader') as reader,patch('solana_executor.SolanaExecutionAdapter') as adapter:
            with self.assertRaisesRegex(ValueError,'Explicit validation setup pair'):
                wallet_validation(self.root,p,preparation=True)
            adapter.return_value.sign.assert_not_called();adapter.return_value.broadcast.assert_not_called()
        self.assertEqual(current(self.root)['stage'],'SHADOW');self.assertEqual(self.book.fills(),[])


if __name__=='__main__':unittest.main()
