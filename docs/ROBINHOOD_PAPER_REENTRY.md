# Robinhood paper count-cap and re-entry revision

The October 29 user-authorized `two-wallet-30k-reentry` profile continues the
existing exploratory account and original seven-day deadline. It removes the
four-position count cap, six-entry UTC-day count cap and 24-hour cooldown.
In this paper policy, zero means **no count cap** for `max_open_positions` and
`max_entries_per_day`; zero cooldown enables the post-close evidence rule below.
Other integer policy fields remain strictly positive. Existing profiles and
policy hashes are unchanged.

Re-entry requires the previous position to be fully closed and two qualifying
buying recipients in different canonical transactions, on the same token/pool,
observed strictly after that close and within the existing 900-second window.
The source filters the quorum and the final ledger transaction checks it again.
It never resets source cursors, decisions, seen events or safety-rejection history.
Duplicate signals and adding to an already open token remain rejected.

Current GoPlus coverage, known safety failures, $30,000 liquidity, size-bounded
routes, quote/evidence freshness, cash, fees, 0.5% position full-loss risk,
2% aggregate full-loss risk and 2% daily equity-loss halt still apply. These
monetary constraints may restrict simultaneous positions despite no count cap.
The existing two-minute schedule and one-entry-per-cycle behavior are unchanged;
this is not a claim of high-frequency execution or guaranteed profitable trades.
Paid models and real execution remain disabled. Solana, NEON, Jev and the strict
reference account retain their existing policies.

Entry quotes now refine the affordable actual input within the same monetary
budgets. Separately accounted baseline/A/B comparisons can observe new positions
without changing active exits or the deadline. See
[paper policy comparison](PAPER_POLICY_COMPARISON.md) for bounds and evidence scope.

`explore.activate_reentry(runtime)` requires the existing v2 account and is called
while holding `exploration-controller.lock`. It allows exactly three policy-field
changes, commits the new policy, manifest and `policy_revision` event together,
then publishes JSON views. Repeating after interrupted publication repairs views
without a duplicate revision, changed cash/holdings or a new deadline. Original
fills retain their entry-policy records and must be analyzed in separate cohorts.
`--activate-reentry` is the explicit CLI activation flag; it is never automatic.

## Position routes and operational diagnostics

An existing holding now requests an exact position-sized sell-only quote. A new
buy quote is not an exit prerequisite. It retains factory/pool/chain/block checks,
current valuation, output coverage, modeled costs and original exit thresholds.
The resulting `MarketQuote.route_side="sell"` cannot admit an entry; entries
continue to require the two-sided route. Neither quote signs or broadcasts.

A quoter execution revert is a typed `RouteUnavailable` observation with side,
pool, token, block/hash and input size. Other RPC errors, including HTTP 429,
remain operational errors. Within one cycle, each unchanged position can reuse a
fresh quote for at most 25 seconds. Failed requests retry after 30 seconds and
again in subsequent cycles; an unavailable route is never filled at zero.
Quantity and pool are part of the cache key, so a partial exit changes coverage.

After an RPC HTTP 429, the affected collection or quote path defers further work
for 60 seconds and resumes on the existing schedule. The timestamp survives worker
restarts. Unverified source events remain pending; no cursor is reset and no fill
is inferred. The independent quote provider still checks exits when collection
is rate-limited. Actual rate-limit failures remain visible, not relabeled success.

`cycle_issues` records scoped issues and actual attempted checks; durable
`route_observation_failure` events preserve the evidence. The console distinguishes
operational errors from unavailable position routes, shows retry counts in Health,
and labels unresolved exits as awaiting an executable quote. Conservative $0 marks
remain unrealized risk valuations, not sales or evidence of malicious intent.

The original meat holding at token
`0x5fd25a3506758b867b2a36ac50d58e548cf5f5d4`, pool
`0x34ea9f8a2b6e9e3cc6d88c0984be33a40a9ef733`, was rechecked on October 29.
At block 100702989 the pool had zero active liquidity and 246 raw WETH units;
actual-position and tiny sell quoter calls reverted. Its quantity and $4.925779
basis remain in the ledger. This dated diagnostic is not a future route guarantee.
