"""Reuse bounded market, holder, LP and SOL-cost reads, under Radon's account."""
from sniper.krypton.providers import Providers as Base
from rug_desk.market import parse
from .config import number


class Providers(Base):
    settings_folder = 'radon_bot'
    agent_name = 'Radon'

    def quote(self, mint, pool=None):
        pairs = self.pairs(mint)
        now = int(self.clock())
        candidates = []
        for pair in pairs if isinstance(pairs, list) else []:
            try:
                q = parse(mint, pair['pairAddress'], [pair], now)
                if pool is not None and q['pool'] != pool:
                    continue
                q['volume_24h'] = str(number(pair['volume']['h24']))
                candidates.append(q)
            except (ValueError, KeyError, TypeError):
                continue
        if not candidates:
            raise ValueError('No complete, pinned Solana base-token market observation')
        quote = max(candidates, key=lambda q: number(q['liquidity']))
        return self.costs(quote)

    def evidence(self, mint, quote):
        return {'quote': quote, 'facts': self.holders(mint, self.book.cfg), 'lp': self.lp(mint, quote['pool'])}
