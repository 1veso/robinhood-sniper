"""Prepare an unapproved Bot 4 deployment proposal; never initializes execution."""
import json
from pathlib import Path
import uuid
from .jev import POLICY
from .jev_dossier import digest,encode
from .execution_solana import validate_policy


def operating_digest(policy):
    """Authority metadata cannot conceal a change to reviewed operating limits."""
    p=validate_policy(policy)
    keys=('account_id','paper_account_id','chain_id','genesis_hash','wallet','base_token','base_decimals',
        'execution_target','capital_raw','max_order_raw','max_total_exposure_raw','daily_loss_raw',
        'max_gas_wei_day','max_tx_gas_wei','max_open_positions','max_entries_per_day','cooldown_seconds',
        'max_quote_age_seconds','max_evidence_age_seconds','lease_seconds','rebroadcast_limit',
        'rebroadcast_seconds','max_slippage_bps','max_rent_lamports','compute_units','micro_lamports',
        'program_pins','strategy_binding','strategy_id','jev_policy_sha256')
    return digest({k:p.get(k) for k in keys})


def draft(root):
    root=Path(root);folder=root/'runtime/deployment-review'
    target=folder/'jev-solana-live-draft.json'
    if target.exists():return json.loads(target.read_text(encoding='utf-8'))
    base=json.loads((folder/'solana-usdc-draft.json').read_text(encoding='utf-8'))
    paper=json.loads((root/'rug_desk/runtime/account.json').read_text(encoding='utf-8'))
    for name in ('policy_hash','draft_status'):base.pop(name,None)
    base.update(account_id=str(uuid.uuid4()),paper_account_id=paper['account_id'],strategy_id='pump_jev_v1',
        jev_policy_sha256=digest(POLICY),capital_raw=1000000000,max_order_raw=25000000,
        max_total_exposure_raw=100000000,daily_loss_raw=50000000,max_open_positions=4,max_entries_per_day=6,
        cooldown_seconds=86400,max_gas_wei_day=5000000,max_tx_gas_wei=250000,max_rent_lamports=3000000,
        max_slippage_bps=100,deployment_authorized=False,acceptance_mode=False,signer_authorized=False,
        user_approval_reference='',independent_review_reference='',wallet_simulation_reference='',
        strategy_review_reference='',signer_path='',validation_pair={},
        proposal_status='UNAPPROVED_PROPOSAL_NOT_AN_EXECUTION_ACCOUNT',
        authorization_note='Proposed limits only. No capital, signing, model spend or deployment authorization.',
        execution_scope='Direct classic-SPL Raydium CP-Swap with USDC only; other AMMs and Pump curves excluded',
        scheduling_note='Preserve approved polling and original automatic exits. No terminal-speed or stop guarantee.')
    validate_policy(base)
    target.write_text(encode(base),encoding='utf-8')
    return base
