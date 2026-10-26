"""Immutable, versionable risk policy. Live execution is intentionally absent."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import tomllib

from .models import decimal, json_safe


@dataclass(frozen=True)
class RiskPolicy:
    paper_only: bool = True
    starting_cash_usd: Decimal = Decimal("1000")
    min_liquidity_usd: Decimal = Decimal("50000")
    min_independent_wallets: int = 5
    max_account_risk_pct: Decimal = Decimal("2")
    per_position_risk_pct: Decimal = Decimal("0.5")
    max_open_positions: int = 4
    ticket_usd: Decimal = Decimal("25")
    max_entries_per_day: int = 6
    daily_loss_halt_pct: Decimal = Decimal("2")
    cooldown_seconds: int = 86400
    max_quote_age_seconds: int = 30
    max_evidence_age_seconds: int = 300
    max_confirmation_age_seconds: int = 900
    min_wallet_spend_usd: Decimal = Decimal("10")
    max_price_impact_bps: Decimal = Decimal("100")
    stop_loss_pct: Decimal = Decimal("35")
    take_profit_pct: Decimal = Decimal("100")
    take_profit_fraction: Decimal = Decimal("0.5")
    trailing_stop_pct: Decimal = Decimal("30")
    time_stop_seconds: int = 21600
    time_stop_min_gain_pct: Decimal = Decimal("20")
    max_api_cost_usd_day: Decimal = Decimal("1")
    required_safety_checks: tuple[str, ...] = (
        "contract_controls", "sellability", "liquidity_controls",
        "holder_distribution", "deployer_history",
    )

    def __post_init__(self) -> None:
        if self.paper_only is not True:
            raise ValueError("This build supports PAPER execution only")
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(f.default, Decimal):
                object.__setattr__(self, f.name, decimal(value, f.name, minimum=Decimal(0)))
            elif type(f.default) is int:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{f.name} must be a positive integer")
        if self.starting_cash_usd <= 0 or self.ticket_usd <= 0 or self.min_liquidity_usd <= 0:
            raise ValueError("Starting cash, ticket and liquidity must be positive")
        for name in ("max_account_risk_pct", "per_position_risk_pct", "daily_loss_halt_pct", "stop_loss_pct", "trailing_stop_pct"):
            if not Decimal(0) < getattr(self, name) <= Decimal(100):
                raise ValueError(f"{name} must be in (0, 100]")
        if self.per_position_risk_pct > self.max_account_risk_pct:
            raise ValueError("Position risk cannot exceed aggregate account risk")
        if not Decimal(0) < self.take_profit_fraction <= Decimal(1):
            raise ValueError("take_profit_fraction must be in (0, 1]")
        if self.max_price_impact_bps > 10000:
            raise ValueError("max_price_impact_bps must be <= 10000")
        if not self.required_safety_checks or len(set(self.required_safety_checks)) != len(self.required_safety_checks):
            raise ValueError("Nonempty unique mandatory safety checks are required")
        if any(not isinstance(x, str) or not x for x in self.required_safety_checks):
            raise ValueError("Safety check names must be nonempty strings")
        object.__setattr__(self, "required_safety_checks", tuple(self.required_safety_checks))

    def as_dict(self) -> dict:
        return json_safe(self)

    @property
    def version(self) -> str:
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @classmethod
    def from_file(cls, path: str | Path) -> RiskPolicy:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        if set(data) != {"risk"}:
            raise ValueError("Policy TOML must contain exactly one [risk] section")
        values = data["risk"]
        allowed = {f.name for f in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown policy fields: {', '.join(sorted(unknown))}")
        return cls(**values)
