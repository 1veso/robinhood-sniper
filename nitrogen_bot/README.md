# NITROGEN — Jev Judgment Desk (bot 7)

NITROGEN is an independent **paper-only** strategy in SNIPER / TENDIES TRACKER.
RADON remains bot 8, with its existing account, configuration and strategy intact.
The article's returns, win rate, rejection outcomes and subsecond latency are unverified;
none is an acceptance target.

The implementation is complete for supported inputs. It has **not** been activated as
an operational real-data desk. No account is automatically initialized, no paid model
calls are enabled, and no wallet, signing or live execution path exists.

## Workflow

`SEARCH → RISK → WHALE → JUDGE → STAGED → APPROVE #id → PAPER FILL`

EXIT runs independently. HEAD follows rejected candidates and writes reports.
These are responsibilities of one strategy, not seven replacement application bots.

- SEARCH polls up to 20 recent finalized signatures for each of up to four explicitly
  configured wallets, decoding at most four unseen receipts per scan. A lead requires
  a recorded paid-for purchase, wallet signature, transaction reference, slot and time.
- Coverage is **Solana, direct classic-SPL Raydium CP exact-input swaps settled in
  USDC or wrapped SOL**. The reused `neon_sources.decode_swap` verifies the instruction
  and matching owner token deltas. Transfers, unsupported routers, pump.fun curves,
  other chains and incomplete receipts never become buy confirmations. This is a
  bounded recent sampler, not a complete launch feed or wallet-history reconstruction.
- RISK reuses Krypton's current mint/freeze, holder concentration, minimum liquidity,
  pool-age and pool-specific LP checks. Known hard failure ends the candidate. Unknown
  data blocks admission. Provider LP classification is not independent on-chain proof.
- WHALE requires at least one recent supported purchase by a configured tracked wallet.
  Multiple addresses do not prove controller independence. Independence must be supplied
  separately as sourced reviewed evidence.
- JUDGE sees only allowlisted facts and computed metrics, not Grok prose or a directional
  Grok BUY. Grok is optional upstream evidence research, not an additional entry gate.
- An admitted BUY produces an immutable order binding the account, candidate, exact
  mint/pool, assessment, dossier hash, policy, size and expiry. It creates **no fill**.
- `approve` refreshes public checks and the pinned quote, revalidates the dossier and
  qualifying purchase, then checks current cash, exposure, timing, size and price drift.
  The order transition and fill share one SQLite transaction. Duplicate approval
  returns its existing status without another fill. The read-only console shows commands.

One durable candidate cycle is retained per mint and pinned pool for this account.
New external lead IDs cannot reset WATCH or re-ask a terminal candidate. Changing pools
cannot reset the mint's cycle. Automatic candidate recycling is intentionally absent.

## Exact four-question gate

NITROGEN owns `judge.py` and `nitrogen_judge_v1`; it does not mutate shared Jev globals.
The pinned repository-supported model is **`jev-1.14.0`**, using TypeSafe System One.
All four questions share one factual request, with no assumption that an answer can
read another answer.

A BUY requires all of:

| Condition | API mapping |
|---|---|
| Selected action is buy | `action.choice == "buy"` |
| Buy-label probability ≥ 0.70 | `action.probabilities["buy"]`, not `confidence` |
| Organic yes probability ≥ 0.65 | Noul is `P(yes)` |
| Late = no | `late.noul < 0.5`; a tie fails; no inherited 0.35 cutoff |
| Displayed exit risk ≤ 3 | Retain raw Score 0–4; display `raw + 1`; gate `raw <= 2` without rounding |

Complete distributions, separate confidence fields and token usage are stored.
A first valid WATCH gets exactly one fresh assessment after 1,800 seconds, with RISK,
WHALE and dossier windows refreshed. A second WATCH is terminal PASS. Missing evidence
has no invented answer; timeout, authorization, budget and provider/response failures
have separate statuses and never automatically retry the judgment. Interrupted paid
requests remain unresolved and are not replayed.

The completed five exit levels are **NITROGEN implementation choices**, not supplied
article settings. Every level stands alone in the versioned request:

