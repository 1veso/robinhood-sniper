"""Explicitly unqualified, live-data PAPER experiment. Never a strict-strategy result."""
from dataclasses import replace
from decimal import Decimal

from .engine import PaperEngine
from .models import SafetyEvidence


class ExploratoryEngine(PaperEngine):
    strategy_name = "observed_wallet_exploration_v1"
    engine_mode = "exploratory_paper_v1"
    allow_unqualified_wallets = True
    allow_unknown_safety = True
    confirmation_label = "observed_wallets"

    def __init__(self, ledger, policy):
        if policy.min_independent_wallets != 1:
            raise ValueError("Exploration follows one observed wallet; independence is not asserted")
        super().__init__(ledger, policy)

    def decision_context(self, candidate):
        return {"evaluation_mode": self.engine_mode,
                "wallet_qualification": "unestablished", "wallet_independence": "unestablished",
                "funding_ownership": "unestablished", "spend_basis": candidate.metadata.get("spend_basis"),
                "unknown_safety_checks": [k for k in self.policy.required_safety_checks
                                          if candidate.safety.checks.get(k) is None],
                "strict_strategy_result": False}


def exploratory_policy(strict_policy):
    """Retain all monetary, position, loss, freshness and exit limits."""
    return replace(strict_policy, min_independent_wallets=1)


def pool_adjusted_screen(screen: SafetyEvidence, verified_pool: str) -> SafetyEvidence:
    """Do not count a factory-verified AMM pool as a whale holder.

    The remaining top-ten concentration is bounded above when the provider only
    supplies its largest holders. Ambiguous coverage remains unknown.
    """
    raw = screen.details.get("raw_report", {})
    holders = raw.get("holders", [])
    if not holders or not any(h.get("address", "").lower() == verified_pool for h in holders):
        return screen
    from .adapters.solana import decimal_value
    shares = [(h["address"].lower(), decimal_value(h["percent"], "holder share", maximum=Decimal(1))) for h in holders]
    count = raw.get("holder_count", "")
    if not isinstance(count, str) or not count.isdigit() or int(count) < len(shares):
        return screen
    remaining = sorted((p for a, p in shares if a != verified_pool), reverse=True)
    lower = sum(remaining[:10], Decimal(0))
    missing = min(10 - min(10, len(remaining)), max(0, int(count) - len(shares)))
    upper = lower + min(p for _, p in shares) * missing
    creator = raw.get("creator_percent")
    creator_share = None if creator in (None, "") else decimal_value(creator, "creator share", maximum=Decimal(1))
    verdict = False if lower > Decimal('.30') or (creator_share is not None and creator_share > Decimal('.05')) else (
        True if upper <= Decimal('.30') and creator_share is not None else None)
    return SafetyEvidence(screen.asset_id, screen.observed_at,
        {**screen.checks, "holder_distribution": verdict}, screen.source,
        {**screen.details, "verified_pool_excluded": verified_pool, "top10_nonpool_lower_bound": str(lower),
         "top10_nonpool_upper_bound": str(upper), "report_pool_id": verified_pool})
