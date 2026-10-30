"""Krypton's trading policy plus a separately pinned internal review policy."""
from sniper.krypton.config import load, encode, digest, number, cost_policy
from sniper.jev import MODEL


TEAM_POLICY = {
    'version': 'helium_team_v1',
    'baseline': 'krypton_new_token_v1',
    'model': MODEL,
    'jev_probability_min': 0.70,
    'max_model_attempts_per_candidate': 1,
    'review_max_age_seconds': 60,
    'watch_behavior': 'terminal_no_entry',
    'authority': 'independent_paper_account_only',
}
TEAM_POLICY_HASH = digest(TEAM_POLICY)
ROLES = [
    {'name': 'Scout', 'purpose': 'Collect current observations for the pinned token and pool.'},
    {'name': 'Integrity', 'purpose': 'Apply the inherited deterministic token and market checks.'},
    {'name': 'Analyst', 'purpose': 'Produce the existing cited Grok risk assessment.'},
    {'name': 'Reviewer', 'purpose': 'Use Jev to assess support and contradictions in that assessment.'},
    {'name': 'Chief', 'purpose': 'Hand off Helium candidates and require recorded completion evidence.'},
    {'name': 'Executor', 'purpose': 'Apply the original sizing, costs and independent exits to Helium only.'},
]
