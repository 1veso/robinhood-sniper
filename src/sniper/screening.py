"""Ordered Solana research screens. A passing screen never opens an account."""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
import time
import tomllib

from .adapters.solana import (DexScreenerClient, RugcheckClient, SourceError,
                             decimal_value, normalize_quote, normalize_report, validate_address)

FACTS = ("not_rugged", "authority_revoked", "freeze_revoked", "no_danger_flags",
         "risk_score", "lp_locked", "holders_distributed", "creator_holding", "no_insiders")
MARKET = ("pool_liquidity", "ticket_share", "pool_age", "hourly_sell_activity")


@dataclass(frozen=True)
class ScreenRules:
    max_risk_score: Decimal = Decimal("40")
    min_lp_locked_pct: Decimal = Decimal("90")
    max_top10_pct: Decimal = Decimal("30")
    max_creator_pct: Decimal = Decimal("5")
    min_liquidity_usd: Decimal = Decimal("30000")
    reference_ticket_usd: Decimal = Decimal("25")
    max_ticket_share: Decimal = Decimal("0.01")
    min_age_seconds: int = 1800
    max_age_seconds: int = 259200
    buys_without_sells_threshold: int = 20

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type == "int":
                if type(value) is not int or value < 0:
                    raise ValueError(f"Invalid screen rule: {field.name}")
            else:
                value = decimal_value(value, field.name)
                object.__setattr__(self, field.name, value)
        if self.max_age_seconds < self.min_age_seconds or self.buys_without_sells_threshold < 1:
            raise ValueError("Invalid age/activity thresholds")
        if any(getattr(self, name) > 100 for name in ("max_risk_score", "min_lp_locked_pct", "max_top10_pct", "max_creator_pct")):
            raise ValueError("Percentage thresholds must be in 0..100")
        if not 0 < self.max_ticket_share <= 1 or self.reference_ticket_usd <= 0 or self.min_liquidity_usd <= 0:
            raise ValueError("Invalid reference ticket or liquidity threshold")

    @classmethod
    def from_file(cls, path):
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        if set(data) != {"screen"}:
            raise ValueError("Screen policy requires one [screen] section")
        return cls(**data["screen"])


def screen_solana(mint, *, rules=None, rugcheck=None, market=None, pool_id=None, clock=time.time):
    mint = validate_address(mint)
    rules = rules or ScreenRules()
    rugcheck, market = rugcheck or RugcheckClient(), market or DexScreenerClient()
    result = {"mode": "RESEARCH_ONLY", "chain": "solana", "token": mint,
              "observed_at": int(clock()), "verdict": "SKIP", "entry_eligible": False,
              "deterministic_checks_passed": False, "checks": [], "stage": "facts"}

    def check(name, value):
        status = "PASS" if value is True else "FAIL" if value is False else "UNKNOWN"
        result["checks"].append({"name": name, "status": status})
        if value is not True:
            result["reason"] = name + (":failed" if value is False else ":missing_or_invalid_evidence")
        return value is True

    def finish():
        done = {r["name"] for r in result["checks"]}
        result["checks"].extend({"name": name, "status": "NOT_RUN"} for name in (*FACTS, *MARKET, "x_research") if name not in done)
        result["completed_at"] = int(clock())
        return result

    try:
        safety = normalize_report(mint, rugcheck.raw_report(mint), now=int(clock()),
                                  **{name: getattr(rules, name) for name in
                                     ("max_risk_score", "min_lp_locked_pct", "max_top10_pct", "max_creator_pct")})
        result["safety"] = safety.as_dict()
        for name in FACTS:
            if not check(name, safety.checks.get(name)):
                return finish()
        selected_pool = safety.details.get("report_pool_id")
        if not selected_pool or (pool_id is not None and pool_id != selected_pool):
            result["reason"] = "safety_pool_mismatch_or_unknown"
            return finish()
        result["stage"] = "pool"
        payload = market.pairs(mint)
        now = int(clock())
        quote = normalize_quote(mint, payload, now=now, pool_id=selected_pool)
        pairs = payload if isinstance(payload, list) else payload["pairs"]
        matching = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"
                    and p.get("pairAddress") == selected_pool
                    and isinstance(p.get("baseToken"), dict) and p["baseToken"].get("address") == mint]
        if len(matching) != 1:
            raise SourceError("ambiguous matching pool")
        pair = matching[0]
        result["quote"] = quote.as_dict()
        result["reference_ticket_usd"] = str(rules.reference_ticket_usd)
        if not check("pool_liquidity", quote.liquidity_usd >= rules.min_liquidity_usd):
            return finish()
        if not check("ticket_share", rules.reference_ticket_usd / quote.liquidity_usd <= rules.max_ticket_share):
            return finish()
        born = pair.get("pairCreatedAt")
        age = Decimal(now) - Decimal(born) / 1000 if type(born) is int and 0 < born <= now * 1000 else None
        result["pool_age_seconds"] = str(age) if age is not None else None
        if not check("pool_age", rules.min_age_seconds <= age <= rules.max_age_seconds if age is not None else None):
            return finish()
        txns = pair.get("txns")
        hour = txns.get("h1") if isinstance(txns, dict) else None
        buys, sells = (hour.get("buys"), hour.get("sells")) if isinstance(hour, dict) else (None, None)
        valid = all(type(n) is int and n >= 0 for n in (buys, sells))
        if not check("hourly_sell_activity", not (buys >= rules.buys_without_sells_threshold and sells == 0) if valid else None):
            return finish()
        result["deterministic_checks_passed"] = True
        result["stage"] = "x_research"
        check("x_research", None)
        result["reason"] = "x_research_not_supplied; safety and executable routes remain unverified"
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        # Provider exceptions can contain arbitrary URLs or payload fragments.
        result["reason"] = f"{result['stage']}_source_unavailable_or_invalid:{type(exc).__name__}"
    return finish()
