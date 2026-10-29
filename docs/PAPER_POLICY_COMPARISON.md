# Radeon entry sizing and prospective paper comparisons

The active re-entry revision remains `two-wallet-30k-reentry`. There is no
position-count cap, daily-entry-count cap or blanket 24-hour cooldown. Monetary
risk, current safety, fresh post-close buyers and executable route requirements
still apply. The entry-sizing correction does not change these policy limits or
the existing stop, partial profit take, trailing stop and six-hour exit.

## Accurate entry route size

`explore.sized_entry_quote` starts from the affordable cash/full-loss risk ceiling,
instead of always requesting the $25 maximum ticket. It uses the actual quoted
base input and token output to refine the intended swap input after modeled
costs. At most three quote attempts share a 25-second deadline. The final engine
transaction rechecks the original risk and price-impact rules. A probe that fails
impact can only inform a smaller probe; it cannot approve an entry. Nonconverging,
stale or invalid evidence cannot fill. The numerical input tolerance is 0.1%; it
does not increase the monetary risk or impact limits. Every successful route
retains its input, coverage, cost evidence and sizing attempts.

For example, an isolated $1,000 test requests $5 initially and refines to
$4.86755550 swap input, with $4.85299651 token-spot notional and $4.99926863
modeled full-loss risk. These are synthetic test values, not actual strategy fills.

## Three isolated portfolios

`python compare_paper.py --initialize` explicitly creates `runtime/paper-comparison`.
It takes one consistent read-only backup of the existing source ledger, clones
that snapshot into three new experimental account identities, and pins their
source UUID, account, policy, observation start and **original trial deadline**.
No extra cash is credited. Existing holdings retain their original exit rules in
all three portfolios. The active account, its history and its policy-revision
events are never rewritten. Repeated initialization refuses to reset an account;
an interrupted initialization must be reviewed, not overwritten.

| Portfolio | New-position behavior |
| --- | --- |
| Baseline | Current re-entry policy and original exits |
| A | Baseline plus cost-aware break-even protection before the first profit take |
| B | Baseline plus recovery above the previous hard-stop threshold before re-entry |

A freezes the modeled original-stop loss at entry, using the recorded entry
basis, actual quantity and entry quote's sale friction, depth stress and gas.
This is an estimate at the stop price, not a promised future route. Protection
arms when a valid, same-pool, quantity-covered quote shows net liquidation profit
at least that large. Before the first partial profit take, a later valid quote
showing net profit at or below zero triggers a full exit. The original hard stop
takes precedence. Gaps, delays and costs can still produce a loss. Arming and
fills persist atomically and survive restart; missing routes leave holdings open.

B applies only after a full close whose reason is `stop_loss`. It requires the
quote price to be **strictly above** that position's saved stop threshold, using
the original engine price basis (cost-adjusted entry fill price). The existing
two post-close buyers, freshness, safety, cash and risk checks still run. Seed
history can supply the threshold only through the matching audited entry fill
and its recorded policy. Missing proof blocks the re-entry. Other exit reasons
keep the baseline behavior. A failed recovery check is re-evaluated on a later
fresh source candidate; old event IDs are not recycled into orders.

## Observation and recovery contract

The existing two-minute worker captures timestamped watch batches and candidates
into a separate SQLite observation tape. No new Windows task or faster scanner
is installed. Source observation time, tape capture time and processing time are
distinct. After the primary account has completed trading, notifications and
report publication, the worker processes each portfolio's pending tape rows.
Stable sequence IDs and payload hashes bind each input. A portfolio commits its
cursor with its fills and decisions, so an interruption rolls them back together.
A restart resumes missing work without duplicate fills. Missing journals,
changed identities, input gaps and changed payloads fail visibly.

The shared intake is conditional on the active collector: only sampled candidates
that reach its engine are available. Its quorum selection, source admission and
one-entry-per-cycle collection stop constrain all three portfolios. The recorded
entry route is sized for the active account; each portfolio rechecks its own
budget and cannot exceed that route's coverage. A smaller divergent position
retains the same conservative quoted cost estimate. This is **not** a complete
independent-market counterfactual or a retrospective reconstruction.

After primary publication, at most two additional sell-only quotes per cycle can
cover the rotating union of divergent experimental holdings. They use the same
pinned route client, existing provider backoff and bounded remaining time, and
cannot approve an entry. Missing, stale, wrong-pool or undersized quotes remain
unresolved. Quote attempts do not imply fills. This bounded coverage, the real
two-minute cadence and source/monitoring gaps remain part of the results.

## Results and exclusions

`runtime/paper-comparison/report.json` reports all portfolios, backlog and scopes.
Per-portfolio metrics include incremental net P&L and modeled costs, realized and
unrealized change, observed maximum drawdown, re-entry P&L grouped by prior exit,
total P&L by token across every re-entry, new-entry counts and closed win-rate
denominators, unresolved holdings and actual observation gaps. Account-to-date
token totals retain inherited losses separately from new-period results.

Later outcomes after protection are sampled hypothetical retention values only
when recorded future quotes cover the full quantity sold on the same pinned
pool. Missing evidence stays unknown. There is no reconstructed continuous path,
invented eventual winner or fictional exit. Quoted pool fees/impact, 100 bps
adverse stress, the additional depth proxy and modeled gas remain explicit.

Comparison portfolios and synthetic acceptance tests are excluded from the
active trial's P&L, its Telegram fill outbox, the other bots and live-readiness
claims. They cannot enable paid models or real execution. No winning variant is
automatically promoted. The shortened prospective sample ends new admissions at
the existing deadline, with exits continuing afterwards; it is not a fresh
seven-day trial.

## Later dual-entry source revisions

The active account can evaluate whale-following and cross-pool two-buyer signals
independently. That does not amend these already initialized comparison
hypotheses, their manifest, seed, monetary policy or original exits. The
comparison accepts a newer source candidate only when its retained two-wallet
signal independently proves two distinct recipients in different transactions
for the selected execution pool, with the original freshness and spend rules.
The recorded canonical check must be current, and the source UUID, chain, token,
pool and proof identities must agree. Pool facts are never copied across pools.

Whale-only signals, confirmations that qualify only across different pools and
missing or mismatched evidence become durable
`entry_strategy_revision_not_comparable` gaps. Their tape cursors still commit;
they produce no comparison fill. Eligible legacy subsets retain their source
signals as provenance, while newer admission selectors are removed from the
comparison candidate. Watch observations and existing exits continue in every
case. This performs no additional provider calls and cannot reconstruct missing
observations. The unchanged shared collector still limits prospective coverage.

The report publishes this compatibility scope alongside the original manifest
scope. These portfolios continue to compare the original exit/re-entry
hypotheses; they are not separate return accounts for the active whale-following
and cross-pool entry strategies, and cannot justify promoting either strategy.
