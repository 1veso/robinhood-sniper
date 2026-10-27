"""Live Robinhood token screening. Absent fields never become a safety pass."""
from decimal import Decimal
import time

from .evm import address
from .solana import request_json, decimal_value, SourceError
from sniper.models import SafetyEvidence

CONTROL_FLAGS = ("is_proxy", "is_mintable", "can_take_back_ownership", "owner_change_balance",
                 "hidden_owner", "selfdestruct", "external_call", "transfer_pausable",
                 "is_blacklisted", "is_whitelisted", "slippage_modifiable", "personal_slippage_modifiable")
TRADE_FLAGS = ("is_honeypot", "cannot_buy", "cannot_sell_all", "trading_cooldown")


def all_known(values):
    return False if False in values else None if None in values else True


def flag(report, name, expected="0"):
    value = report.get(name)
    return value == expected if type(value) is str and value in ("0", "1") else None


def normalize(token, payload, *, observed_at):
    token = address(token)
    if not isinstance(payload, dict) or payload.get("code") != 1 or not isinstance(payload.get("result"), dict):
        raise SourceError("GoPlus did not confirm a successful token report")
    report = payload["result"].get(token)
    if not isinstance(report, dict) or not report:
        raise SourceError("GoPlus has no report for this token")
    controls = all_known([flag(report, "is_open_source", "1")] + [flag(report, k) for k in CONTROL_FLAGS])
    taxes = []
    for name in ("buy_tax", "sell_tax", "transfer_tax"):
        value = report.get(name)
        taxes.append(None if value in (None, "") else decimal_value(value, name, maximum=Decimal(1)) == 0)
    sellability = all_known([flag(report, "is_in_dex", "1")] + [flag(report, k) for k in TRADE_FLAGS] + taxes)
    # The provider returns the largest holders. Count pool/locker holdings too:
    # this intentionally errs toward rejection rather than trusting arbitrary tags.
    holders = report.get("holders")
    concentration = None
    holder_check = None
    if isinstance(holders, list) and holders:
        percents = []
        seen = set()
        for holder in holders:
            if not isinstance(holder, dict) or address(holder.get("address")) in seen:
                raise SourceError("Malformed or duplicate GoPlus holder")
            seen.add(address(holder["address"]))
            percents.append(decimal_value(holder.get("percent"), "holder share", maximum=Decimal(1)))
        count = report.get("holder_count")
        if isinstance(count, str) and count.isdigit() and len(percents) >= min(10, int(count)):
            concentration = sum(sorted(percents, reverse=True)[:10])
            creator = report.get("creator_percent")
            creator_check = decimal_value(creator, "creator share", maximum=Decimal(1)) <= Decimal("0.05") if creator not in (None, "") else None
            holder_check = all_known([concentration <= Decimal("0.30"), creator_check])
    checks = {"contract_controls": controls, "sellability": sellability,
              "holder_distribution": holder_check, "liquidity_controls": None,
              "deployer_history": False if flag(report, "honeypot_with_same_creator") is False else None}
    return SafetyEvidence(f"eip155:4663/erc20:{token}", observed_at, checks, "goplus:4663:token_security",
        {"creator_address": report.get("creator_address"),
         "top10_share_including_contracts": str(concentration) if concentration is not None else None,
         "missing_checks": [name for name, value in checks.items() if value is None],
         "scope": "Provider screening fetched now; provider supplies no canonical block or analysis timestamp. V3 LP control and deployer history require separate verified evidence.",
         "raw_report": report})


class GoPlusClient:
    def __init__(self, transport=request_json, clock=time.time):
        self.transport, self.clock = transport, clock

    def screen(self, token):
        token = address(token)
        # Timestamp the start so slow responses cannot make old evidence look new.
        observed_at = int(self.clock())
        payload = self.transport("GET", "https://api.gopluslabs.io/api/v1/token_security/4663?contract_addresses=" + token,
                                 None, {"Accept": "application/json", "User-Agent": "SNIPER-Research/0.1"}, 10)
        return normalize(token, payload, observed_at=observed_at)


def combine_review(automatic, reviewed, *, now):
    """A current scoped review may resolve unknowns, never override a failure."""
    if not 0 <= now - automatic.observed_at <= 300:
        raise SourceError("Automatic safety observation is stale")
    if reviewed is None:
        return automatic
    reviewed = SafetyEvidence.from_dict(reviewed)
    if reviewed.asset_id != automatic.asset_id or not 0 <= now - reviewed.observed_at <= 300:
        raise SourceError("Safety review is stale or belongs to another token")
    checks = {}
    for name in set(automatic.checks) | set(reviewed.checks):
        a, b = automatic.checks.get(name), reviewed.checks.get(name)
        checks[name] = False if a is False or b is False else True if a is True or b is True else None
    details = {**automatic.details, "external_review": reviewed.as_dict()}
    if "report_pool_id" in reviewed.details:
        details["report_pool_id"] = reviewed.details["report_pool_id"]
    return SafetyEvidence(automatic.asset_id, min(automatic.observed_at, reviewed.observed_at), checks,
                          "goplus+scoped-review", details)
