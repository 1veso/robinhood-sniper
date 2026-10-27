"""Pinned Solana pool observations. These are indicative prices, not swap quotes."""
from __future__ import annotations
from decimal import Decimal
import time
from rug_desk import config
from rug_desk.book import number
from sniper.adapters.solana import DexScreenerClient, validate_address


def validate_quote(quote, now):
    validate_address(quote['mint'])
    validate_address(quote['pool'])
    number(quote['price'], 'price', positive=True)
    number(quote['liquidity'], 'liquidity')
    observed = quote.get('observed_at')
    if type(observed) is not int or not 0 <= now - observed <= config.QUOTE_MAX_AGE_SECONDS:
        raise ValueError('Pool quote missing, stale or from the future')


def parse(mint, pool, payload, now):
    validate_address(mint)
    validate_address(pool)
    pairs = payload if isinstance(payload, list) else payload.get('pairs') if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        raise ValueError('Missing pool list')
    matches = [p for p in pairs if isinstance(p, dict) and p.get('chainId') == 'solana'
               and p.get('pairAddress') == pool and isinstance(p.get('baseToken'), dict)
               and p['baseToken'].get('address') == mint]
    if len(matches) != 1:
        raise ValueError('Selected safety pool unavailable, duplicated or mismatched')
    pair = matches[0]
    liquidity = pair.get('liquidity')
    out = {'mint': mint, 'pool': pool, 'observed_at': now,
           'price': str(number(pair.get('priceUsd'), 'price', positive=True)),
           'liquidity': str(number(liquidity.get('usd') if isinstance(liquidity, dict) else None, 'liquidity')),
           'symbol': str(pair['baseToken'].get('symbol', ''))[:40],
           'created_at_ms': pair.get('pairCreatedAt'),
           'h1': pair.get('txns', {}).get('h1') if isinstance(pair.get('txns'), dict) else None,
           'source': 'dexscreener', 'executable': False}
    validate_quote(out, now)
    return out


def check_limits(quote, now):
    validate_quote(quote, now)
    liquidity = number(quote['liquidity'])
    if liquidity < config.FACTS['min_liquidity_usd']:
        return 'pool_liquidity: below minimum'
    if Decimal(config.TICKET_USD) / liquidity > Decimal(str(config.FACTS['max_ticket_share'])):
        return 'ticket_share: exceeds pool limit'
    created = quote.get('created_at_ms')
    if type(created) is not int or created <= 0 or created > now * 1000:
        return 'pool_age: missing or invalid timestamp'
    age = Decimal(now) - Decimal(created) / 1000
    if not config.FACTS['min_age_min'] * 60 <= age <= config.FACTS['max_age_hours'] * 3600:
        return 'pool_age: outside permitted range'
    h1 = quote.get('h1')
    if not isinstance(h1, dict) or any(type(h1.get(k)) is not int or h1[k] < 0 for k in ('buys', 'sells')):
        return 'hourly_sell_activity: missing or invalid transaction counts'
    if h1['buys'] >= 20 and h1['sells'] == 0:
        return 'hourly_sell_activity: 20+ buys with zero sells'
    return None


class Market:
    def __init__(self, client=None, clock=time.time, costs=False):
        self.client, self.clock = client or DexScreenerClient(timeout=15), clock
        self.costs, self.native_cache = costs, None

    def native_price(self):
        now = int(self.clock())
        if self.native_cache and 0 <= now - self.native_cache['sol_observed_at'] <= 20:
            return dict(self.native_cache)
        mint = 'So11111111111111111111111111111111111111112'
        stable = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
        pairs = self.client.pairs(mint)
        choices = []
        for pair in pairs if isinstance(pairs, list) else []:
            if (isinstance(pair, dict) and pair.get('chainId') == 'solana'
                    and (pair.get('baseToken') or {}).get('address') == mint
                    and (pair.get('quoteToken') or {}).get('address') == stable):
                try:
                    choices.append((number(pair['liquidity']['usd'], positive=True), number(pair['priceUsd'], positive=True)))
                except (ValueError, KeyError, TypeError):
                    continue
        if not choices:
            raise ValueError('SOL/USD valuation feed unavailable')
        self.native_cache = {'sol_usd': str(max(choices)[1]), 'sol_observed_at': int(self.clock()), 'sol_price_source': 'DexScreener WSOL/USDC indicative USD'}
        return dict(self.native_cache)

    def quote(self, mint, pool):
        payload = self.client.pairs(mint)
        out = parse(mint, pool, payload, int(self.clock()))
        if self.costs:
            out.update(self.native_price())
        return out

    def profiles(self):
        return self.client.profiles(limit=25)
