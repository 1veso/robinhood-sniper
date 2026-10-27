"""Pinned V3 buy/sell quotes for bounded PAPER simulation; never sends a swap."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import time

from .evm import (FACTORY, WETH, USDG, address, hash32, quantity, token_units,
                  _words, _address_word, RpcError)
from sniper.models import MarketQuote
from .solana import SourceError

QUOTER = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
D = Decimal


class V3RouteClient:
    def __init__(self, rpc, market, *, clock=time.time):
        self.rpc, self.market, self.clock = rpc, market, clock
        self.last_evidence = None

    def quote(self, token, pool, *, max_notional, sell_quantity=D(0)):
        token, pool = address(token), address(pool)
        max_notional, sell_quantity = D(max_notional), D(sell_quantity)
        if not max_notional.is_finite() or not 0 < max_notional <= 1000 or not sell_quantity.is_finite() or sell_quantity < 0:
            raise ValueError("Invalid bounded paper route size")
        rpc = self.rpc
        with rpc.budget(25):
            rpc.verify_chain()
            block = quantity(rpc.call("eth_blockNumber")) - 5
            header = rpc.call("eth_getBlockByNumber", [hex(block), False])
            anchor = hash32(header["hash"])
            if quantity(header["number"]) != block or not 0 <= int(self.clock()) - quantity(header["timestamp"]) <= 30:
                raise RpcError("Route block is mismatched or stale")
            def call(to, data):
                return rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
            def uint(to, data):
                return int(_words(call(to, data), 1)[0], 16)
            if rpc.call("eth_getCode", [QUOTER, hex(block)]) == "0x":
                raise RpcError("Quoter has no bytecode")
            if (_address_word(_words(call(QUOTER, "0xc45a0155"), 1)[0]) != FACTORY or
                    _address_word(_words(call(QUOTER, "0x4aa4a4fc"), 1)[0]) != WETH):
                raise RpcError("Quoter deployment identity mismatch")
            token0 = _address_word(_words(call(pool, "0x0dfe1681"), 1)[0])
            token1 = _address_word(_words(call(pool, "0xd21220a7"), 1)[0])
            if token not in (token0, token1):
                raise RpcError("Token is not in requested pool")
            base = token1 if token == token0 else token0
            if base not in (WETH, USDG):
                raise RpcError("Route requires a supported quote asset")
            fee = uint(pool, "0xddca3f43")
            if not 0 < fee < 1_000_000:
                raise RpcError("Invalid pool fee")
            lookup = "0x1698ee82" + token0[2:].zfill(64) + token1[2:].zfill(64) + f"{fee:064x}"
            if _address_word(_words(call(FACTORY, lookup), 1)[0]) != pool:
                raise RpcError("Pool is not registered in the configured factory")
            decimals = {a: uint(a, "0x313ce567") for a in (token, base)}
            if any(n > 36 for n in decimals.values()):
                raise RpcError("Unsupported token precision")
            slot = _words(call(pool, "0x3850c7bd"), 7)
            sqrt_price = int(slot[0], 16)
            if not sqrt_price or int(slot[6], 16) != 1:
                raise RpcError("Pool is uninitialized or locked")
            ratio = D(sqrt_price) ** 2 / D(2 ** 192)
            raw_ratio = ratio if token == token0 else 1 / ratio
            base_per_token = raw_ratio * D(10) ** (decimals[token] - decimals[base])
            base_quote = self.market.quote(base, now=int(self.clock()))
            eth_quote = base_quote if base == WETH else self.market.quote(WETH, now=int(self.clock()))
            price = base_per_token * base_quote.price_usd
            liquidity_source = 'dexscreener:pool-specific TVL'
            try:
                liquidity = self.market.quote(token, now=int(self.clock()), pool_id=pool).liquidity_usd
            except SourceError as exc:
                if str(exc) != 'no valid matching pool; no substitute quote':
                    raise
                # Some factory-verified pools are not indexed by the market
                # feed. Value their actual same-block ERC20 balances instead.
                # This is total pool value, not active V3 depth; the independent
                # size-bounded quoter remains mandatory for executable depth.
                selector = '0x70a08231' + pool[2:].zfill(64)
                token_balance = token_units(uint(token, selector), decimals[token])
                base_balance = token_units(uint(base, selector), decimals[base])
                liquidity = token_balance * price + base_balance * base_quote.price_usd
                liquidity_source = 'pinned ERC20 pool balances valued at on-chain spot and current base/USD; includes inactive liquidity'
            if min(price, liquidity, base_quote.price_usd, eth_quote.price_usd) <= 0:
                raise RpcError("Missing route valuation or liquidity")
            amount_in = int((max_notional / base_quote.price_usd * D(10) ** decimals[base]).to_integral_value(rounding=ROUND_DOWN))
            if amount_in <= 0:
                raise RpcError("Route input rounds to zero")
            def simulate(source, destination, amount):
                data = "0xc6a5026a" + source[2:].zfill(64) + destination[2:].zfill(64) + f"{amount:064x}{fee:064x}" + "0" * 64
                words = _words(call(QUOTER, data), 4)
                output, gas = int(words[0], 16), int(words[3], 16)
                if output <= 0 or gas <= 0:
                    raise RpcError("Route produced no output or gas estimate")
                return output, gas
            buy_raw, buy_gas = simulate(base, token, amount_in)
            buy_qty = token_units(buy_raw, decimals[token])
            covered_sell_raw = max(buy_raw, int((sell_quantity * D(10) ** decimals[token]).to_integral_value(rounding=ROUND_UP)))
            sell_raw, sell_gas = simulate(token, base, covered_sell_raw)
            covered_sell_qty = token_units(covered_sell_raw, decimals[token])
            buy_value = token_units(amount_in, decimals[base]) * base_quote.price_usd
            sell_value = token_units(sell_raw, decimals[base]) * base_quote.price_usd
            buy_loss = max(D(0), buy_value / (buy_qty * price) - 1)
            sell_loss = max(D(0), 1 - sell_value / (covered_sell_qty * price))
            fee_fraction = D(fee) / 1_000_000
            price_impact = max(D(0), (1 + buy_loss) * (1 - fee_fraction) - 1,
                               1 - (1 - sell_loss) / (1 - fee_fraction))
            # Quote costs include pool fees. Add 100 bps adverse execution stress;
            # the engine also retains its existing liquidity-impact stress model.
            buy_bps, sell_bps = buy_loss * 10000 + 100, sell_loss * 10000 + 100
            gas_price = quantity(rpc.call("eth_gasPrice"))
            gas_usd = D((max(buy_gas, sell_gas) + 100000) * 2 * gas_price) / D(10 ** 18) * eth_quote.price_usd
            end_header = rpc.call("eth_getBlockByNumber", [hex(block), False])
            if hash32(end_header["hash"]) != anchor or int(self.clock()) - quantity(header["timestamp"]) > 30:
                raise RpcError("Route anchor changed or became stale")
            observed = int(self.clock())
            self.last_evidence = {"block": block, "block_hash": anchor, "observed_at": observed,
                "pool": pool, "token": token, "base": base, "quoter": QUOTER, "fee": fee,
                "buy_input_raw": str(amount_in), "buy_output_raw": str(buy_raw),
                "sell_input_raw": str(covered_sell_raw), "sell_output_raw": str(sell_raw),
                "liquidity_usd": str(liquidity), "liquidity_source": liquidity_source,
                "gas_assumption": "2x (quoter gas + 100000) at current gas price; not a signed transaction estimate",
                "scope": "Two independent same-block quoter calls, not an executed round trip. No wallet, approvals or future sell guarantee."}
            return MarketQuote(f"eip155:4663/erc20:{token}", pool, observed, price, liquidity,
                buy_bps, sell_bps, gas_usd, True, f"paper:v3-quoter:{block}:{anchor}",
                route_max_notional_usd=buy_qty * price, route_max_sell_quantity=covered_sell_qty,
                route_impact_bps=price_impact * 10000)
