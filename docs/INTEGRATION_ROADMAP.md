# Selective trading-desk integrations

Reviewed November 6, 2026 against `main` at
`2c97d9c518dc92074d1d4d235962d6d118fe268f`. This document separates upstream
capabilities, our architectural judgment, implementation, and installed operation.

## What already exists

The repository already has separate paper accounts, deterministic risk and cost
rules, SQLite accounting, account-bound evidence, entry/exit workers, replay and
comparison studies, browser cockpits, and a persistent paper supervisor. Replacing
those with a new bot framework would create another execution/accounting authority.
Keep the current engines and their approved policies.

The screenshots list useful building blocks, not a finished profitable system.
None supplies missing token safety proofs, a complete on-chain discovery feed,
historical leader skill, verified buyer-controller independence, or executable
size-specific exit liquidity merely by being installed. Strategies observing or
holding the same tokens do not automatically provide independent evidence.

## Decision on each upstream project

These recommendations are our assessment of fit, not upstream claims about SNIPER.

| Project | Actual role | Fit and decision |
| --- | --- | --- |
| [DuckDB](https://github.com/duckdb/duckdb) | Embedded analytical SQL database | **First integration, implemented here.** Read selected existing ledger/observation tables into a separate derived archive. Keep SQLite authoritative. |
| [QuantStats](https://github.com/ranaroussi/quantstats) | Return statistics, plots and HTML performance reports | **Next candidate.** Add only after validating account-specific, cost-aware equity history and observation gaps. Existing closed-trade P&L and sampled equity are not interchangeable with a complete regular return series. |
| [VectorBT](https://github.com/polakowo/vectorbt) | Vectorized strategy research and parameter sweeps | **Research stage after data coverage.** Use a separate optional environment. Retain our event/route-aware replay for execution verification. Test on held-out periods, retain failed/delisted tokens and record every parameter trial. Candle-only fills cannot verify our on-chain routes. |
| [TA-Lib Python](https://github.com/TA-Lib/ta-lib-python) | Technical indicators | **Only for a specific testable feature.** It does not provide market data or demonstrate predictive value. Require ordered, complete OHLCV and indicator warm-up; missing inputs remain missing. |
| [CCXT](https://github.com/ccxt/ccxt) | Unified exchange market/account/trading adapters | **Conditional exchange-market branch.** Useful for exchange-listed assets or reference data. It does not replace our chain/mint/pool evidence and settlement contracts. Start with read-only public data if a concrete venue need appears; never add a second order owner. |
| [Cryptofeed](https://github.com/bmoscon/cryptofeed) | Normalized exchange WebSocket feeds and recording | **Conditional feed improvement.** Adopt only for measured latency/coverage gaps on supported venues. The upstream README currently requires Python 3.13+, while SNIPER supports 3.11+. Isolate the collector rather than upgrade the whole trading runtime incidentally. |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Complete exchange trading bot, backtesting and dry runs | **Do not embed as another controller.** It overlaps accounting, scheduling, strategy execution, positions and UI. Consider only as a separately accounted exchange-strategy research engine if that scope is later requested. |
| [skfolio](https://github.com/skfolio/skfolio) | Portfolio optimization, risk models and validation | **Defer.** Require sufficiently long, synchronized, net-of-cost return histories first. Shared tokens and correlated strategy signals reduce diversification. Any future allocation advice remains subject to the current hard risk limits. |
| [APScheduler](https://github.com/agronholm/apscheduler) | In-process job scheduling | **No current replacement benefit.** We already persist cadence, supervise child processes, prevent overlaps and enforce trial windows. A scheduler alone is not a process or machine restart mechanism. The upstream v4 branch warns it is pre-release; do not install unpinned development code. |
| [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) | Financial chart renderer | **Optional later UI upgrade.** Our cockpit already charts observations. Add only for a concrete chart interaction improvement. It supplies no price feed. Use this open-source project rather than assuming access to TradingView Advanced Charts; retain required attribution and gap indicators. |

VectorBT's repository license is Apache 2.0 **with Commons Clause**, not plain
Apache 2.0. Review its exact terms before any later commercial redistribution or
hosted product use: [upstream license](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md).
No VectorBT code or dependency is added in this change.

## Stage 1: DuckDB analytical sidecar

`desk_analytics.py` uses the existing SQLite engines and paper supervisor. It does
not initialize trading accounts, enroll/extend trading trials, place orders,
call providers, authorize models, or promote experimental strategies.

- Seven original SQLite account locations plus the eight experimental locations
  are explicit. Argon is reported as Xenon's alias rather than double-counted.
- Original Oxygen uses the article's JSON/CSV files, and comparison accounts have
  specialized reports. Both are explicitly unsupported by this initial adapter;
  their existing operation/reporting continues independently.
- Only named accounting, decision, mark and observation tables are read. Metadata
  keys are allowlisted. API-request tables, credentials, provider caches, arbitrary
  directories and signer stores are excluded.
- A `mode=ro`, `query_only`, `BEGIN` SQLite read sees committed WAL data consistently
  within each source. Sources are collected sequentially, not as one simultaneous
  portfolio valuation. Individual read start/end times are recorded.
- Snapshot IDs bind the source location, account, mode, fixture flag, selected
  schema and every copied row. Repeating unchanged input reuses its snapshot;
  updated/deleted rows produce a new snapshot. This is a research extract, not a
  full database backup or cryptographic verification of market truth.
- DuckDB imports commit as one collection. An interrupted collection rolls back.
  A missing/invalid source has no rows in `latest_records`, even if an older
  successful snapshot exists. Duplicate account identities cannot be presented as
  independent sources. A corrupt source does not suppress other source statuses.
- Money remains the original decimal strings in JSON. Fixture/demo flags remain
  separate; unknown provenance stays `null`. No aggregate P&L or Sharpe ratio is
  invented, and no missing equity history is reconstructed from fills.
- Per-source reads are bounded to 100,000 rows, 32 MB of serialized rows and three
  seconds of read work. Exceeding a bound produces an error, not a partial extract.
  Future scale work must address incremental ingestion/retention before raising
  limits. Changed full snapshots consume disk; monitor archive size. There is no
  automatic deletion of historical evidence in v1.
- DuckDB runs with one thread, a 256 MB engine memory limit and external access
  disabled. Python's bounded input buffers use additional memory. No SQLite
  extension download, additional server, subscription or credentials are needed.
- The optional job runs every 15 minutes through the current supervisor, with a
  90-second process limit and its own overlap lock. It is disabled until explicitly
  enabled, survives entry-window expiry, and never replaces exit workers.

DuckDB's in-process file access has its own locking rules. Query the archive when
the collector is idle; do not run another writer. Use the atomically published
`runtime/analytics/status.json` for continuously displayed status. It is a derived
publication with a timestamp, not the financial authority. References:
[concurrency](https://duckdb.org/docs/1.4/connect/concurrency),
[Python API](https://duckdb.org/docs/1.4/clients/python/overview).

## Install and verify on the actual trading host

Code and isolated fixtures can be tested here. Private installed ledgers, worker
health, uptime and current performance cannot be verified from the Git repository.
Do not describe this PR as a deployed or continuously running desk.

Use the **same Python environment as the existing supervisor**, from the checked
out repository. The dependency is pinned to the 1.4 LTS patch tested in this PR.

```powershell
python -m pip install -r requirements-analytics.txt
python desk_analytics.py collect
python desk_analytics.py report
```

Check each expected source's account ID, mode, provenance, row counts and read
times. Investigate `MISSING`/`ERROR` rather than initializing replacement accounts.
Confirm original source balances, positions and workers remain intact. An empty
checkout correctly reports all fifteen SQLite sources as missing.

After that local check, enable the analytical job:

```powershell
python desk_analytics.py enable
```

An already running old supervisor needs a normal supervisor restart to load its
new job definition; use the installation's existing restart procedure. Keep the
current workers single-owned and check `paper_autopilot.py status` for
`desk-analytics`. A supervisor already running this code notices the enable file
within its 30-second disabled-job polling interval. Enabling this job does not
install or start a supervisor if none exists. To disable future collections:

```powershell
python desk_analytics.py disable
```

Disable prevents future starts; an already running read-only collection can
finish. Standalone `collect` remains an explicit one-off command. The archive and
status remain in ignored `runtime/analytics`; do not commit or publish them.

Example read-only query, when the collector is idle:

```python
from contextlib import closing
from pathlib import Path
from sniper.desk_analytics import open_archive

with closing(open_archive(Path.cwd(), readonly=True)) as db:
    print(db.execute("""
        SELECT source, account_id, fixture, table_name, count(*) AS rows
        FROM latest_records
        GROUP BY source, account_id, fixture, table_name
        ORDER BY source, table_name
    """).fetchall())
```

`latest_records.data` contains the exact selected row as JSON. Some payload
columns themselves contain JSON strings, as in their original SQLite schema;
consumers must parse them explicitly rather than silently changing their meaning.

## Remaining work before hands-off operation can be claimed

1. Commission the code on the actual host, using preserved original accounts;
   observe restart recovery, feed freshness, independent exits and job failures.
2. Add adapters for unsupported sources only if needed for a concrete comparison.
   Verify mark completeness, API bills, observation gaps, cash flows, policy and
   trial revisions before producing QuantStats return series. Never zero-fill or
   forward-fill an outage into a smooth curve. Suppress unsupported statistics.
3. Address the real data/execution bottleneck for each strategy. Exchange candles
   cannot substitute for token evidence, missing CopyFomo order control, or verified
   on-chain exits. Replay and independent forward paper tests must agree on costs.
4. Preserve approvals and time windows. Nitrogen's explicit order approvals mean
   its current policy is not hands-off. The documented common seven-day trial
   ends November 8 at 16:45:02 Europe/Berlin; verify the actual host's manifests.
   This integration neither removes approvals nor renews trading windows.
5. Real-money commissioning is a separate execution-parity, reconciliation and
   authorization task. Neither package installation nor passing tests establishes
   profitable performance or authorizes live trading.
