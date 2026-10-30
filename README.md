# SNIPER

An auditable, **paper-only** on-chain trading research system. SNIPER collects
Robinhood Chain events, checks supplied wallet and token evidence, models entries
and exits, and keeps a durable account ledger. Solana screening and optional Grok
research annotation are included as separate adapters.

This is version **0.1.23**: runnable software for testing the proposed strategy.

[Helium — Bot 6](helium_bot/README.md) is a standalone Solana paper trader with
its own account and internal research team. It inherits Krypton's trading rules
unchanged, adds a bound Jev review of the Grok assessment, and has independent
scan/watch workers and a sixth console view. Its [trading cockpit](docs/HELIUM_BOT.md#trading-cockpit)
adapts NEON's responsive presentation to Helium's recorded equity, market prices,
internal pipeline, decisions, positions and committed paper fills. Run
`python helium_bot.py --demo` for the isolated offline lifecycle. Paid model
calls and live execution are not enabled by installation; see
[Helium operations](docs/HELIUM_BOT.md).

[Solana trader selection and signals](docs/TRADER_SIGNALS.md) reuse the history analyser, model budget and paper pipeline. Versioned leader selections fail closed; manual/replayed events deduplicate before entry and never replace position exits. CopyFomo has no verified external approval/order/fill connection here. Paid model calls and live copying remain disabled. Run `python rug_desk/trader_acceptance.py` for the isolated six-case demonstration.

The [automatic executor](docs/EXECUTOR_OPERATIONS.md) adds a separate durable order journal, restricted Router02 signer, exact approvals, recovery, finalized receipt accounting and independent exits. `python executor.py once --mode shadow` observes current candidate inputs with no keys, signing or submissions. Real-money deployment remains disabled pending separate wallet/capital authorization and wallet-specific validation; no per-trade manual approval is part of the intended live workflow.

The [RUG DESK observation console](docs/CONSOLE.md) shows the existing paper account at **http://127.0.0.1:8765/**: positions, cost-aware P&L, recorded pool charts, paper fills, safety evidence and worker/Telegram health. Run `python console_server.py` from the installed project, or install its Windows task with `scripts/install-console.ps1`. It refreshes the view every five seconds, exposes no trading controls and keeps the Solana account separate.

The separate [Solana RUG DESK](docs/RUG_DESK.md) implements the article's ten-module paper desk, pinned-pool checks, transactional book and independent Windows schedules. Its paid Grok/X entry gate is disabled until funded validation and operator authorization. It does not replace the Robinhood paper account. Legacy manual-fill bookkeeping is isolated; the intended live workflow is [automatic execution](docs/AUTOMATED_EXECUTION.md), with a restricted Robinhood EVM implementation now available in disabled/shadow mode; the restricted Solana path is now implemented but requires independent review and funded validation. See [0.1.19 execution preparation](docs/EXECUTION_RELEASE_0.1.19.md).

[Execution costs and live preparation](docs/LIVE_PREPARATION.md) add a versioned Solana cost-aware paper model, net-performance review, unsigned quote/simulation preparation, and finalized actual-fill/fee reconciliation. That Solana reader cannot sign or broadcast. Neither strategy can automatically promote itself to real money.

The [pipeline validation guide](docs/PIPELINE_VALIDATION.md) covers resumable wallet
history, supplemental safety evidence, and the isolated live-quote entry/exit test
with delivered Telegram alerts. Qualification remains evidence-driven.

The active [paper strategy](docs/EXPLORATORY_PAPER.md) can follow two distinct observed buyers within 15 minutes with at least $30,000 pool liquidity. It retains monetary risk limits, requires live route quotes and a current GoPlus rug-screen response, and labels unproven independence and unknown safety evidence. Activate the user-requested revision with `python explore.py --activate-v2`; it preserves the existing paper cash, holdings, exit rules, deadline and historical decisions. The original strict configuration remains a separate research reference.

Live paper setup and the seven-day readiness requirements are described in
[PAPER_TRIAL.md](docs/PAPER_TRIAL.md). A running collector is not evidence of an
active trading trial; the account report shows blocked prerequisites separately.

The separate [two-minute Telegram monitor](docs/TELEGRAM.md) sends conditional
research alerts after current safety checks and a corroborated reviewed-wallet
purchase. It does not place orders or change the paper-entry rules. Telegram
pairing, live-delivery status and required evidence remain explicit.
No profitable edge or live execution performance has been established. The
demonstration uses fabricated prices and wallets and is labeled accordingly.

The Windows service, automatic wallet discovery, receipt-checked historical
accounting and controller-link screening are documented in
[Autonomous research operations](docs/AUTONOMY.md). This update also fixes the
Windows SQLite constructor leak and the Robinhood DEX Screener chain identifier.

[Automatic wallet discovery and address evidence](docs/WALLET_DISCOVERY.md) describes the durable candidate journal, role-aware proofs, per-address JSON/CSV and aggregate CSV/JSONL exports. `python research.py --export-only` refreshes available local evidence without network calls. Discovery alone never qualifies a wallet or changes the active trading policy.

The [article review and integration](docs/ARTICLE_REVIEW.md) records the next
improvements: existing-account protection, exit checks before collection, a
separate Solana research screen, and explicitly enabled X research with source
citations and provider-reported cost accounting.

## Run it now

Install Python **3.11 or newer**. If you received `SNIPER_v0.1.0.zip`, extract it,
open a terminal in the `sniper-v0.1.0` folder, then run:

```bash
python run.py demo --html runtime/demo-report.html --csv runtime/demo-trades.csv
python run_tests.py
python run.py --config config/paper.toml doctor
```

Use `python3` if that is your Python command. On Windows, `py -3` is another
option. There are **no third-party runtime dependencies**. The demonstration,
tests, and configuration check require no account, API key, network connection,
or wallet. Open `runtime/demo-report.html` in a browser to inspect the report.

The demonstration rejects unknown safety evidence and a cluster of related
wallets, opens a synthetic position, rejects a duplicate, takes partial profit,
handles a missing quote without inventing a sale, and processes trailing and
stop exits. Its account exists only in memory; it never uses your persistent
paper ledger.

The archive also includes a saved demo report under `preview/` and a complete
Git bundle. See [Publishing](docs/PUBLISHING.md) to push this exact version to
the target repository using your authenticated terminal. After it is published,
you can obtain the project with `git clone https://github.com/1veso/robinhood-sniper.git`.

## What is implemented

| Component | Behavior |
|---|---|
| Robinhood Chain collector | Chain-ID validation, correct Uniswap V3 event decoding, token decimals, transaction/receipt checks, durable cursors, restart recovery, reorg handling |
| Wallet confirmation pipeline | Five distinct reviewed wallet/controller identities, actual attributable swap flows, minimum spend, freshness, one asset and pool per signal |
| Token screening | Solana Rugcheck normalization and DexScreener market data; missing evidence stays unknown |
| Strategy and risk engine | Deterministic `wallet_follow_v1`, immutable experiment policy, position sizing, aggregate exposure, daily equity halt, cooldown, duplicate protection |
| Paper execution | Modeled fees, gas, price impact, partial exits, trailing exits, time exits, gap-aware stops, conservative unavailable-price treatment |
| Account and reports | SQLite transactions, cash and token quantities, cost basis, realized/unrealized P&L, whole-position statistics, CSV and standalone HTML |
| Trader ranking | Deterministic ranking of supplied, dated wallet statistics; source verification remains an upstream responsibility |
| Grok annotation | Explicit opt-in, bounded requests, persistent cost reservations, strict schemas and source references; advisory labels do not authorize entries |
| Command-line runner | Collection, screening, paper decisions, position monitoring, source-error reports, persistent halt/resume controls |
| Continuous integration | Offline tests and the synthetic scenario on Python 3.11, 3.12, and 3.13 |

## Initial experiment settings

All values are in [`config/paper.toml`](config/paper.toml). They are experimental
settings, not optimized parameters or evidence of expected returns.

| Setting | Default |
|---|---:|
| Synthetic starting account | $1,000 |
| Minimum pool liquidity | **$50,000** |
| Independent wallet confirmations | **5** |
| Aggregate full-loss capital budget at entry | **2%** |
| Full-loss budget for one position | 0.5% |
| Maximum ticket | $25, reduced to fit the budget |
| Maximum open positions | 4 |
| Maximum entries per UTC day | 6 |
| Daily equity loss halt | 2% of the first observed equity that UTC day |
| Re-entry cooldown | 24 hours |
| Quote / token-evidence / wallet-event freshness | 30 / 300 / 900 seconds |
| Stop / first profit take / trailing distance | −35% / +100% sells half / 30% |
| Time exit | 6 hours if the position is below +20% |

The risk reference is the smaller of starting equity and current conservative
equity. Entry sizing includes full principal loss and modeled trading friction;
it does not assume a stop can guarantee an exit price. A $25 ticket therefore
shrinks to about $5 before costs on the default $1,000 account. The 2% rule is an
**entry-time allocation constraint**, not a promise to bound portfolio drawdown,
foregone gains, future costs, or losses in a real market.

Each account pins its complete policy. To test different settings, copy the
configuration and use a new `--db` path. The runner also pins its wallet
selection across restarts. Existing positions keep their original rules.

## Connect the read-only collector

Check the endpoint from the computer where you will run SNIPER:

```bash
python run.py doctor --network
python run.py collect --max-blocks 100
```

For a continuous paper research loop:

```bash
python run.py --db runtime/experiment-001.sqlite3 run --follow --wallets examples/wallets.json
```

`examples/wallets.json` is intentionally empty. Add only actual addresses with
dated qualification and controller-independence evidence. Use
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the input formats and evidence
workflow. A different working HTTPS RPC can be supplied with `--rpc`; its chain
ID must be 4663. Current and archive reads were verified on the operator's
Windows installation on 2026-10-27; check connectivity on each new host.

**Default collection is not sufficient to open a position.** DexScreener prices
are indicative, and simple contract reads do not establish all required safety
properties. The runner reports skips until an external evidence producer
supplies dated safety records and executable-route observations. Setting an
`executable` flag or a safety boolean without supporting evidence does not
validate those claims. SNIPER does not manufacture profitable wallets, safety
audits, or executable liquidity.

The event collector covers the configured V3 factory and pools discovered in
the selected interval. Its swap attribution supports direct, attributable
ERC-20 wallet-to-pool flows. Coverage of complex routes, native-ETH wrapping,
smart-wallet delegation, other AMMs, and all historical launches is not claimed.

## Operator commands

Global options such as `--db` and `--config` go **before** the command.

```bash
# Inspect a persistent account and export it.
python run.py --db runtime/experiment-001.sqlite3 report --html runtime/account.html --csv runtime/fills.csv

# Block new entries. Continue running the loop to process exits.
python run.py --db runtime/experiment-001.sqlite3 halt --reason "operator review"
python run.py --db runtime/experiment-001.sqlite3 resume

# Replay supplied observations in order into a separate experiment.
python run.py --db runtime/synthetic-replay.sqlite3 replay --input examples/synthetic-replay.jsonl

# Display all commands.
python run.py --help
python run.py run --help
```

`resume` clears the manual halt only; it does not override the daily loss halt.
Repeated synthetic replays need a new database because an account cannot move
backward in time. A wall-clock `report` also advances its processing watermark.

No command can sign a transaction, submit a swap, or send a real broker order.
Changing `paper_only` to false is rejected. A future live executor would require
a separate implementation and validation process.

## Reports and interpretation

Every accepted or rejected engine decision records its inputs, policy version,
timestamp and reason. Missing or stale quotes keep token quantities in the
account and value them at zero for risk until valid evidence returns; they do
not create a fictitious realized sale. Partial exits count toward one completed
position, so repeated profit takes do not inflate the win rate.

The fill model uses supplied prices, explicit cost estimates, gas, and a simple
liquidity-impact proxy. It is **not a concentrated-liquidity V3 execution
simulator**. The current reports do not invent a buy-and-hold benchmark, Sharpe
ratio, or tested alpha: `benchmark` is null until a comparable, point-in-time
dataset is provided. Paper P&L alone is not evidence that a strategy is viable.

## Project map

| File or directory | Purpose |
|---|---|
| `run.py` | Cross-platform launcher without installation |
| `run_tests.py` | Offline test launcher |
| `config/paper.toml` | All engine risk and exit settings |
| `src/sniper/models.py`, `policy.py` | Strict input records and validated policy |
| `src/sniper/adapters/` | Read-only EVM, Rugcheck and market-data adapters |
| `src/sniper/pipeline.py` | Durable wallet confirmations and evidence-to-engine flow |
| `src/sniper/engine.py`, `ledger.py` | Deterministic paper account and execution |
| `src/sniper/ranking.py`, `advisor.py` | Wallet ranking and optional Grok annotation |
| `src/sniper/reporting.py` | Escaped standalone HTML report |
| `tests/` | Fault, accounting, schema and integration checks |
| `runtime/` | Local observations, account databases and reports; ignored by Git |

Detailed guides: [Operations](docs/OPERATIONS.md), [EVM adapter](docs/EVM.md),
[Solana and AI adapters](docs/SOLANA.md), [Validation and remaining work](docs/VALIDATION.md).
# Windows desktop installer

The per-user Windows package bundles Python, a native launcher, background paper
workers and the browser console. See [desktop setup](docs/DESKTOP.md). It preserves
existing trials and ships without credentials or account data. Paid AI and live
execution remain disabled; the installer does not bypass strategy gates.
