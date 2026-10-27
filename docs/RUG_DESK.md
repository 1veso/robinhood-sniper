# Solana RUG DESK

**0.1.12 requirement update:** live trading must be automatic, as specified in [AUTOMATED_EXECUTION.md](AUTOMATED_EXECUTION.md). The article’s manual-signing workflow is superseded. The existing manual-record commands below remain legacy bookkeeping/testing utilities, not the intended live product. Automatic live execution is not implemented or enabled.

**0.1.9 update:** the user subsequently authorized execution-cost accounting. The installed empty paper account now uses the versioned [cost-aware model and live-preparation tools](LIVE_PREPARATION.md). References below to the article's no-cost model describe the original 0.1.8 reproduction; current report labels come from the ledger's active policy. Trading thresholds and `config.py` remain unchanged.

This optional desk implements the supplied Solana article in `rug_desk/`, with exactly its ten Python modules. It is separate from the Robinhood wallet-following accounts: different chain, signals, sizing, exits and cost assumptions. Combining their P&L would not measure either strategy.

## What changed from the previous integration

The earlier Solana integration only screened tokens and optionally annotated X evidence. This desk adds a position book, entry pipeline, two-step exit ladder, manual-fill recording, independent scanner/watcher, Berlin-day reporting and durable Telegram outbox. The installed supervisor delegates Solana work to this desk when `runtime/rug-desk-managed.json` exists. Historical seen-once screening records are imported during deployment; they are not silently retried.

`config.py` was initialized with the article's settings. Its values and account mode are pinned to the ledger. Later configuration changes require the operator's approval and an explicit account migration; a mismatch blocks startup. No tool in this desk connects a wallet, signs, broadcasts swaps, or moves funds.

## Environment and commands

Verified host: the user's Windows PC, Python 3.12, Windows `W. Europe Standard Time` (Europe/Berlin civil time). Keep the PC awake, online and signed in. Project installation:

`C:\Users\User\Downloads\sniper-v0.1.0\rug_desk`

From the parent application folder in PowerShell:

```powershell
python -m venv rug_desk/.venv
& .\rug_desk\.venv\Scripts\python.exe -m pip install -r rug_desk/requirements.txt
& .\rug_desk\.venv\Scripts\python.exe rug_desk/book.py --initialize
& .\rug_desk\.venv\Scripts\python.exe rug_desk/check.py ACTUAL_SOLANA_MINT
& .\rug_desk\.venv\Scripts\python.exe rug_desk/watch.py --send
& .\rug_desk\.venv\Scripts\python.exe rug_desk/report.py
& .\scripts\install-rug-desk.ps1
```

`--initialize` never resets an account. A missing established database/marker, identity mismatch or corrupt database is an error. Restore a matching backup; do not delete the marker to obtain fresh capacity. The core Robinhood engine remains standard-library-only; this optional desk adds timezone data for Windows and the requested Requests environment. Use the desk venv for the complete test suite.

Windows Task Scheduler uses the installed venv's `pythonw.exe` and absolute paths:

| Task | Cadence | Overlap policy |
|---|---|---|
| RUG DESK Watch | 10 minutes | IgnoreNew + OS lock |
| RUG DESK Scan | 30 minutes | IgnoreNew + OS lock |
| RUG DESK Report | 21:30 Europe/Berlin daily | IgnoreNew + OS lock |

The watcher is a separate process from the scanner. Network requests occur outside short ledger transactions. The OS releases process locks on crash. SQLite run records retain starts, finishes, status and errors; the report estimates missed intervals. Interrupted executions remain visible. Task timeouts terminate stalled jobs. Hourly Codex supervision is not a substitute for these jobs.

## Accounting and rules

The authoritative journal is `rug_desk/runtime/book.sqlite3`. `positions.json`, `trades.csv` and `checks.csv` are atomically regenerated views. Position changes, fills and alert events commit together. Lost exports are recoverable; a lost ledger is not treated as an empty account. Final entry limits and entry insertion share one write transaction.

Article constants remain: $25 ticket, four open positions, six entries/day, $50 net realized daily loss halt, 24-hour re-entry cooldown; normalized risk <=40, LP lock/burn >=90%, top-ten concentration <=30%, creator <=5%, insiders=0; liquidity >=$30,000, ticket/liquidity <=1%, age 30 minutes–72 hours; reject 20+ hourly buys with zero sells; Grok BUY confidence >=0.70.

Exit priority: liquidity falls more than 40%; stop at -35%; next ladder take at +100% (50% of original quantity) or +200% (25% of original quantity); 30% trailing drop after the first take; six-hour exit when gain is below 20%. At most one take-profit step per poll. Peak is tracked from entry. Missing price/pool evidence retains holdings and raises an operational alert. A ten-minute stop check is not an exchange stop order or a guaranteed loss bound.

The daily halt uses realized net P&L on the Europe/Berlin civil day. Unrealized losses do not count. The article's paper model uses refreshed indicative pool prices and excludes fees, gas and slippage; it cannot establish executable profitability. The separate Robinhood exploratory ledger retains its existing route checks and modeled costs.

## Fourteen gates and data semantics

Checks stop at the first failure, including missing evidence. Required null mint/freeze authorities are distinct from absent fields. Numeric values must be finite and correctly typed. Rugcheck's selected largest reported market must have resolvable identity and lock evidence; that exact pool is used by DexScreener, saved at entry and monitored afterward. Unknown schemas are skipped.

