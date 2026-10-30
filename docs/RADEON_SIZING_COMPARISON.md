# Prospective Radeon size comparison

This user-authorized paper study opens two **fresh $1,000 accounts**, with no
copied fills or inherited positions. It does not change the active Radeon
account, its unresolved holdings, risk limits, entry/exit revisions or deadline.
The earlier baseline/A/B exit comparisons also remain separate.

| Portfolio | Maximum full-loss commitment per position | Aggregate full-loss ceiling | Daily equity-loss halt |
| --- | --- | --- | --- |
| control | 0.5%, initially $5 | 2%, initially $20 | 2% of day-opening equity |
| usd25 | 2.5%, initially $25 | 10%, initially $100 | 2% of day-opening equity |

The $25 ceiling includes entry friction, modeled gas and the future exit
reserve. Actual token notional is lower and shrinks if risk, cash, impact or
route coverage requires it. Sizing retains the existing full-loss formula;
stops never release unresolved cost basis. These deliberately different
position/aggregate risk limits are experimental, not permission to change
Radeon's limits. Both use the same costs, safety gates, monetary checks,
one-entry-per-source-cycle rule, parallel-whale-v1 entries and bound
runner-ladder-v1 exits captured at initialization. Entries stop at the original
Radeon deadline; independent exits continue. There is no new seven-day clock.

Intake starts strictly after the current sequence of the existing prospective
Radeon tape. It contains only sampled candidates reaching the primary engine;
primary blockers, collection limits and stopping after one entry constrain it.
It cannot establish a complete alternative portfolio or recover missed trades.
Each account independently rechecks canonical proofs and current qualified-wallet
evidence, and requests fresh pinned-pool buy/sell coverage at its affordable
size. Saved smaller quotes never support a larger fill. Original source,
capture, quote and processing times remain recorded; sequential quote timing
and differing eligible holdings can produce different decisions.

The worker uses the primary source and tape through read-only SQLite connections.
Input digest and identity checks fail closed. Each account commits its cursor,
decision, exit binding and fill together. Retry after interruption cannot repeat
a fill. Provider failures retain pending input until its evidence expires;
expiry is a recorded gap. Source and local provider backoff block relevant new
requests. At most one candidate per account is attempted per worker run. Exit
quotes precede entries, with up to two holdings per account rotating through a
bounded run; coverage gaps and unresolved marks remain visible. This is bounded
paper monitoring, not continuous prices or guaranteed exit latency.

Fresh safety evidence does not extend a purchase signal's 900-second life. If
all captured purchase proofs have expired before processing, the worker records
`candidate_signal_expired` and advances atomically without RPC/quote requests or
a fill. The original timestamps and any earlier failed attempts remain intact.

`runtime/radeon-sizing/manifest.json` binds the two account IDs, source UUID,
source-tape identity, original deadline and immutable policies. Each subfolder
has its own `ledger.sqlite3`. `status.json` records actual worker completion;
`report.json`/`report.html` report separate net/realized P&L, modeled fill costs,
observed drawdown, open/unresolved positions, cohort denominators and backlog.
Unknown marks are conservative unrealized valuations, never zero-price sales.
Small samples and a single large winner do not establish an edge.

Initialize explicitly once with `python radeon_sizing.py init`. Reinitialization
refuses existing data. `scripts/desktop-jobs.ps1 -SizingOnly` installs only
`RADEON Paper Size Comparison`: same limited Windows user/S4U, one-minute
recurrence, 30-second boot trigger, 110-second child limit and three-minute task
limit. Overlapping recurrences do not run a second instance. No primary schedule
changes. Reports are read-only at `/radeon-sizing` and `/api/radeon-sizing`.

Radeon's main workspace shows a prominent `$25 paper comparison` panel directly
under its account heading. It polls the derived report independently of the main
account request and compares both portfolios' P&L, equity, modeled costs,
observed drawdown and entry/close counts. Empty cohorts are labeled explicitly;
unavailable fields are not converted to zero. Report timestamps, backlog and
stale/disconnected states remain visible. The panel is hidden immediately when
selecting another bot; its full-report link remains available in the panel.

No Telegram delivery, paid models, live signer, broadcast or automatic promotion
exists in this study. Synthetic tests remain outside all production accounts.
The archived installer is not rebuilt by this source installation.
