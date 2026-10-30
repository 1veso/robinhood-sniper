# SNIPER · TENDIES TRACKER

An auditable on-chain research and **paper-trading** system for Robinhood Chain
and Solana. Independent workers collect evidence, evaluate entries, manage
positions and record costs, fills and monitoring gaps in durable SQLite books.
The **TENDIES TRACKER Observation Console** brings those records into one
read-only desktop/browser workspace.

**Current source version: 0.1.23 · status reviewed October 30, 2026.**
This README describes current `main` and the separately authorized reference
installation. Cloning or installing the source does not initialize its accounts,
activate its strategy revisions, enable paid models or authorize real-money trades.

The reference installation runs automatic paper trading. Paid Grok/X and Jev
calls remain disabled. Restricted live-executor code exists, but no live
deployment is enabled or validated by paper results. A profitable edge has not
been established.

## Six views, distinct strategies

| Console view | Purpose and account boundary | Current operating state |
| --- | --- | --- |
| **Radeon** (`robinhood`) | Robinhood Chain V3 buyer-following; its own paper ledger | Active automatic paper trial, parallel entry paths and a bound runner exit ladder |
| **Xenon** (`solana`) | Solana RUG DESK; its own book | Scanning and independent monitoring; required model/evidence gates block new entries and its trial has not started |
| **Neon** (`neon`) | Solana leader following; separate $1,000 virtual account | Automatic source research and monitoring; qualified histories, eligible selection and authorized model evidence remain required |
| **Argon** (`jev`) | Pump/Jev review of the **existing Xenon account**, not another balance | ACTIVE_PAPER admission filter, with entries blocked by disabled models and incomplete factual evidence |
| **Krypton** (`krypton`) | Standalone Solana token screening; separate $1,000 virtual account | Independent scan/watch jobs; hard checks and authorized Grok evidence are required before entry |
| **Helium** (`helium`) | Standalone Solana research team; separate $1,000 virtual account | Krypton's trading baseline plus a bound Jev review of Grok; both paid model gates remain disabled |

Six views do **not** represent six independent bankrolls. Shared adapters and
model-budget accounting do not merge strategy accounts or returns. Krypton and
Helium have no live adapter or Telegram delivery. Other strategies' durable
notification queues remain separate from comparison studies.

Krypton and Helium retain their own rules: $25,000 minimum pool liquidity,
10-minute minimum pool age, largest owner at most 10%, top-ten owners at most
40%, revoked mint/freeze authorities and pinned-pool provider-reported LP
lock/burn evidence. Their all-in position ceiling is 2%, full take profit +50%,
stop −25%, and daily equity-loss halt 5% on the Berlin account day. Radeon's
ladder does not apply to them. Provider assertions are not independent safety
verification; incomplete owner enumeration cannot prove passing concentration.

Guides: [Xenon](docs/RUG_DESK.md), [Neon](docs/NEON_BOT.md),
[Argon](docs/JEV_PROMOTION.md), [Krypton](krypton_bot/README.md),
[Helium](helium_bot/README.md).

## Current Radeon paper rules

The reference account uses **`parallel-whale-v1`**, with either entry path able
to qualify a token:

| Path | Required signal |
| --- | --- |
| A — whale follow | One current reviewed, qualified tracked wallet's independently verified direct funded purchase. The installed choice is `reviewed_tracked`; an empty qualified registry blocks A only. |
| B — two-wallet V3 | Two distinct observed buying recipients in different canonical transactions buying the same token within 900 seconds, across one or more eligible V3 pools. Distinct recipients do not by themselves prove independent controllers. |

Both require at least $10 per observed purchase, current GoPlus coverage,
rejection of known safety failures, at least $30,000 execution-pool liquidity,
fresh size-covered pinned-pool routes and the account's cash, cost and risk
checks. A signal is not a fill. Both signals can be recorded for one shared token
position, together with its actual initiating strategy; this is not two
independent strategy returns.