Holder records must be ordered, unique by account and reported owner, reconcile amount/percentage with raw supply, and cover ten nonexcluded accounts or an explicitly complete list. Only provider-tagged AMM/LOCKER accounts are excluded. Nonzero creator balance must reconcile with the creator's reported raw account amount; unknown unit mappings are skipped. This does not independently establish all beneficial owners or controllers. Provider reports are evidence, not proof of a rug-free token.

After a validated Grok BUY the same pool is fetched again, every market gate is repeated, and the book limits are rechecked atomically. The scanner retains the article's **seen-once policy**, including outages, full books and too-young pools. It attempts at most five fresh mints from latest token profiles per scan. Latest profiles are not a complete new-pool feed and are not guaranteed new paid launches.

## xAI: wired, paid requests disabled

The existing private key stays in the parent application's `runtime/xai-api-key.txt`. The read-only models check on 2026-10-27 returned **HTTP 403 / CREDITS_REQUIRED**. It made no inference/search request. A saved key alone does not establish funded API access. No recurring paid calls were enabled.

After the user funds xAI and authorizes paid validation, run these separately, reviewing the first result before the second:

```powershell
& .\rug_desk\.venv\Scripts\python.exe rug_desk/grok.py --test-inference --allow-paid
& .\rug_desk\.venv\Scripts\python.exe rug_desk/grok.py --test-x ACTUAL_SOLANA_MINT --allow-paid
```

These test commands do not open positions or enable recurring calls. Basic inference records the returned text and status. X validation additionally requires completed search-tool execution, complete strict JSON, finite confidence in [0,1], all required fields, retrieved X references and verified usage metadata. BUY requires retrieved posts and supporting returned URLs in the judgment. Citation provenance is checked; factual entailment remains a model assessment, retained for review. X content cannot alter configuration or issue tool instructions.

The model remains `grok-4.7`, endpoint `https://api.x.ai/v1/responses`, X Search and `max_turns=2`. Two turns are not a spending cap. Actual billed ticks, input/output tokens, posts fetched and profiles fetched are retained. The local daily reservation limit is $1 (UTC API-budget day), with $0.50 reserved per request. Reservations survive failures; unknown billed costs block further reservations. A reservation cannot enforce the vendor's bill. Set vendor spending limits too. Standard published prices checked 2026-10-27: $2/M input tokens, $6/M output tokens, $5/1,000 X posts, $10/1,000 X profiles, with long-context pricing differences. Use actual billed usage for reporting.

Only after both tests pass and the user approves recurring paid usage, create `rug_desk/runtime/paid-ai.json` with `{"enabled":true,"operator_authorized":true}`. Without both successful test artifacts this flag does not enable requests. This is an operational switch, not permission to modify `config.py` or bypass any filter.

## Alerts, legacy fill records and the seven-day clock

Telegram uses the existing paired bot and chat; token and destination are never copied into source. An account-specific durable outbox deduplicates ledger events. Only Telegram's acknowledged message ID counts as server delivery evidence; it does not prove the user read the message. Ambiguous network delivery is retained as uncertain and is not automatically resent. Missing/replaced notification journals block delivery until reconciled.

For legacy bookkeeping or reconciliation tests, external holdings use a separate account root and `--manual-records`. These commands are not a substitute for the required automatic live executor. Paper mode is not switched to real trading. For example initialize `rug_desk/runtime/manual-account` with `book.py --root PATH --initialize --manual-records`. Record a real fill using `book.py --root PATH --manual-records --confirm-fill PATH_TO_JSON`:

```json
{"fill_id":"actual-unique-fill-id","side":"sell","now":1793100001,"quantity":"6.25","price":"4","fee":"0.01","position_id":"recorded-position-id"}
```

A buy also supplies a `quote` object containing the actual `mint`, `pool`, `price` and observed `liquidity`; its supplied `quantity`, `price` and `fee` describe the actual fill. This is operator bookkeeping, not independent on-chain confirmation. Manual watcher exits only create pending alerts. Holdings, realized P&L and ladder steps remain unchanged until recorded fills. Partial confirmations accumulate against the requested take; repeated fill IDs cannot double-book it. Oversells and conflicting reuse are rejected.

The Solana seven-day clock remains **NOT_STARTED** while funded X validation is absent. `report.py --start-trial` verifies the account's host/provider/persistence/scheduler preflight, recent completed jobs, acknowledged setup alert, paid authorization and successful inference/X tests. It creates one persistent seven-day window; retries do not reset it. The already-running Robinhood trial is unaffected.

At 21:30 review checks/reasons, BUY judgments versus actual entries, open positions, realized P&L, complete-position wins versus profitable partial exits, run gaps/provider failures, retained X evidence and actual costs. The report asks one configuration question without changing the setting. A week tests operation and supplies observations; it does not establish a profitable edge or authorize live trading.

## Verification sources

- [xAI Grok 4.7 model](https://docs.x.ai/developers/models/grok-4.7)
- [xAI X Search](https://docs.x.ai/developers/tools/x-search)
- [xAI structured outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)
- [xAI tool-usage accounting](https://docs.x.ai/developers/tools/tool-usage-details)
- [Rugcheck public API specification](https://api.rugcheck.xyz/swagger/doc.json)
- [DexScreener API reference](https://docs.dexscreener.com/api/reference)

Offline acceptance covers malformed/missing evidence, pool mismatch, invalid Grok responses, stale refresh, concurrent final-slot claims, missing-price holds, exact exit ordering, manual-fill idempotency, ledger loss, Berlin daily limits/DST, seen-once scanning, paid-default-off behavior and complete paper entry/partial/close with notification deduplication.
