"""Persist isolated prospective acceptance and separately scoped Solana proof."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT),str(ROOT/'tests')]
from test_jev_promotion import app_fixture,acceptance,enrich
from test_jev import check,quote,normalize,raw_report,MINT,BUY,response
from sniper.jev_lifecycle import code_binding,record
from sniper.jev_review import detail
from sniper.jev_dossier import encode


def run_suite(names):
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped:raise AssertionError('Required acceptance tests failed or skipped')
    return {'tests':result.testsRun,'failed':len(result.failures)+len(result.errors),'skipped':len(result.skipped),'modules':names}


def main():
    p=argparse.ArgumentParser();p.add_argument('--binding-root',type=Path,required=True);args=p.parse_args()
    account=json.loads((args.binding_root/'rug_desk/runtime/account.json').read_text())['account_id']
    target=ROOT/'runtime/jev-acceptance'/('promotion-'+str(int(time.time())))
    if target.exists():raise ValueError('New isolated directory required')
    target.mkdir(parents=True)
    paper_tests=run_suite(['test_jev_promotion','test_jev'])
    solana_tests=run_suite(['test_solana_executor','test_solana_service','test_signer_vault.SignerVaultTests.test_sol_roundtrip_policy_binding_tamper_and_exclusive_creation','test_signer_vault.SignerVaultTests.test_live_requires_separate_explicit_provisioning'])
    now=int(time.time());book=app_fixture(target,now)
    try:
        proof=acceptance(target,book.identity['account_id'],now)
        (target/'runtime/jev-review/paper-acceptance.json').write_text(encode(proof))
        record(target,'ACTIVE_PAPER',proof,'ISOLATED ACCEPTANCE ONLY',paper_account_id=book.identity['account_id'],now=now)
        q={**quote(now=now),'created_at_ms':(now-3600)*1000,'sol_usd':'200','sol_observed_at':now}
        market=Mock(quote=Mock(return_value=q));client=Mock(ask=Mock(return_value=response()))
        kwargs=dict(market=market,rugcheck=Mock(report=Mock(return_value=normalize(MINT,raw_report(),now))),
            judge=Mock(ask=Mock(return_value=BUY)),clock=lambda:now,jev_client=client,jev_enrich=enrich)
        out=check(book,MINT,**kwargs);assert out['entry'],out
        duplicate=check(book,MINT,**kwargs);assert duplicate['stage']=='seen_once'
        assert len(book.fills())==1 and client.ask.call_count==1
        a=detail(target,out['jev']['assessment_id'])
        trace={'scope':'SYNTHETIC_PROSPECTIVE_ACCEPTANCE_NOT_STRATEGY_PERFORMANCE','fixture_account_id':book.identity['account_id'],
            'assessment_id':a['id'],'position_id':out['position_id'],'snapshot_at':a['snapshot_at'],'assessment_finished':a['finished'],
            'entry_at':book.positions()[0]['opened_at'],'fills_after_replay':len(book.fills()),'mock_provider_calls':client.ask.call_count,
            'external_api_calls':0,'mainnet_signing_calls':0,'broadcast_calls':0,'entry':out,'replay':duplicate}
        (target/'verification.json').write_text(encode(trace))
    finally:book.close()
    folder=ROOT/'runtime/jev-review';folder.mkdir(parents=True,exist_ok=True)
    proof=acceptance(ROOT,account,int(time.time()))
    proof.update(test_result=paper_tests,trace_path=str(target/'verification.json'),synthetic_fills_excluded=True)
    (folder/'paper-acceptance.json').write_text(encode(proof))
    solana={'scope':'SOLANA_SIGNED_OFFLINE_ACCEPTANCE','code_binding':code_binding(ROOT),**solana_tests,
        'tested_at':int(time.time()),'disposable_fixture_signing_only':True,'real_chain_execution_verified':False,
        'note':'Real Ed25519 over disposable fixture keys; RPC receipts and balances are synthetic. No EVM case is counted.'}
    (folder/'solana-offline-acceptance.json').write_text(encode(solana))
    print(encode({'paper':paper_tests,'solana':solana_tests,'trace':str(target/'verification.json')}))


if __name__=='__main__':main()
