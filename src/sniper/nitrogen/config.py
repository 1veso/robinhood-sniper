"""Versioned paper baseline. No live mode or implicit configuration migration."""
import json
from pathlib import Path
from sniper.krypton.config import encode, digest, number
from sniper.execution_costs import validate_policy
from sniper.adapters.solana import validate_address
from .judge import BINDING, MODEL


def validate(c):
    if c.get('version') != 'nitrogen_paper_v1' or c.get('mode') != 'paper' or c.get('judge_binding') != BINDING or c.get('model') != MODEL:
        raise ValueError('NITROGEN paper policy binding invalid')
    for k in ('max_position_pct','max_single_holder','max_top10_share','max_daily_loss_pct','max_exposure_pct','approval_max_price_move_pct'):
        if not 0 < number(c[k]) < 1: raise ValueError('Invalid risk percentage')
    for k in ('starting_cash_usd','min_liquidity_usd','model_daily_limit_usd','model_reservation_usd'): number(c[k], True)
    for k in ('require_mint_disabled','require_freeze_disabled','require_lp_locked'):
        if c[k] is not True: raise ValueError('Required checks cannot be disabled')
    bounds = {'poll_seconds':(30,300),'quote_max_age_seconds':(1,60),'evidence_max_age_seconds':(1,300),
              'order_expiry_seconds':(1,300),'candidate_expiry_seconds':(1800,7200), 'whale_max_age_seconds':(60,3600),
              'max_holder_accounts':(20,4096),'candidates_per_scan':(1,2),'min_pool_age_minutes':(10,1440),'max_open':(1,10),'max_entries_per_day':(1,20)}
    for k,(lo,hi) in bounds.items():
        if type(c[k]) is not int or not lo <= c[k] <= hi: raise ValueError('Invalid '+k)
    if c['report_timezone'] != 'Europe/Berlin' or c['report_time'] != '21:00': raise ValueError('Invalid report schedule')
    if not isinstance(c['tracked_wallets'],list) or len(c['tracked_wallets'])>4 or len(set(c['tracked_wallets']))!=len(c['tracked_wallets']): raise ValueError('At most four distinct tracked wallets')
    for w in c['tracked_wallets']: validate_address(w)
    e=c['exit']
    for k in ('stop_pct','trail_pct','liq_drop_pct'):
        if not 0 < number(e[k]) < 100: raise ValueError('Invalid exit percent')
    number(e['time_stop_hours'],True);number(e['time_stop_min_pct'])
    if len(e['ladder']) != 2 or sum(number(x[1]) for x in e['ladder']) >= 1: raise ValueError('Invalid partial ladder')
    if not 0 < number(e['ladder'][0][0]) < number(e['ladder'][1][0]): raise ValueError('Invalid ladder gains')
    if any(not 0 < number(x[1]) < 1 for x in e['ladder']): raise ValueError('Invalid partial sizes')
    validate_policy(c['cost_policy'])
    return c


def load(root): return validate(json.loads((Path(root)/'nitrogen_bot/config.json').read_text(encoding='utf-8-sig')))
