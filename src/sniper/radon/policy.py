"""Shared execution rules for replay and prospective paper accounting."""
from decimal import Decimal as D
from sniper.adapters.solana import validate_address
from sniper.execution_costs import simulate
from .config import number, costs


def quote_check(cfg, quote, mint, now, route=None):
    if quote.get('mint') != mint:
        raise ValueError('Quote mint does not match position')
    validate_address(quote['pool'])
    observed = quote.get('observed_at')
    if type(observed) is not int or not 0 <= now - observed <= cfg['quote_max_age_seconds']:
        raise ValueError('Quote missing, stale or future-dated')
    if route is not None and route not in cfg['supported_routes']:
        raise ValueError('Unsupported source route')
    number(quote['price'], positive=True)
    number(quote['liquidity'], positive=True)


def market_check(cfg, quote, mint, now, route):
    quote_check(cfg, quote, mint, now, route)
    if number(quote['liquidity']) < number(cfg['min_liquidity_usd']):
        raise ValueError('Pool liquidity below policy')
    if number(quote.get('volume_24h')) < number(cfg['min_volume_24h_usd']):
        raise ValueError('Pool volume below policy')
    created = quote.get('created_at_ms')
    if type(created) is not int or not 0 < created <= (now - cfg['min_pool_age_seconds']) * 1000:
        raise ValueError('Pool age unavailable or below policy; pool age is not token launch time')


def integrity_check(cfg, evidence, mint, now):
    facts, lp = evidence.get('facts', {}), evidence.get('lp', {})
    for item in (facts, lp):
        at = item.get('observed_at')
        if item.get('mint') != mint or type(at) is not int or not 0 <= now - at <= cfg['evidence_max_age_seconds']:
            raise ValueError('Token safety evidence unavailable or stale')
    if facts.get('mint_disabled') is not True or facts.get('freeze_disabled') is not True or facts.get('complete_owner_coverage') is not True:
        raise ValueError('Mint/freeze/holder evidence is incomplete or unsafe')
    if number(facts.get('max_single_holder')) > number(cfg['max_single_holder']) or number(facts.get('top10_share')) > number(cfg['max_top10_share']):
        raise ValueError('Holder concentration exceeds policy')
    if lp.get('pool') != evidence['quote']['pool'] or lp.get('state') != 'PASS':
        raise ValueError('Pinned-pool LP evidence unavailable or failed')


def buy_plan(cfg, quote, mint, now, *, cash, deployed, episode_spend, route):
    market_check(cfg, quote, mint, now, route)
    ticket = number(cfg['ticket_usd'])
    if episode_spend + ticket > number(cfg['coin_episode_cap_usd']):
        raise ValueError('Coin episode cap reached')
    result = simulate(costs(), 'buy', quote, ticket, now)
    reserve = number(result['rent_capital_reserved_usd'])
    fee = number(result['explicit_fee_usd'])
    if cash - ticket - reserve - fee < number(cfg['cash_reserve_usd']):
        raise ValueError('Insufficient account cash after fees and reserve')
    if deployed + ticket + fee + reserve > number(cfg['max_deployed_usd']):
        raise ValueError('Account exposure limit reached')
    return result


def sell_plan(cfg, quote, mint, now, quantity):
    quote_check(cfg, quote, mint, now)
    return simulate(costs(), 'sell', quote, quantity, now)
