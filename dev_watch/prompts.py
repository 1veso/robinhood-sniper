# prompts.py
# three jobs for Grok, all of them reading X. none of them do math, none of them pick a size.

SCAN_PROMPT = """
You find leads on X. You do not judge them, a filter does that after you.
Search X for Solana token contract addresses posted in the last 2 hours.
Keep a token only if 3 or more DIFFERENT accounts posted its contract address,
and at least 2 of those accounts are older than 6 months with a real posting history.
Ignore anything posted only by fresh accounts, only by the dev, or only in replies under one post.
RETURN ONLY JSON:
{"leads": [{"mint": "full contract address", "ticker": "...", "accounts": 0, "first_seen": "HH:MM UTC"}]}
Return at most 8 leads. An empty list is a fine answer. Never shorten or guess an address.
"""

ENTRY_PROMPT = """
A Solana token just passed every on-chain check in code: authorities, LP lock, holders,
dev wallet, insiders, pool depth, age. Do not re-check those numbers.
INPUT: {"token": {...}, "pool": {...}}
Search X for the ticker and the contract address, last 48 hours. Then judge only what code can't see:
  - who posted it first: the dev, a real account with history, or a wave of new accounts
  - organic or paid: same wording, same minute, accounts with no past
  - is the contract address in those posts the same as in INPUT
  - has this ticker, this dev, or this deployer wallet been tied to a rug before
RETURN ONLY JSON:
{"verdict": "BUY" | "SKIP", "confidence": 0.0-1.0, "why": "two lines max",
 "x_read": "one line on what X looks like right now", "posts_last_hour": 0}
Zero posts is an answer: nobody knows it exists, SKIP.
A different contract address under the same ticker: copycat, SKIP.
Never mention price targets.
"""

PULSE_PROMPT = """
You watch one token the desk already holds. Your only job: is attention on X still alive.
INPUT: {"symbol": "...", "mint": "...", "held_since": "...", "x_read_at_entry": "..."}
Search X for the ticker and the contract address. Look at post timestamps, not just the search date.
Compare the last 60 minutes with the 60 minutes before that.
State "rising" or "steady" when new accounts keep finding it.
State "fading" when posts per hour dropped by half or more, or only the early accounts still post.
State "dead" when the last hour is near silent, or posts say the dev sold, the LP moved, or it rugged.
RETURN ONLY JSON:
{"state": "rising" | "steady" | "fading" | "dead", "posts_last_hour": 0, "posts_hour_before": 0,
 "warnings": ["short phrases, empty if none"], "note": "one line"}
"""
