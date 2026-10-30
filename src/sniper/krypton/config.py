"""Immutable initialized account configuration; public values only."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from sniper.execution_costs import DEFAULT_POLICY


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid numeric evidence')
    try: n = Decimal(str(value))
    except InvalidOperation: raise ValueError('Invalid numeric evidence') from None
    if not n.is_finite() or n < 0 or (positive and n == 0):
        raise ValueError('Invalid numeric range')
    return n


def load(root):
    cfg = json.loads((Path(root)/'config.json').read_text(encoding='utf-8-sig'))
    expected = {'version','mode','starting_cash_usd','min_liquidity_usd','min_pool_age_minutes',
        'max_single_holder','max_top10_share','require_mint_disabled','require_freeze_disabled',
        'require_lp_locked','grok_max_risk','model','max_position_pct','stop_loss_pct',
        'take_profit_pct','max_daily_loss_pct','poll_seconds','quote_max_age_seconds',
        'evidence_max_age_seconds','candidate_expiry_seconds','max_holder_accounts','candidates_per_scan'}
    if set(cfg) != expected or cfg['mode'] != 'paper' or cfg['version'] != 'krypton_new_token_v1':
        raise ValueError('Unsupported Krypton configuration; live mode is not implemented')
    for k in ('require_mint_disabled','require_freeze_disabled','require_lp_locked'):
        if cfg[k] is not True: raise ValueError('Required safety checks cannot be disabled')
    for k in ('max_single_holder','max_top10_share','max_position_pct','stop_loss_pct','max_daily_loss_pct'):
        if not 0 < number(cfg[k]) < 1: raise ValueError('Invalid percentage')
    for k in ('starting_cash_usd','min_liquidity_usd','take_profit_pct'): number(cfg[k], True)
    if number(cfg['grok_max_risk']) > 4 or cfg['model'] != 'grok-4.7': raise ValueError('Unsupported model/risk gate')
    bounds = {'poll_seconds':(60,3600),'quote_max_age_seconds':(1,120),'evidence_max_age_seconds':(1,300),
        'candidate_expiry_seconds':(300,3600),'max_holder_accounts':(20,4096),'candidates_per_scan':(1,2),'min_pool_age_minutes':(10,1440)}
    for k,(lo,hi) in bounds.items():
        if type(cfg[k]) is not int or not lo <= cfg[k] <= hi: raise ValueError('Invalid bounded setting: '+k)
    return cfg


def cost_policy():
    return deepcopy(DEFAULT_POLICY)