Re-entry requires full close and new eligible purchases **strictly after that
close**: one for A, two for B. There is no blanket cooldown, open-position count
cap or daily entry count cap in this activated revision. The normal worker still
permits at most one successful entry per cycle and runs on a two-minute schedule.

| Primary account limit | Value |
| --- | --- |
| Virtual starting cash | $1,000 |
| Per-position full-loss allowance | 0.5% of the conservative risk reference |
| Aggregate full-loss allowance | 2% of that reference |
| Daily equity-loss halt | 2% of day-opening equity |
| Nominal ticket ceiling | $25, reduced to fit risk, costs, cash and route coverage |

The risk reference is the smaller of starting equity and current conservative
equity. A $25 ticket setting does not mean a $25 fill: the initial primary
per-position allowance is about $5 **including modeled costs and exit reserves**.
Unresolved holdings retain their basis and consume risk capacity even when their
conservative mark is zero. Sizing does not assume a stop guarantees an exit.

New positions bind **`runner-ladder-v1`** atomically with entry:

| Recorded condition | Bound exit behavior |
| --- | --- |
| +40% before the first profit take | Arm cost-aware break-even protection |
| +100% | Sell 10% of original quantity |
| +300% | Sell 20% of original quantity |
| +600% | Sell 20% of original quantity |
| Remaining 50% | Retain as a runner, subject to the bound exits |
| After the first profit take | Trail at a 30% decline from the recorded high |
| −35% | Hard stop retains precedence |
| Six hours with gain below +20% | Time exit |

Only one profit stage can fill per fresh quote timestamp. Costs, discrete quotes,
gaps and unavailable routes can produce a loss after break-even protection arms;
there is no guaranteed stop or top fill. Older holdings retain their original
bound exits. Missing, stale, wrong-pool or undersized quotes retain holdings;
they never create a fictional sale or justify substituting another pool.

Installing code does not activate these revisions on a new account. The original
strict five-wallet/$50,000 configuration in [`config/paper.toml`](config/paper.toml)
remains a separate reference, including its historical count caps, cooldown and
exits. It is not the current Radeon profile. Account revisions preserve cash,
holdings, history and the original trial deadline; entries stop at that deadline
while exits continue.

Details: [parallel entries](docs/PARALLEL_ENTRIES.md),
[runner exits](docs/RADEON_EXIT_REVIEW.md),
[paper operations](docs/EXPLORATORY_PAPER.md).

## Isolated paper comparisons

The **$25 sizing study** uses two fresh $1,000 accounts with no inherited trades:

| Portfolio | Per-position full-loss ceiling | Aggregate ceiling | Daily loss halt |
| --- | --- | --- | --- |
| Control | 0.5%, initially $5 | 2%, initially $20 | 2% of day-opening equity |
| $25 variant | 2.5%, initially $25 | 10%, initially $100 | 2% of day-opening equity |

Both bind the same current Radeon entry and runner-exit rules, costs and original
deadline. The $25 ceiling includes entry friction, modeled gas and reserved exit
costs; it is not $25 of token notional. Only these isolated portfolios use the
study budgets. The primary account is unchanged.

Each account obtains its own fresh, exact-size buy/sell coverage on the pinned
source pool. Smaller fills are never multiplied into a claimed executable $25
backtest. Intake is conditional on the primary collector's sampled candidates,
account blockers and one-entry-per-cycle stop, so it is not exhaustive market
coverage. Capture, quote and processing times remain explicit. Expired purchase
signals cannot be made current by a fresh safety report.

The prominent comparison panel directly below Radeon's title shows separate
net/realized P&L, modeled costs, conservative equity, observed drawdown, entry and
close counts, freshness and backlog. The full report is at `/radeon-sizing`.
Empty cohorts, missing values and stale reports are labeled explicitly.

Two other studies remain separate: the earlier Radeon baseline/A/B exit
comparison and the Xenon/Argon baseline/full-crew comparison. Their policies,
accounts, seed exclusions and coverage limitations are preserved. Synthetic
acceptance, copied seed trades and selected historical replays are not new
prospective performance. No comparison automatically promotes a winner.

