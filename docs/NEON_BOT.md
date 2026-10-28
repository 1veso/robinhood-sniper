# Neon bot — Evidence-Gated Solana Leader Following

Neon is a third, isolated **paper-only** bot. It reuses the existing trader screen,
Grok integration, strict Rugcheck facts, pinned Market quotes, Book ledger,
execution-cost model, portfolio controls, independent exit watcher and Telegram
outbox. Dots remains the operator. It has no signing, submission or live command.

The supplied article is the trader-copying strategy already integrated in release
0.1.16. Its claimed month of profit is an unverified anecdote, not evidence of our
performance. The new work adds an independent account, automatic research and
restricted on-chain signal collection, background jobs and a third console view.

## Current gates and trial clock

The user explicitly retained the original Grok gate and prohibited paid calls.
Neon therefore starts in `SETUP_BLOCKED`. Its virtual opening cash is **$1,000**;
that balance is local accounting, not money deposited in a mainnet wallet.
Real, complete cost-accounted trader histories and a current validated model
selection are still required. Public headline P&L never qualifies a leader.

The seven-day clock is created once, only after current nonempty leader selection,
authorized and validated model access, current successful leader collection,
known model costs, acknowledged setup notification and recorded operational
acceptance/persistence/scheduled completions. Blocked setup time is excluded.
The fixed deadline stops new entries; existing exits continue. Restarting,
reinstalling or a profitable result cannot reset the deadline or enable live mode.
Another strategy's authorization or trial does not authorize Neon.

Authoritative files under the application installation:

- `neon_bot/runtime/book.sqlite3`: identity, policy, positions, fills, decisions,
  selections, source observations, cursors, trial and monitoring gaps.
- `neon_bot/runtime/account.json`: account identity marker.
- `neon_bot/runtime/status.json`: derived console/status view.
- `neon_bot/runtime/notifications.sqlite3`: independent durable outbox.
- `neon_bot/runtime/paid-ai.json`: disabled; do not enable without authorization.
- `neon_bot/runtime/traders.json`: complete histories in the existing
  `docs/TRADER_SIGNALS.md` format, if real source evidence becomes available.

No private wallet key is needed. Existing API/pairing files are referenced locally;
no credentials are copied into the console, logs or transferable installer.
Any later authorized model calls share RUG DESK's existing daily budget and are
tagged to Neon's account. Unknown billed usage blocks entries. The budget remains
a local accounting control, not a vendor-enforced cap.

## Automatic data connection and its limits

The [independent Fomo API documentation](https://www.fomo-api.com/docs) exposes a
keyless 30-day Solana leaderboard with ten rows. This is a third-party research
source, not an official FOMO/CopyFomo connection. Source identity mappings and
headline performance claims remain unverified. The hourly snapshot discovers
candidate addresses automatically; it does not create qualified histories.

The minute worker watches all currently selected leaders plus one rotating
research candidate. It uses documented Solana
[signature history](https://solana.com/docs/rpc/http/getsignaturesforaddress),
[transactions](https://solana.com/docs/rpc/http/gettransaction) and
[block membership](https://solana.com/docs/rpc/http/getblock), at finalized
commitment. Only a direct classic-SPL Raydium CP-Swap exact-input transaction with
the watched wallet signing, verified owned token-balance changes and USDC/WSOL as
one settlement leg is decoded. Multi-hop, aggregator, Token-2022 and unsupported
instructions are recorded as unsupported. Transfers/gifts are never inferred buys.
The observed pool must equal the pool that passes the existing token checks.

This restricted coverage can miss most of a trader's activity. It is not a
complete trading history, a full-market sniper feed, or high-frequency execution.
Finality and polling introduce latency; stale events are rejected. Native fees
are retained as lamports; missing historic USD fees remain unknown.

Initial collection establishes a baseline without trading old history. At most
three 20-signature pages and four receipt checks per wallet occur in a pass.
Pending transaction work persists across processes. An unavailable receipt/block
keeps the checkpoint; a missing bounded-history checkpoint records a gap and
rebaselines without admitting omitted history. Recorded events and admission are
deduplicated by transaction/wallet/mint/action. Failed/stale/empty model selections
block new entries and never disable the independent watcher.

[CopyFomo's documented Telegram features](https://www.copyfomo.com/docs) do not
establish a supported external machine event/approval/order/confirmed-fill API.
That bridge remains **unconnected**. No automatic copying is armed. A model BUY
decision and a simulated fill are separate database records. Leader sells are
observations, not mirror-exit orders.

## Preserved settings versus the article

| Setting | Neon / approved existing rules | Article |
|---|---|---|
| Ticket / positions | $25 / 4 | $50 / 5 |
| Entries / daily realized loss halt | 6 / $50 | Different / unspecified |
| Token risk score | ≤40 | ≤50 |
| Model / confidence | Existing grok-4.7 / ≥0.70 | grok-4 / ≥0.65 |
| Trader history | Existing transaction-level cost-aware screen | Profile aggregates |
| Liquidity / age | ≥$30,000; 30 minutes–72 hours | Unspecified |
| Exits | −35% stop, +100% half, +200% quarter of original, 30% trailing, original 6-hour/liquidity exits | Mirror, −50%, +100% half |
| Re-entry | Existing 24 hours | 24 hours |
| Fill costs | Existing `solana_cost_scenario_v1` | Execution claims without auditable fills |

The original `rug_desk/config.py` is unchanged. Numerical history qualification
includes ≥20 closed positions, ≥55% wins, no single-token positive-profit share
above 50%, ≥30-minute average holding, net profitability after costs and follower
stress, and nonnegative recent-week results. See `TRADER_SIGNALS.md` for evidence
requirements. Arithmetic stays in code; model output is strictly validated.

## Operating and verification

`python neon_bot.py initialize` is idempotent and creates no trial. `signals` and
`watch` execute one bounded pass; `status` refreshes the derived view. There is no
live argument. Windows tasks `NEON Paper Signals` (one minute) and `NEON Paper
Watch` (ten minutes) also trigger at sign-in, reject overlapping instances and
retry failed task runs. The latter provides the 21:30 Berlin daily summary.
This PC must remain awake, connected and signed in. A trigger is not proof of a
completed run: check application `runs`, desktop result files and source cursors.

The console's Neon panel shows its own cash, net P&L, open quantities/cost basis,
fills, selection, candidate addresses, blockers, worker completions and delivery
acknowledgements. Prices come only from recorded pinned-pool watcher observations;
the five-second UI refresh is not a five-second quote feed. Missing marks are
conservatively zero-valued unresolved holdings, never invented sales.

`sniper.neon_acceptance.run` uses a new isolated synthetic account and checks entry,
missing-risk/unapproved-leader rejection, duplicates, empty selection, separate
decision/fill records, restart persistence and original partial/trailing exits.
It never supplies strategy performance. `tests/test_neon.py` additionally verifies
RPC proof/checkpoint recovery, pool binding, disabled paid access, deadlines,
accounting and read-only console isolation. Runtime `neon-verification.json`
records the installed release's actual results.
