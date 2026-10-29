# Pump / Jev promotion and deployment

Version 0.1.22 adds explicit **SHADOW → ACTIVE PAPER → LIVE READY → LIVE** authority.
There is no minimum shadow duration. Account/code-bound integration acceptance,
then an explicit transition, promotes paper use. Timers, P&L, confidence and a
successful install cannot promote. The user chose the existing RUG DESK Solana
paper account; Robinhood and Neon keep their existing admission policies.

## Prospective paper operation

The existing `scan → check → Book` path remains authoritative. Strict facts,
pinned-pool limits and the approved Grok BUY run first. Only factual fields are
then sent to Jev. Its result must be COMPLETE/BUY with current required evidence.
The existing fresh quote, costs, risk controls and final Book transaction still
run. The transaction rechecks the bound Jev assessment even if a caller bypasses
`check`. A successful entry links the assessment in its stored judgment; replay
cannot add an order or paid call. Confidence never changes the $25 ticket.

In SHADOW, disabling Jev leaves baseline behavior unchanged. After explicit
ACTIVE PAPER promotion, disabling its required filter blocks new RUG DESK entries;
it cannot silently revert the strategy to baseline. Original exits continue.
A missing price creates no sale. Code changes invalidate the paper acceptance
manifest until the new release passes the same integration checks.

Paid Jev and Grok remain disabled by the user's current choice. The opt-in
[GPTHEIST evidence crew](GPTHEIST_CREW.md) now supplies immutable, previously
received observations to the factual builder. It can establish complete positive
classic-SPL holder-owner coverage only when a bounded account snapshot reconciles
the entire minted supply. It supports venue and size-bounded exit calculations
only for independently reviewed direct classic-SPL Raydium CP-Swap/USDC pools.
Provider holder totals with unknown source times, incomplete coverage, mismatched
one-hour endpoints and unreviewed programs remain unknown. No independent
buyer-controller producer is currently established. Fixture enrichment remains
test injection. Therefore an
installed ACTIVE PAPER stage can correctly show **entries BLOCKED**, zero strategy
fills and an unstarted trial. Setup time is not paper performance evidence.

WATCH remains a persisted, one-time research refresh with fresh evidence after
30 minutes. It does not reset the original seen-once policy or re-enter a rejected
mint. Existing cooldowns and risk checks remain authoritative.

## Evidence and operator commands

`runtime/jev-review/assessments.sqlite3` owns append-only `promotion_events` as
well as assessment snapshots. `settings.json` controls external calls only; its
legacy `mode=SHADOW` value grants no execution authority. The promotion journal
is the stage authority. The existing one-minute task retains its legacy name
`Pump Jev Shadow Review`; prospective entries run through the existing RUG DESK
Scan task, not this background review job. All original cadences remain unchanged.

```
python jev_review.py readiness --root <active-app>
python jev_review.py promote --root <active-app> --to ACTIVE_PAPER --evidence <paper-acceptance.json> --paper-account-id <existing-id> --authorization-reference <user-instruction-reference>
```

`readiness.json` and `readiness.md` list every promotion requirement as PASS,
FAIL or NOT VERIFIED with local evidence, hashes, account/code binding and scope.
The board shows the current stage separately from entry status and offers a
read-only JSON download. No browser controls enable spending, signing or trading.
`tests/verify_jev_promotion.py` produces an isolated prospective trace and test
manifests. Its synthetic fills are excluded from every strategy result.

Matched-fill comparison retains identical recorded timing, size, costs and exits.
It does not manufacture baseline outcomes for prospective Jev-rejected candidates:
those require an independently recorded counterfactual price/exit history. The
reported cohort is a subset, not full-portfolio comparative performance. No profit
claim or probability-of-profit calibration follows from action-label probabilities.

The separate `runtime/crew-comparison` baseline/full-crew portfolios extend this
evaluation prospectively using the same source inputs, original Book cost/risk
controls and exits. They preserve separate accounts, actual decision and processing
times, rejected-candidate observations and unresolved bills. They inherit the
source trial gate; initialization does not start a clock or invent a bankroll.
They do not replace the primary account or the existing matched-fill report.

## Separate Solana live preparation

Bot 4 reuses the Solana direct **classic-SPL Raydium CP-Swap / USDC** executor:
independent pool/config/oracle/vault/mint/program checks, local instruction
reconstruction, restricted Ed25519 signing, durable exact-byte submission,
canonical finalized receipts, bounded expiry/recovery and original automatic exits.
Jev evidence is rechecked at preparation, signing and broadcast, with reviewed
operating-policy binding. Exits do not depend on Jev/Grok being enabled.

Solana's real-signature offline suite uses disposable public fixture keys and
synthetic RPC accounts/receipts. It proves software behavior only. EVM acceptance,
unsigned simulation and paper fills cannot satisfy signed Solana chain evidence.
No actual signer has been provisioned and no live account initialized by this release.

The draft `runtime/deployment-review/jev-solana-live-draft.json` has its own UUID.
Its proposed limits are **unapproved**:

| Limit | Proposal |
|---|---|
| Settlement / allocated capital | USDC / 1,000 USDC |
| Order / aggregate exposure | 25 USDC / 100 USDC, plus the existing $25 price-mark check |
| Positions / daily entries | 4 / 6 |
| Daily realized loss gate | 50 USDC; current USD risk includes API costs and network stress |
| Cooldown / slippage cap | 24 hours / 1% |
| Network fee reservation | 250,000 lamports per transaction; 5,000,000 per Berlin day |
| Rent reserve bound | 3,000,000 lamports per newly created token account |
| Original exits and polling | Preserved; no guaranteed stop or terminal-speed claim |

The proposal does not ask the user to fund a wallet. It cannot load a signer or
authorize spending. Deployment requires independent Solana review, reviewed
program upgrades, funded actual-wallet buy/sell validation, signed canonical
chain lifecycle evidence, operational models/data, actual Jev billing
reconciliation, approved capital/risk, local restricted signer provisioning and
a strategy evidence review. No arbitrary multiweek shadow period is imposed.
Jev token usage is an estimate, not a bill: live cost rows remain unknown until
bound provider billing evidence exists, blocking entries without disabling exits.
The shared paper report also includes Jev usage under its account identity;
unknown bills cannot become zero-cost net-profit evidence.

Read-only wallet preparation can run before promotion with
`solana_executor.py --app-root <app> validate-wallet --policy <draft> --read-only-draft`.
It requires an exact reviewed setup pair and actual wallet balances/accounts; it
cannot load a signer or submit. Signed chain lifecycle validation is a separate,
explicitly authorized bounded validation deployment using the existing restricted
executor and an isolated execution journal. It is not enabled by ACTIVE PAPER,
and its validation fills never enter strategy P&L. Initial strategy activation
remains a subsequent authorization, avoiding a need to enable strategy trading
merely to obtain preparation evidence.

LIVE READY requires all applicable evidence to pass, bound to this account, code
and policy. LIVE additionally requires the user's initial deployment authorization.
After that initial activation, ordinary trades are automatic within the reviewed
limits; per-trade approval/manual wallet signing is not the workflow. Changing
the operating limits invalidates the deployment binding. Paper and live balances,
fills, costs and identities remain separate.

Other AMMs, Pump bonding curves, PumpSwap, multi-hop Jupiter routes and Token-2022
remain unsupported by the live executor. The bounded public setup probe cannot
approve a different candidate's pool. No router fallback manufactures coverage.
