"""Read-only token reports and indicative market quotes. Unknown is not safe."""
from __future__ import annotations

import json
import re
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sniper.models import MarketQuote, SafetyEvidence

JsonTransport = Callable[[str, str, dict | None, dict, float], Any]
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class SourceError(ValueError):
    """A source failed or returned unusable evidence."""


def request_json(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> Any:
    if method not in ("GET", "POST") or not url.startswith("https://"):
        raise SourceError("unsupported transport request")
    raw = None if body is None else json.dumps(body, allow_nan=False).encode()
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(3_000_001)
    if len(data) > 3_000_000:
        raise SourceError("response exceeds size limit")
    return json.loads(data.decode("utf-8"), parse_float=Decimal, parse_constant=lambda x: (_ for _ in ()).throw(SourceError("non-finite JSON")))


def decimal_value(value: Any, name: str, *, minimum=Decimal("0"), maximum=None) -> Decimal:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, str, Decimal)):
        raise SourceError(f"{name}: missing or invalid number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise SourceError(f"{name}: invalid decimal") from exc
    if not result.is_finite() or result < minimum or (maximum is not None and result > maximum):
        raise SourceError(f"{name}: invalid numeric range")
    return result


def validate_address(address: str, chain_id: str = "solana") -> str:
    if not isinstance(address, str):
        raise SourceError("token address must be text")
    if chain_id == "solana":
        if not (32 <= len(address) <= 44) or any(c not in BASE58 for c in address):
            raise SourceError("invalid Solana mint")
        n = 0
        for c in address:
            n = n * 58 + BASE58.index(c)
        decoded_size = (n.bit_length() + 7) // 8 + len(address) - len(address.lstrip("1"))
        if decoded_size != 32:
            raise SourceError("Solana mint must decode to 32 bytes")
        return address
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise SourceError("invalid EVM token address")
    return address.lower()


def _optional_decimal(value: Any, name: str, *, maximum=None) -> Decimal | None:
    if value is None:
        return None
    return decimal_value(value, name, maximum=maximum)


def _authority(data: dict, token: dict, key: str) -> bool | None:
    owner = token if key in token else data if key in data else None
    if owner is None:
        return None
    value = owner[key]
    if value is None:
        return True
    if isinstance(value, str) and value:
        return False
    raise SourceError(f"{key}: malformed authority")


def normalize_report(mint: str, payload: Any, *, now: int, max_risk_score=40,
                     min_lp_locked_pct=90, max_top10_pct=30, max_creator_pct=5) -> SafetyEvidence:
    mint = validate_address(mint)
    if not isinstance(payload, dict):
        raise SourceError("Rugcheck report is not an object")
    if "mint" in payload and payload["mint"] != mint:
        raise SourceError("report mint mismatch")
    token = payload.get("token")
    if not isinstance(token, dict):
        token = {}
    checks: dict[str, bool | None] = {
        "authority_revoked": _authority(payload, token, "mintAuthority"),
        "freeze_revoked": _authority(payload, token, "freezeAuthority"),
        "not_rugged": not payload["rugged"] if isinstance(payload.get("rugged"), bool) else None,
        "contract_controls": None,
        "sellability": None,
        "deployer_history": None,
    }
    score = _optional_decimal(payload.get("score_normalised"), "risk score", maximum=Decimal(100))
    checks["risk_score"] = score <= Decimal(str(max_risk_score)) if score is not None else None
    risks = payload.get("risks")
    checks["no_danger_flags"] = None
    warnings = []
    if isinstance(risks, list):
        if all(isinstance(r, dict) and r.get("level") in ("info", "warn", "warning", "danger") for r in risks):
            checks["no_danger_flags"] = not any(r["level"] == "danger" for r in risks)
            warnings = [str(r.get("name", "unnamed risk"))[:200] for r in risks]
    markets = payload.get("markets")
    pool_id, lp_pct = None, None
    if isinstance(markets, list) and markets:
        usable = []
        for market in markets:
            if not isinstance(market, dict) or not isinstance(market.get("lp"), dict):
                continue
            lp = market["lp"]
            base = _optional_decimal(lp.get("baseUSD"), "pool baseUSD")
            quote = _optional_decimal(lp.get("quoteUSD"), "pool quoteUSD")
            if base is not None and quote is not None:
                usable.append((base + quote, market))
        if len(usable) == len(markets):
            market = max(usable, key=lambda m: m[0])[1]
            pool_id = market.get("pubkey") or market.get("address")
            if isinstance(pool_id, str) and pool_id:
                lp_pct = _optional_decimal(market["lp"].get("lpLockedPct"), "LP locked percentage", maximum=Decimal(100))
    checks["lp_locked"] = lp_pct >= Decimal(str(min_lp_locked_pct)) if lp_pct is not None else None
    checks["liquidity_controls"] = checks["lp_locked"]
    known = payload.get("knownAccounts") if isinstance(payload.get("knownAccounts"), dict) else {}
    raw_holders = payload.get("topHolders")
    top10 = None
    filtered = []
    if isinstance(raw_holders, list) and raw_holders:
        for holder in raw_holders:
            if not isinstance(holder, dict):
                raise SourceError("invalid holder record")
            tag = known.get(holder.get("address")) or known.get(holder.get("owner")) or {}
            if isinstance(tag, dict) and str(tag.get("type", "")).upper() in ("AMM", "LOCKER"):
                continue
            pct = _optional_decimal(holder.get("pct"), "holder pct", maximum=Decimal(100))
            filtered.append((holder, pct))
        total = payload.get("totalHolders")
        complete = len(filtered) >= 10 or (isinstance(total, int) and not isinstance(total, bool) and total == len(raw_holders))
        if filtered and complete and all(pct is not None for _, pct in filtered):
            top10 = sum(sorted((pct for _, pct in filtered), reverse=True)[:10], Decimal(0))
            if top10 > 100:
                raise SourceError("holder percentages exceed total supply")
    checks["holders_distributed"] = top10 <= Decimal(str(max_top10_pct)) if top10 is not None else None
    checks["holder_distribution"] = checks["holders_distributed"]
    supply = _optional_decimal(token.get("supply"), "supply")
    creator_balance = _optional_decimal(payload.get("creatorBalance"), "creator balance")
    creator_pct = None
    if supply is not None and supply > 0 and creator_balance is not None:
        creator_pct = creator_balance / supply * 100
        if creator_pct > 100:
            raise SourceError("creator balance exceeds supply or units incompatible")
    checks["creator_holding"] = creator_pct <= Decimal(str(max_creator_pct)) if creator_pct is not None else None
    graph = payload.get("graphInsidersDetected")
    insider_flags = [h.get("insider") for h, _ in filtered]
    checks["no_insiders"] = None
    if graph is not None:
        graph_count = decimal_value(graph, "insider count")
        if graph_count != graph_count.to_integral_value():
            raise SourceError("insider count must be integer")
        if graph_count > 0 or any(v is True for v in insider_flags):
            checks["no_insiders"] = False
        elif insider_flags and all(isinstance(v, bool) for v in insider_flags):
            checks["no_insiders"] = True
    return SafetyEvidence(asset_id=f"solana:{mint}", observed_at=now, checks=checks,
                          source="rugcheck", details={"report_pool_id": pool_id, "warnings": warnings,
                          "risk_score": str(score) if score is not None else None,
                          "lp_locked_pct": str(lp_pct) if lp_pct is not None else None,
                          "top10_pct": str(top10) if top10 is not None else None,
                          "creator_pct": str(creator_pct) if creator_pct is not None else None,
                          "limitations": "Provider screening only; report retrieval time is not a guarantee of fresh on-chain state. Program controls, deployer history, and executable exit route are unverified."})


class RugcheckClient:
    def __init__(self, transport: JsonTransport | None = None, timeout: float = 15):
        self.transport, self.timeout = transport or request_json, timeout

    def report(self, mint: str, *, now: int) -> SafetyEvidence:
        mint = validate_address(mint)
        payload = self.transport("GET", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", None,
                                 {"Accept": "application/json", "User-Agent": "sniper-paper/0.1"}, self.timeout)
        return normalize_report(mint, payload, now=now)

    def raw_report(self, mint: str) -> dict:
        mint = validate_address(mint)
        return self.transport("GET", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", None,
                              {"Accept": "application/json", "User-Agent": "sniper-paper/0.1"}, self.timeout)


def normalize_quote(token: str, payload: Any, *, now: int, pool_id: str | None = None,
                    chain_id: str = "solana", asset_prefix: str | None = None,
                    buy_cost_bps=Decimal("100"), sell_cost_bps=Decimal("100"),
                    gas_usd=Decimal("0.01")) -> MarketQuote:
    token = validate_address(token, chain_id)
    pairs = payload if isinstance(payload, list) else payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        raise SourceError("DEX Screener pairs must be a list")
    eligible = []
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != chain_id:
            continue
        address = (pair.get("baseToken") or {}).get("address")
        if chain_id != "solana" and isinstance(address, str):
            address = address.lower()
        if address != token or not isinstance(pair.get("pairAddress"), str):
            continue
        if pool_id is not None:
            expected = pool_id if chain_id == "solana" else pool_id.lower()
            actual = pair["pairAddress"] if chain_id == "solana" else pair["pairAddress"].lower()
            if actual != expected:
                continue
        price = _optional_decimal(pair.get("priceUsd"), "price")
        liq = pair.get("liquidity")
        liquidity = _optional_decimal(liq.get("usd"), "liquidity") if isinstance(liq, dict) else None
        if price is not None and price > 0 and liquidity is not None and liquidity > 0:
            eligible.append((liquidity, pair, price))
    if not eligible:
        raise SourceError("no valid matching pool; no substitute quote")
    liquidity, pair, price = max(eligible, key=lambda p: p[0])
    prefix = asset_prefix or ("robinhood" if chain_id == "robinhoodchain" else chain_id)
    result = MarketQuote(asset_id=f"{prefix}:{token}", pool_id=pair["pairAddress"], observed_at=now,
                         price_usd=price, liquidity_usd=liquidity,
                         buy_cost_bps=decimal_value(buy_cost_bps, "buy costs", maximum=Decimal(10000)),
                         sell_cost_bps=decimal_value(sell_cost_bps, "sell costs", maximum=Decimal(10000)),
                         gas_usd=decimal_value(gas_usd, "gas estimate"), executable=False,
                         source="dexscreener:indicative-only:route-unverified")
    result.validate()
    return result


class DexScreenerClient:
    def __init__(self, transport: JsonTransport | None = None, timeout: float = 15,
                 chain_id: str = "solana", asset_prefix: str | None = None):
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", chain_id):
            raise SourceError("invalid chain identifier")
        self.transport, self.timeout = transport or request_json, timeout
        # Preserve the historical CLI alias while using the provider's name.
        self.chain_id = "robinhood" if chain_id == "robinhoodchain" else chain_id
        self.asset_prefix = asset_prefix

    def quote(self, mint: str, *, now: int, pool_id: str | None = None) -> MarketQuote:
        mint = validate_address(mint, self.chain_id)
        payload = self.pairs(mint)
        return normalize_quote(mint, payload, now=now, pool_id=pool_id, chain_id=self.chain_id, asset_prefix=self.asset_prefix)

    def pairs(self, mint: str):
        mint = validate_address(mint, self.chain_id)
        return self.transport("GET", f"https://api.dexscreener.com/tokens/v1/{self.chain_id}/{mint}", None,
                              {"Accept": "application/json", "User-Agent": "sniper-paper/0.1"}, self.timeout)

    def profiles(self, *, limit: int = 5) -> list[str]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
            raise SourceError("profile limit must be 1..25")
        payload = self.transport("GET", "https://api.dexscreener.com/token-profiles/latest/v1", None,
                                 {"Accept": "application/json", "User-Agent": "sniper-paper/0.1"}, self.timeout)
        if not isinstance(payload, list):
            raise SourceError("profiles response must be a list")
        result = []
        for profile in payload:
            if isinstance(profile, dict) and profile.get("chainId") == self.chain_id:
                address = validate_address(profile.get("tokenAddress"), self.chain_id)
                if address not in result:
                    result.append(address)
                if len(result) >= limit:
                    break
        return result