Details: [sizing study](docs/RADEON_SIZING_COMPARISON.md),
[baseline/A/B](docs/PAPER_POLICY_COMPARISON.md),
[evidence crew](docs/GPTHEIST_CREW.md).

## Console and background operation

On an initialized installation, open **http://127.0.0.1:8765/** or the
**TENDIES TRACKER** desktop shortcut. The console provides bot switching,
positions, cost-aware financials, recorded market charts, decisions, evidence,
worker completion and notification status. Neon and Helium have responsive
workspaces with account-bound event/journal recovery.

The console is loopback-only and read-only: it cannot trade, initialize an
account, enable paid access or change risk. Its five-second refresh is a display
cadence, not a five-second market feed. Charts use recorded observations and
retain gaps rather than inventing continuous prices.

Windows Task Scheduler runs the independent workers. The reference installation
uses the same limited Windows user with S4U background execution and boot
recovery, so workers can run without interactive sign-in. Session 0 completions
were verified; a physical reboot/sign-out test was not performed. This scope does
not establish support for encrypted credentials or a live signer. The PC must
remain powered, awake and connected. Actual application completions, provider
results and monitoring gaps matter more than an enabled task.

The archived **v0.1.22 Windows installer was not rebuilt for later main-branch
changes**, including the newest bots, comparisons and panel. Do not assume that
archive contains current source. See [desktop packaging](docs/DESKTOP.md),
[restart recovery](docs/RESTART_RECOVERY.md) and [console operations](docs/CONSOLE.md).

## Try the offline core

Install Python **3.11 or newer**, then from a new checkout:

```bash
git clone https://github.com/1veso/robinhood-sniper.git
cd robinhood-sniper
python run.py demo --html runtime/demo-report.html --csv runtime/demo-trades.csv
python run.py --config config/paper.toml doctor
```

Use `python3` if that is your Python command. The core demonstration and local
configuration check require no keys, network or third-party packages. The demo
uses fabricated observations in an in-memory account and never touches a live
strategy ledger. Its report is implementation evidence, not strategy performance.

The wider application has component-specific dependencies:

| Component | Dependency instructions |
| --- | --- |
| Solana RUG DESK / shared app runtime | [`rug_desk/requirements.txt`](rug_desk/requirements.txt); pinned packaging inputs in [`requirements-lock.txt`](rug_desk/requirements-lock.txt) |
| Read-only Solana crew evidence | [`requirements-crew.txt`](requirements-crew.txt) for public-key parsing and PDA derivation |
| Optional Krypton/Helium `.env` loading | [`requirements-krypton.txt`](requirements-krypton.txt) |
| Optional executor cryptography | [`requirements-executor.txt`](requirements-executor.txt); installing libraries does not provision or authorize a signer |

Use the application interpreter and the relevant dependency sets for the full
offline suite (`python run_tests.py`), including timezone data on Windows.
Optional signing checks may require their separate dependencies. Synthetic
acceptance roots remain isolated from every strategy account. Follow each bot's
guide for explicit initialization and scheduling; opening the console does not
create an account or start a trial.

Useful inspection commands from the initialized application folder:

```bash
python run.py --help
python radeon_sizing.py status
python krypton_bot.py status
python helium_bot.py status
```

The legacy `run.py` reference-account commands are documented in
[Operations](docs/OPERATIONS.md). Do not use a reference configuration or an
old activation command to overwrite an existing account's bound policy.

## Execution preparation and evidence limits

The intended future live workflow is automatic validation, signing, submission,
exit management, canonical receipt reconciliation and notification under an
approved deployment policy. Routine trades are not intended to require manual
signing or pasted fill confirmations.

Two restricted implementations exist separately from paper workers:

- **Robinhood EVM:** pinned single-pool Router02 validation, exact approvals,
  restricted signing, durable order/recovery journal and canonical finalized
  accounting. The installed executor runs in **shadow mode**, importing candidate
  inputs with no signer and no signing or broadcast calls.
