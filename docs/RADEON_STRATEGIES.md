# Radeon accounts, entry controls and profit taking

The **Strategies & paper comparisons** link at the top of Radeon opens
`/radeon-strategies`. Select an account to see its own results, entry switches,
and every trade's initiating entry rule, exit policy, revisions, source candidate,
pool, costs and fills. Token addresses distinguish coins that share a symbol.

## Three questions, six existing accounts

| Group | Display name | What it tests |
| --- | --- | --- |
| Primary | Primary Radeon | Whale-follow OR token-wide two-wallet V3 confirmation, sharing one balance and one position per token |
| Exit / re-entry | Original exits · baseline | Original exits with the legacy same-pool entry subset |
| Exit / re-entry | Early protection · former A | Cost-aware break-even protection before the original first take profit |
| Exit / re-entry | Recovery re-entry · former B | After a full stop close, require price above that trade's original stop threshold before re-entry |
| Sizing | $5 risk control | Fresh $1,000 account, 0.5% position / 2% aggregate full-loss ceilings |
| Sizing | $25 risk variant | Fresh $1,000 account, 2.5% position / 10% aggregate full-loss ceilings |

The old experiment labels A/B are **not** the primary whale-follow / two-wallet
entry rules. Both experiments were separately requested research portfolios.
They do not add capital or results to the primary account, and are never promoted
automatically. The sizing study keeps its creation-time runner exit revision;
later primary exit changes do not rewrite its hypothesis.

Whale-follow still requires a current reviewed, qualified tracked wallet and a
new eligible direct funded purchase after full close. Two-wallet confirmation
still requires two eligible distinct buying recipients in distinct canonical
transactions within 900 seconds, both after full close. Enabling both combines
them with OR, not AND or double position sizing. Shared qualifying signals are
shown, while P&L is attributed once to the actual initiator. Earlier policies
remain historical cohorts; they are not relabeled as today's entry rules.

## Measuring results

The account overview separates realized, conservative unrealized and total P&L,
entries, completed trades, wins and still-held positions. All P&L includes the
ledger's modeled costs; costs must not be deducted a second time. The primary
account also shows current-entry-revision results separately from account history.
Trade filters expose entry rule, exit policy and position state.

The exit experiment's 42 copied starting fills (18 trades) are excluded from its
prospective cohort. Its seeded inherited holdings remain visible when requested.
The sizing accounts started without copied trades. A second table matches only
the same source candidate, token and pinned pool completed in every member of
that experiment; it retains each account's actual prices, sizes and costs. Full
portfolio totals and matched-subset results answer different questions.

The read-only report reconciles all trade P&L to each authoritative ledger.
Account reads are separate SQLite snapshots, each with its own timestamp.
Stale/missing marks retain quantity and basis and show conservative unrealized
loss, never a fictional sale. Observed drawdown includes these valuations and is
not a continuous-market drawdown. Source sampling, coverage gaps, sequential
quotes, small samples and outlier concentration remain material limitations.

## Entry switches

Each existing account can pause/resume new entries. The primary account can also
enable/disable either entry path. Existing exits always continue under the
position's bound policy. Experiment hypotheses are fixed; only their new entries
can be paused. Portfolios cannot be merged or used to increase primary risk.

The bounded `/api/radeon-entry-controls` POST is the console's sole write
capability. It requires exact loopback Host/Origin, a same-session anti-CSRF token,
a fixed account key and identity, booleans, and the expected control revision.
The update and audit event commit together in that account's SQLite transaction,
serialized with admissions. Stale concurrent edits are rejected. GET requests
remain read-only and never initialize accounts or run providers.

The final admission transaction rechecks controls. A fill already committed
before a pause is retained and managed normally. Resume requires candidates
observed after the resume watermark; skipped inputs are not replayed. Enabling
does not clear manual/daily halts or relax freshness, qualification, safety,
risk, cash, costs, source binding, or the original deadline. No risk, order,
credential, paid-model, signing or live-control endpoint exists.

## Harvest ladder for new primary positions

The reviewed successor `harvest-ladder-v1` sells fractions of original quantity:

| Trigger above cost-adjusted entry price | Original quantity sold |
| --- | --- |
| +40% | 50% |
| +80% | 25% |
| +160% | 15% |
| Remaining runner | 10% retained |

Cost-aware break-even protection arms at +20% before TP1. After TP1, a 20%
decline from the recorded high closes the remainder. The −35% hard stop keeps
precedence, and the existing six-hour / below +20% time exit remains. Fresh
exact-quantity pinned-pool coverage is still required; one fresh quote can advance
only one TP stage. Discrete quotes, gaps and costs can close below thresholds.

The successor replaces only new primary-position exits after explicit activation.
Existing positions keep their prior bound exits, including inherited unresolved
holdings. The account, entry revision, monetary risk, original deadline, prior
events and experiment manifests remain unchanged. `RUNNER` retains its original
content/hash for old positions and the sizing study.

Activation uses `revise_radeon_exits.py --policy harvest --activate` with the
expected previous exit revision, exact account/source/entry identities and a
review artifact. It does not run a trading cycle. No CLI flag alone authorizes
deployment or a policy change.

The audit used fixed actual entries and recorded size-covered quotes, reconciled
against actual bound-policy outcomes. Earlier profit taking reduced recent
giveback but did not make the recent closed cohort profitable. This selected
historical replay is not an independent holdout or proof of market edge. Future
results must stay segmented by entry **and** exit revision; no automatic risk
increase or promotion follows a favorable result.
