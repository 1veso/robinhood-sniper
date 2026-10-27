"""Strict domain records shared by all adapters and the paper engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Mapping


def decimal(value: Any, name: str = "number", *, minimum: Decimal | None = None,
            maximum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return result


def timestamp(value: Any, name: str = "timestamp") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be nonnegative integer UTC epoch seconds")
    return value


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} cannot contain control characters")
    return value


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Cannot serialize nonfinite Decimal")
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def loads_strict(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Nonfinite JSON value rejected: {value}")
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant,
                      object_pairs_hook=unique_object)


def _known(data: Mapping[str, Any], cls: type) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{cls.__name__} must be an object")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} fields: {', '.join(sorted(unknown))}")
    return dict(data)


@dataclass(frozen=True)
class MarketQuote:
    asset_id: str
    pool_id: str
    observed_at: int
    price_usd: Decimal
    liquidity_usd: Decimal
    buy_cost_bps: Decimal = Decimal("100")
    sell_cost_bps: Decimal = Decimal("100")
    gas_usd: Decimal = Decimal("0.02")
    executable: bool = False
    source: str = "unspecified"
    route_max_notional_usd: Decimal | None = None
    route_max_sell_quantity: Decimal | None = None
    route_impact_bps: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("price_usd", "liquidity_usd", "buy_cost_bps", "sell_cost_bps", "gas_usd"):
            object.__setattr__(self, name, decimal(getattr(self, name), name, minimum=Decimal(0)))
        for name in ("route_max_notional_usd", "route_max_sell_quantity", "route_impact_bps"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, decimal(getattr(self, name), name, minimum=Decimal(0)))
        self.validate()

    def validate(self) -> None:
        identifier(self.asset_id, "asset_id")
        identifier(self.pool_id, "pool_id")
        identifier(self.source, "source")
        timestamp(self.observed_at)
        if not isinstance(self.executable, bool):
            raise ValueError("executable must be boolean")
        for name in ("price_usd", "liquidity_usd", "buy_cost_bps", "sell_cost_bps", "gas_usd"):
            decimal(getattr(self, name), name, minimum=Decimal(0))
        if self.buy_cost_bps > 10000 or self.sell_cost_bps > 10000:
            raise ValueError("Costs must be between 0 and 10000 basis points")
        if self.executable and (self.price_usd <= 0 or self.liquidity_usd <= 0):
            raise ValueError("An executable quote needs positive price and liquidity")
        bounds = (self.route_max_notional_usd, self.route_max_sell_quantity, self.route_impact_bps)
        if any(x is not None for x in bounds):
            if any(x is None for x in bounds) or any(decimal(x, minimum=Decimal(0)) < 0 for x in bounds):
                raise ValueError("Route bounds must be complete and nonnegative")
            if self.route_max_notional_usd <= 0 or self.route_max_sell_quantity <= 0:
                raise ValueError("Route size coverage must be positive")

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MarketQuote:
        return cls(**_known(data, cls))


@dataclass(frozen=True)
class SafetyEvidence:
    asset_id: str
    observed_at: int
    checks: dict[str, bool | None]
    source: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.asset_id, "asset_id")
        timestamp(self.observed_at)
        identifier(self.source, "source")
        if not isinstance(self.checks, dict):
            raise ValueError("checks must be an object")
        for name, value in self.checks.items():
            identifier(name, "check name")
            if value is not None and type(value) is not bool:
                raise ValueError(f"Safety check {name} must be true, false, or null")
        if not isinstance(self.details, dict):
            raise ValueError("details must be an object")

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SafetyEvidence:
        return cls(**_known(data, cls))


@dataclass(frozen=True)
class WalletConfirmation:
    wallet: str
    cluster_id: str
    tx_hash: str
    observed_at: int
    spent_usd: Decimal
    verified_swap: bool
    qualified: bool

    def __post_init__(self) -> None:
        for name in ("wallet", "cluster_id", "tx_hash"):
            identifier(getattr(self, name), name)
        timestamp(self.observed_at)
        object.__setattr__(self, "spent_usd", decimal(self.spent_usd, "spent_usd", minimum=Decimal(0)))
        if type(self.verified_swap) is not bool or type(self.qualified) is not bool:
            raise ValueError("verified_swap and qualified must be booleans")

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WalletConfirmation:
        return cls(**_known(data, cls))


@dataclass(frozen=True)
class Candidate:
    event_id: str
    asset_id: str
    symbol: str
    observed_at: int
    confirmations: list[WalletConfirmation]
    safety: SafetyEvidence
    strategy: str = "wallet_follow_v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "asset_id", "symbol", "strategy"):
            identifier(getattr(self, name), name)
        timestamp(self.observed_at)
        if not isinstance(self.confirmations, list) or any(not isinstance(x, WalletConfirmation) for x in self.confirmations):
            raise ValueError("confirmations must be a list of WalletConfirmation")
        if not isinstance(self.safety, SafetyEvidence):
            raise ValueError("safety must be SafetyEvidence")
        if self.safety.asset_id != self.asset_id:
            raise ValueError("Safety evidence belongs to another asset")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        if "decision_at" in self.metadata:
            timestamp(self.metadata["decision_at"], "decision_at")

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Candidate:
        values = _known(data, cls)
        values["confirmations"] = [WalletConfirmation.from_dict(c) for c in values.get("confirmations", [])]
        values["safety"] = SafetyEvidence.from_dict(values["safety"])
        return cls(**values)
