# config.py
# every number on the desk. you retune here and nowhere else.
import os


def env(name, default=None):
    """reads .env so keys never live in code or in the chat."""
    if os.environ.get(name):
        return os.environ[name]
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return default


MODEL = "grok-4.7"           # if the API rejects it, check console.x.ai -> Models
PAPER = True                 # fake fills at real prices. flip after a week of reading logs
RPC_URL = env("RPC_URL", "https://api.mainnet-beta.solana.com")

TICKET_USD = 25              # flat, forever. never sized off the last win
MAX_OPEN = 4
MAX_ENTRIES_PER_DAY = 6
DAILY_LOSS_HALT_USD = 50     # realized -$50 today -> no new entries until tomorrow
COOLDOWN_HOURS = 24          # never re-enter a token you just left

ENTRY = {                    # the door. code only, no model
    "max_risk_score": 40,        # rugcheck score_normalised, higher = worse
    "min_lp_locked_pct": 90,     # deepest pool, locked or burned
    "max_top10_pct": 30,         # top 10 holders, pool vaults and lockers removed
    "max_dev_pct": 5,            # creator wallet share of supply
    "max_insiders": 0,
    "min_liquidity_usd": 30000,
    "max_ticket_share": 0.01,    # your $25 is never more than 1% of the pool
    "min_age_min": 30,
    "max_age_hours": 48,
}
MIN_CONFIDENCE = 0.7         # Grok BUY under this = SKIP

WATCH = {                    # the part that sells before the dev does
    "dev_sell_pct": 20,          # dev wallet down 20% from entry -> out, all of it
    "dev_arm_pct": 0.5,          # dev rule only arms if the dev held 0.5%+ of supply at entry
    "holders_peel_pct": 25,      # entry top 10 combined down 25% -> out
    "holders_gone": 3,           # 3 of the entry top 10 fully emptied -> out
    "liq_drop_pct": 30,          # pool liquidity down 30% from entry -> out
}

PULSE = {                    # Grok reading X while you hold
    "every_min": 60,
    "fading_sells": 0.5,         # first "fading" read sells half
}

EXIT = {                     # plain price rules. they fire last
    "stop_pct": -30,
    "take_pct": 80, "take_frac": 0.5,   # +80% sells half
    "trail_pct": 25,                    # after the take: out 25% below the peak
    "time_stop_hours": 6, "time_stop_min_pct": 15,
}

POSITIONS, TRADES, CHECKS, COSTS, PENDING = (
    "positions.json", "trades.csv", "checks.csv", "costs.csv", "pending.json")