| Display / raw | Fresh verified size-specific route criteria |
|---|---|
| 1 / 0 | Ticket/capacity ≤ 0.1% and impact ≤ 0.5% |
| 2 / 1 | Ticket/capacity ≤ 0.5%, impact ≤ 1%, and either > 0.1% or > 0.5% respectively |
| 3 / 2 | Ticket/capacity ≤ 1%, impact ≤ 2%, and either > 0.5% or > 1% respectively |
| 4 / 3 | Ticket/capacity ≤ 2%, impact ≤ 3%, and either > 1% or > 2% respectively |
| 5 / 4 | Missing/failed/stale/unverified size-specific route, or ticket/capacity > 2%, or impact > 3% |

Missing mandatory exit evidence blocks admission before a model call. The shared
execution-cost simulator returns `executable=False`; its modeled impact is never
substituted for this verified exit evidence.

## Independent $500 baseline

Public settings live in `nitrogen_bot/config.json` and are bound into the initialized
ledger. Changes require an explicit reviewed migration; running workers never rewrite
them. Entry checks reject edited configuration. Exits retain the ledger's original
configuration even if the public file changes.

Baseline source revision: **334c73b9925b069a4bf9e7ce4cae914154fa493b**.

| Setting | Value and provenance |
|---|---|
| Initial virtual capital | $500, user-requested experiment; never transferred from another bot |
| Position budget | 2% of marked equity, limited by cash, including modeled network fee and rent; Krypton `max_position_pct` |
| Daily loss halt | 5%, Europe/Berlin; Krypton baseline |
| Liquidity / pool age | $25,000 / 10 minutes; Krypton baseline; pool age is not token launch age |
| Largest owner / top ten | 10% / 40%; complete owner coverage required; Krypton baseline |
| Mint / freeze / LP | Disabled / disabled / provider-reported 100% locked for pinned pool; Krypton baseline |
| Open positions / daily entries | 4 / 6; existing `rug_desk/config.py` limits |
| Aggregate exposure | 8% of equity, NITROGEN choice from four 2% position budgets |
| Stop | 35% below entry, existing Solana desk exit baseline |
| Partial takes | At +100%, sell 50% of original quantity; at +200%, sell 25% of original; existing desk baseline |
| Trailing exit | 30% below peak after a partial take; existing desk baseline |
| Time / liquidity exit | After 6h if gain <20%; exit if liquidity drops >40%; existing desk baseline |
| Pending-order lifetime | 120 seconds; NITROGEN choice, bounded by assessment start |
| Approval price drift | At most 5% from decision price; NITROGEN choice |
| Quote / factual age | 60s / 300s; Krypton baseline; native SOL cost mark ≤30s |
| Candidate / whale age | Candidate 2h; qualifying purchase ≤1h; NITROGEN choices |
| Loop cadence | 60s scheduled workers; bounded provider work; actual completion recorded |

Sizing reserves one model request before calculating the ticket. Jev probability or
confidence does not enter the formula. A fresh account's net token buy is slightly
under $10 after modeled network fee and rent. Approval cannot increase the staged size.

Costs are the pinned shared `solana_cost_scenario_v1`: 30 bps DEX fee, 10 bps platform
fee, 100 bps adverse slippage, constant-product impact, 5,000 base +100,000 priority
lamports, and 2,039,280 lamports retained rent capital. Failed attempts are zero in this
baseline; the stress scenario adds 100 bps and two network attempts. These are modeled
assumptions, not verified actual charges. Sales allocate basis proportionally. Closed
wins/losses count a fully closed position, not each partial sale. Missing marks leave
holdings unresolved, and never produce a sale or known complete account return.

## Operating commands

From the repository root:

```bash
# Configure tracked_wallets and reviewed public settings BEFORE initial account creation.
python nitrogen_bot.py init
python nitrogen_bot.py status
python nitrogen_bot.py model-binding
python nitrogen_bot.py model-status

# Import an authorized, account-bound factual supplement (see docs/nitrogen-evidence.md).
python nitrogen_bot.py import --source /path/to/factual-supplement.json
python nitrogen_bot.py scan
python nitrogen_bot.py pending
python nitrogen_bot.py approve --id N-ORDER_ID
python nitrogen_bot.py reject --id N-ORDER_ID

# Independent exits, entry pause and reports.
python nitrogen_bot.py watch
python nitrogen_bot.py pause
python nitrogen_bot.py resume
python nitrogen_bot.py report
python nitrogen_bot.py head --scheduled

# Explicitly reconcile an actual reviewed provider invoice, never an estimate.
python nitrogen_bot.py reconcile --source /path/to/typesafe-invoice.json

# Entirely separate synthetic scenario, without paid requests or wallet activity.
python nitrogen_bot.py demo
```

