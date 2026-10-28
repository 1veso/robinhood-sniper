"""Evidence-scoped promotion report. Paper/offline proof is never chain execution."""
import hashlib
import json
from pathlib import Path
import time

from .jev import POLICY
from .jev_dossier import digest
from .jev_lifecycle import current, code_binding, PAPER_CHECKS
from .jev_review import settings


def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}


def report(root,*,now=None):
    root=Path(root);now=int(time.time()) if now is None else now
    folder=root/'runtime/jev-review';state=current(root);rows=[]
    identity=read(root/'rug_desk/runtime/account.json');account=identity.get('account_id')
    acceptance=read(folder/'paper-acceptance.json')
    code=code_binding(root)
    valid_acceptance=(acceptance.get('scope')=='JEV_PAPER_INTEGRATION_ACCEPTANCE'
        and account is not None and acceptance.get('account_id')==account and acceptance.get('code_binding')==code)
    def add(key,required,status,reason,paths,scope):
        assert status in ('PASS','FAIL','NOT VERIFIED')
        rows.append({'id':key,'required_for':required,'status':status,'reason':reason,
            'scope':scope,'evidence':[{'path':str(root/p),'exists':(root/p).is_file(),
                'sha256':hashlib.sha256((root/p).read_bytes()).hexdigest() if (root/p).is_file() and (root/p).suffix!='.sqlite3' else None} for p in paths]})
    for check in PAPER_CHECKS:
        passed=valid_acceptance and acceptance.get('checks',{}).get(check)=='PASS'
        add(check,'ACTIVE_PAPER','PASS' if passed else 'NOT VERIFIED',
            'Bound integration acceptance passed' if passed else 'Current bound acceptance is missing',
            ['runtime/jev-review/paper-acceptance.json','tests/test_jev_promotion.py'],'ISOLATED_PAPER_INTEGRATION')
    add('explicit_paper_promotion','PAPER_OPERATION','PASS' if state['stage']!='SHADOW' and state['paper_account_id']==account else 'NOT VERIFIED',
        'Promotion is explicit and bound to the existing account; no minimum shadow duration',
        ['runtime/jev-review/assessments.sqlite3'],'LOCAL_AUTHORITY')
    try:config=settings(root)
    except Exception:config={}
    paid=config.get('enabled') is True and config.get('paid_authorized') is True and float(config.get('daily_budget_usd',0))>0
    add('jev_paid_authorization','PAPER_OPERATION','PASS' if paid else 'FAIL',
        'Jev calls are explicitly disabled' if not paid else 'Explicit local authorization is present; vendor access still needs verification',
        ['runtime/jev-review/settings.json'],'AUTHORIZATION')
    add('jev_server_key','PAPER_OPERATION','PASS' if (root/'runtime/typesafe-api-key.txt').is_file() else 'NOT VERIFIED',
        'File presence only; no credential content or external call is used by readiness reporting',[], 'CREDENTIAL_METADATA_ONLY')
    from rug_desk.model_gate import status as grok_status
    grok=grok_status(root/'rug_desk',now)
    add('approved_grok_gate','PAPER_OPERATION','PASS' if grok['enabled'] else 'FAIL',
        '; '.join(grok['blockers']) or 'Existing bound Grok gate enabled',
        ['rug_desk/runtime/paid-ai.json','rug_desk/runtime/inference-test.json','rug_desk/runtime/x-search-test.json'],'EXISTING_MODEL_GATE')
    prospective=read(folder/'prospective-verification.json')
    add('complete_live_dossier_and_real_jev_reply','PAPER_OPERATION','PASS' if (
        prospective.get('scope')=='REAL_PROSPECTIVE_JEV' and prospective.get('account_id')==account
        and prospective.get('code_binding')==code and prospective.get('validated') is True
        and type(prospective.get('observed_at'))is int and 0<=now-prospective['observed_at']<=3600) else 'NOT VERIFIED',
        'Requires actual pre-entry factual history, independence, phase, size-bounded exit evidence and a real typed reply; fixtures do not qualify',
        ['runtime/jev-review/prospective-verification.json'],'REAL_PROSPECTIVE_OBSERVATIONS')
    solana=read(folder/'solana-offline-acceptance.json')
    signed_pass=(solana.get('scope')=='SOLANA_SIGNED_OFFLINE_ACCEPTANCE' and solana.get('code_binding')==code
        and solana.get('failed')==0 and solana.get('skipped')==0 and solana.get('tests',0)>0)
    add('solana_instruction_signing_recovery_exits','LIVE_READY','PASS' if signed_pass else 'NOT VERIFIED',
        'Direct classic-SPL Raydium CP-Swap only; real Ed25519 signatures over synthetic accounts/receipts. This is software acceptance, not mainnet execution.',
        ['runtime/jev-review/solana-offline-acceptance.json','src/sniper/execution_solana.py','src/sniper/execution_solana_service.py'],'SOLANA_OFFLINE_SOFTWARE')
    probe=read(folder/'solana-route-check.json')
    recent=type(probe.get('finished_at'))is int and 0<=now-probe['finished_at']<=3600
    add('supported_solana_route','LIVE_READY',('PASS' if probe.get('independent_pool_quote_validated') else 'FAIL') if recent else 'NOT VERIFIED',
        'Dated direct CP setup route result; other AMMs, EVM routes, Pump curves and arbitrary Jupiter instructions are not substitutes',
        ['runtime/jev-review/solana-route-check.json'],'READ_ONLY_SOLANA_SETUP_ROUTE')
    draft=read(root/'runtime/deployment-review/jev-solana-live-draft.json')
    separated=bool(draft.get('account_id') and draft.get('account_id')!=account and draft.get('paper_account_id')==account
        and draft.get('chain_id')=='solana:mainnet' and draft.get('strategy_id')=='pump_jev_v1' and draft.get('jev_policy_sha256')==digest(POLICY))
    try:
        from .jev_deployment import operating_digest
        operating_digest(draft)
        separated=separated and draft['capital_raw']>0 and draft['max_order_raw']>0
    except (ValueError,KeyError,TypeError):separated=False
    add('separate_bounded_live_draft','LIVE_READY','PASS' if separated else 'NOT VERIFIED',
        'Prepared policy is a proposal, not approved capital or authority',
        ['runtime/deployment-review/jev-solana-live-draft.json'],'DRAFT_POLICY_ONLY')
    proofs=[('independent_solana_review','SOLANA_INDEPENDENT_EXECUTOR_REVIEW',86400*30),
        ('funded_wallet_buy_sell_validation','SOLANA_FUNDED_WALLET_VALIDATION',3600),
        ('signed_chain_lifecycle_validation','SOLANA_CHAIN_EXECUTION_VALIDATION',86400*7),
        ('reviewed_program_upgrade_pins','SOLANA_PROGRAM_PIN_REVIEW',86400),
        ('jev_live_cost_reconciliation','JEV_LIVE_COST_RECONCILIATION',86400),
        ('approved_capital_risk_policy','SOLANA_CAPITAL_RISK_APPROVAL',86400*30),
        ('restricted_signer_provisioning','SOLANA_RESTRICTED_SIGNER_PROVISIONING',86400),
        ('strategy_evidence_review','JEV_STRATEGY_EVIDENCE_REVIEW',86400*30)]
    for name,scope,age in proofs:
        rel='runtime/jev-review/readiness/'+name+'.json';p=read(root/rel)
        bound=(p.get('scope')==scope and p.get('validated') is True and p.get('chain_id')=='solana:mainnet'
            and p.get('paper_account_id')==account and p.get('live_account_id')==draft.get('account_id')
            and p.get('policy_sha256')==digest(draft) and p.get('code_binding')==code
            and type(p.get('verified_at'))is int and 0<=now-p['verified_at']<=age)
        if name=='signed_chain_lifecycle_validation':
            bound=bound and p.get('rpc_canonical_verified') is True and p.get('signed_buy_signature') and p.get('signed_sell_signature') and p.get('finalized_receipts_verified') is True
        if name=='independent_solana_review':bound=bound and p.get('independent_reviewer') and p.get('review_reference')
        add(name,'LIVE_READY','PASS' if bound else ('FAIL' if p else 'NOT VERIFIED'),
            'Current, chain/account/policy/code-bound evidence required. A paper fill, EVM result or unsigned simulation is insufficient.',[rel],scope)
    operational=[r for r in rows if r['required_for']=='PAPER_OPERATION']
    add('operational_models_and_factual_evidence','LIVE_READY','PASS' if operational and all(r['status']=='PASS' for r in operational) else 'FAIL',
        'The complete Jev/Grok strategy must have real operational inputs; paper promotion itself does not satisfy this requirement',
        ['runtime/jev-review/prospective-verification.json','runtime/jev-review/settings.json','rug_desk/runtime/paid-ai.json'],'MODEL_AND_DATA_DEPENDENCIES')
    ready=[r for r in rows if r['required_for']=='LIVE_READY']
    add('all_live_ready_requirements','LIVE','PASS' if ready and all(r['status']=='PASS' for r in ready) and state['stage'] in ('LIVE_READY','LIVE') else 'FAIL',
        'Every chain-specific readiness requirement and explicit LIVE READY transition is required',
        ['runtime/jev-review/assessments.sqlite3'],'PROMOTION_AUTHORITY')
    deployed=read(root/'runtime/executor/solana-live/policy.json')
    approval=read(folder/'readiness/initial-deployment-approval.json')
    approved=(separated and deployed.get('account_id')==draft.get('account_id') and deployed.get('deployment_authorized') is True
        and deployed.get('strategy_id')=='pump_jev_v1' and approval.get('scope')=='USER_INITIAL_SOLANA_DEPLOYMENT_AUTHORIZATION'
        and approval.get('policy_sha256')==digest(deployed) and approval.get('authorized') is True and approval.get('reference'))
    try:approved=approved and operating_digest(deployed)==operating_digest(draft)
    except (ValueError,KeyError,TypeError):approved=False
    add('initial_live_deployment_authorization','LIVE','PASS' if approved else 'NOT VERIFIED',
        'Explicit authorization for the reviewed separate live account; ordinary trades thereafter are automatic within this policy',
        ['runtime/jev-review/readiness/initial-deployment-approval.json','runtime/executor/solana-live/policy.json'],'INITIAL_DEPLOYMENT_ONLY')
    return {'schema':'jev_promotion_readiness_v1','generated_at':now,'stage':state['stage'],'account_id':account,
        'entry_status':'READY' if operational and all(r['status']=='PASS' for r in operational) else 'BLOCKED',
        'requirements':rows,'live_execution_ready':bool(ready) and all(r['status']=='PASS' for r in ready),
        'automatic_promotion':False,'minimum_shadow_wait_seconds':0,
        'evidence_note':'Each PASS is limited to its stated scope. Synthetic execution acceptance is excluded from strategy P&L.'}


def write_report(root):
    root=Path(root);out=report(root)
    folder=root/'runtime/jev-review';folder.mkdir(parents=True,exist_ok=True)
    (folder/'readiness.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    lines=['# Pump / Jev promotion readiness','',f"Stage: **{out['stage']}**. Entries: **{out['entry_status']}**. Live ready: **{out['live_execution_ready']}**.",'',
        'No minimum shadow waiting period. Promotions require explicit, bound evidence and authorization; time, profit and confidence never promote.',
        '', '| Requirement | Required for | Result | Evidence and scope |','|---|---|---|---|']
    for r in out['requirements']:
        links='; '.join('['+Path(e['path']).name+']('+e['path'].replace('\\','/')+')' for e in r['evidence']) or 'Credential file metadata only'
        lines.append('| '+r['id']+' | '+r['required_for']+' | **'+r['status']+'** | '+r['reason'].replace('|','/')+' '+links+' |')
    lines+=['',out['evidence_note']]
    (folder/'readiness.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return out
