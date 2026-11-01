"""Public Radon policy, pinned to each initialized account."""
from copy import deepcopy
import json
from pathlib import Path

from sniper.krypton.config import digest, encode, number
from sniper.execution_costs import DEFAULT_POLICY

FIELDS = set('version mode starting_cash_usd ticket_usd coin_episode_cap_usd max_deployed_usd cash_reserve_usd min_liquidity_usd min_volume_24h_usd min_pool_age_seconds max_single_holder max_top10_share max_holder_accounts min_history_buys min_median_hold_seconds min_copy_multiple one_coin_share max_shortlist lookback_days shortlist_ttl_seconds source_max_age_seconds signal_expiry_seconds execution_delay_seconds quote_max_age_seconds evidence_max_age_seconds poll_seconds research_hour_utc report_hour_utc model model_daily_limit_usd model_reservation_usd supported_routes'.split())


def validate(cfg):
    if not isinstance(cfg, dict) or set(cfg) != FIELDS or cfg['version'] != 'radon_copy_v1' or cfg['mode'] != 'paper':
        raise ValueError('Unsupported Radon policy; live execution is not implemented')
    for name in FIELDS - {'version', 'mode', 'model', 'supported_routes'}:
        number(cfg[name])
    for name in FIELDS - {'version', 'mode', 'model', 'supported_routes'}:
        if name.endswith('_seconds') or name in ('max_holder_accounts', 'min_history_buys', 'max_shortlist', 'lookback_days', 'research_hour_utc', 'report_hour_utc'):
            if type(cfg[name]) is not int:
                raise ValueError('Integer policy required: ' + name)
    if not 0 < number(cfg['ticket_usd']) <= number(cfg['coin_episode_cap_usd']) <= number(cfg['max_deployed_usd']) <= number(cfg['starting_cash_usd']):
        raise ValueError('Invalid capital limits')
    if number(cfg['cash_reserve_usd']) >= number(cfg['starting_cash_usd']):
        raise ValueError('Cash reserve consumes bankroll')
    for name in ('max_single_holder', 'max_top10_share', 'one_coin_share'):
        if not 0 < number(cfg[name]) <= 1:
            raise ValueError('Invalid policy fraction')
    bounds = {'poll_seconds': (1, 60), 'quote_max_age_seconds': (1, 30), 'evidence_max_age_seconds': (1, 300),
        'signal_expiry_seconds': (5, 300), 'execution_delay_seconds': (0, 120), 'source_max_age_seconds': (1, 300),
        'shortlist_ttl_seconds': (60, 86400), 'max_shortlist': (1, 50), 'min_history_buys': (1, 10000),
        'lookback_days': (1, 365), 'research_hour_utc': (0, 23), 'report_hour_utc': (0, 23),
        'max_holder_accounts': (20, 4096), 'min_pool_age_seconds': (1, 86400), 'min_median_hold_seconds': (1, 86400)}
    for name, (low, high) in bounds.items():
        if not low <= cfg[name] <= high:
            raise ValueError('Policy outside supported range: ' + name)
    if cfg['execution_delay_seconds'] >= cfg['signal_expiry_seconds'] or cfg['model'] != 'grok-4.7':
        raise ValueError('Invalid model or execution timing')
    if not isinstance(cfg['supported_routes'], list) or not cfg['supported_routes'] or not set(cfg['supported_routes']) <= {'raydium', 'pumpfun', 'pumpswap'}:
        raise ValueError('Unsupported route coverage')
    if not 0 < number(cfg['model_reservation_usd']) <= number(cfg['model_daily_limit_usd']):
        raise ValueError('Invalid model budget')
    return cfg


def load(root):
    return validate(json.loads((Path(root) / 'config.json').read_text(encoding='utf-8-sig')))


def costs():
    return deepcopy(DEFAULT_POLICY)


def policy_hash(cfg):
    return digest({'strategy': cfg, 'execution_costs': costs()})