`init` is idempotent and does not reset capital, trades, or paid authorization.
`status` and the console never initialize an account. `--mode live` is rejected.
The demo writes only under `runtime/nitrogen-demo/<id>` and labels every result synthetic.

Windows workers follow existing desktop conventions:

```powershell
powershell -File scripts/desktop-jobs.ps1 -AppRoot C:\path\to\app -Python C:\path\to\python.exe -NitrogenOnly
```

Independent jobs: `nitrogen-scan`, `nitrogen-watch`, `nitrogen-head`. Initialization is
required before registration. HEAD samples due 24h outcomes each minute and emits the
21:00 **Europe/Berlin** report once per local date. Sunday reports include the week's
buy-probability cohorts. No messaging integration or phone delivery is enabled.

## Paid authorization and setup blockers

Initialization creates `nitrogen_bot/runtime/paid-jev.json` disabled. To authorize
paid requests, the operator supplies the exact output of `model-binding` as `binding`,
sets `enabled` and `operator_authorized` true, records an `approval_reference`, and
sets a positive `daily_budget_usd` bounded by NITROGEN's public cap. This does not grant
order approval. The credential lives in `runtime/typesafe-api-key.txt`; never paste it
in chat or commit it.

NITROGEN reserves its own budget **and** the existing shared Jev budget at
`runtime/jev-review/budget.sqlite3` using the ceiling in
`runtime/jev-review/settings.json`, **and** the installation AI budget at
`rug_desk/runtime/ai-budget.sqlite3`. Existing strategy authorizations are not borrowed.
A zero shared ceiling blocks the call; operator review must set the desired ceiling.
Every reservation carries NITROGEN's account ID. Failed or interrupted paid requests
block later calls until their invoices are reconciled. No retry obtains a preferable answer.

TypeSafe's typed response supplies token usage, not a provider invoice. The documented
$0.042/million input-token rate is shown only as an estimate; $0.01 per request remains
reserved until an actual invoice is reconciled. Unknown billing makes complete net P&L
unknown. The reviewed invoice import must contain exactly:

```json
{"scope":"TYPESAFE_PROVIDER_INVOICE","account_id":"YOUR_NITROGEN_ACCOUNT","request_id":"nitrogen:jev:ASSESSMENT_HASH","provider_invoice_reference":"ACTUAL_PROVIDER_REFERENCE","billed_usd":"0.002"}
```

This example is a schema, **not** an invoice. Reconciliation is bound, idempotent, and
rejects changed charges. Shared limits retain conservative reservations even after
reconciliation.

Real-data operation requires all of:

1. Explicit tracked-wallet configuration and an RPC provider that serves the supported
   finalized transactions and bounded complete token-owner enumeration.
2. Fresh pinned DEX Screener/Rugcheck observations, current SOL price, and the required
   account-bound factual supplements: historical windows, controller review, social
   counts, developer share, launch age, and genuine size-specific sell-route evidence.
3. NITROGEN authorization, TypeSafe credential, and available shared/local ceilings.
4. Running independent workers and explicit operator approval for each staged order.

The implementation does not claim to collect unsupported facts automatically. Source
outages, unsupported activity, missing fields and overdue workers are visible in the
console and database. No paid service or operational worker was enabled during delivery.

## Reports and evaluation

Evening reports reconcile recorded pipeline events, assessments, order transitions,
fills, closed positions, cash, costs and unresolved holdings. Sunday cohorts show
`0.70–0.79`, `0.80–0.89`, `0.90–1.00`: assessed, approved, executed, closed, wins, net
trading P&L and unresolved outcomes. They are **observed outcomes by buy-label
probability**, not a calibration of profit probability. Model costs remain separately
attributed account totals rather than silently allocated to favorable trades.

Valid risk-clean Jev PASS decisions receive a hypothetical decision-time entry under
the same cost model and ticket. HEAD records a matching-pool mark at 24h ±5 minutes.
Missing or stale coverage becomes UNKNOWN; price declines are not labeled confirmed
rugs. Hypothetical outcomes never enter cash, positions, fills or actual win rate.
Recommendations never edit the gate or public configuration.

See [evidence contract](../docs/nitrogen-evidence.md) and
[validation and changed files](../docs/nitrogen-validation.md).