- **Solana:** direct **classic-SPL Raydium CP-Swap with USDC settlement** only,
  with local instruction reconstruction, restricted signing, recovery and
  finalized receipt/cost accounting. Arbitrary Jupiter routes, multi-hop swaps,
  other AMMs and Token-2022 are unsupported. This path is implemented and
  offline-tested, not independently reviewed or funded-mainnet validated.

Neither path is an authorized live deployment. Public quotes and unsuccessful
unfunded-wallet simulations are not successful wallet execution. Independent
review, approved wallet/capital/risk and finality policy, current required model
and safety evidence, actual-wallet validation, restricted signer provisioning
and explicit initial deployment authorization remain prerequisites. A profitable
paper week, installed UI or passing test does not satisfy those gates.

Further limits apply to research and paper results:

- DEX profiles and bounded V3/leader sampling are partial discovery, not a full
  launch feed. No supported CopyFomo machine order/fill bridge is connected.
- Wallet discovery, token-account counts and model labels do not establish
  qualified histories, independent controllers or safe liquidity. Required
  missing facts remain unknown. No production independent-controller source is
  established for the Solana crew.
- Provider-reported LP classifications remain provider assertions even when
  their block identity matches RPC. No model can override baseline hard failures.
- Modeled fees, gas, slippage, impact and Solana rent reserves are assumptions,
  not proof of an executable mainnet lifecycle. Unknown model bills keep complete
  net performance unknown until reconciled.
- An unavailable mark can be conservatively valued at zero without selling or
  removing the holding. Stops, backoff and bounded worker schedules do not
  guarantee fills or continuous monitoring.
- Historical runner replays are selected, quote-covered, fixed-entry research;
  they do not establish out-of-sample returns. Review prospective cohorts by
  entry **and** exit revision, including costs, unresolved holdings, drawdown,
  gaps and dependence on the largest winner.

Details: [automatic execution](docs/AUTOMATED_EXECUTION.md),
[EVM operations](docs/EXECUTOR_OPERATIONS.md),
[restricted Solana execution](docs/EXECUTION_RELEASE_0.1.19.md),
[live preparation](docs/LIVE_PREPARATION.md),
[trader signals](docs/TRADER_SIGNALS.md).

## Project map

| Path | Purpose |
| --- | --- |
| `run.py`, `src/sniper/engine.py`, `src/sniper/ledger.py` | Core reference runner, deterministic paper accounting and reports |
| `explore.py`, `src/sniper/parallel_entry.py`, `src/sniper/radeon_exits.py` | Radeon collection/admission and position-bound exits |
| `research.py` | Resumable wallet research, canonical evidence and address exports |
| `radeon_sizing.py`, `src/sniper/sizing_comparison.py` | Separate prospective control/$25 study |
| `rug_desk/`, `neon_bot/` | Xenon and Neon engines, accounts and workers |
| `jev_review.py`, `crew.py` | Argon reviews, evidence coordination and separate crew comparison |
| `krypton_bot.py`, `helium_bot.py` | Independent Solana paper bots |
| `console_server.py`, `console/`, `src/sniper/console.py` | Read-only shared console and APIs |
| `desktop.py`, `scripts/`, `packaging/` | Windows workers, task setup, recovery and packaging |
| `executor.py`, `solana_executor.py` | Separately gated execution preparation and restricted executors |
| `tests/` | Accounting, isolation, evidence, recovery, UI/API and synthetic acceptance checks |
| `runtime/` and bot-specific runtime folders | Local private ledgers, reports and authorization evidence; excluded from Git |

Keep runtime accounts, credentials, private evidence and acceptance data out of
commits. Repository source and tests are not a copy of an operator's account.
Additional guides: [wallet discovery](docs/WALLET_DISCOVERY.md),
[pipeline validation](docs/PIPELINE_VALIDATION.md),
[Neon workspace](docs/NEON_EXPERIENCE.md),
[Helium cockpit](docs/HELIUM_BOT.md#trading-cockpit).
