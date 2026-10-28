# TENDIES TRACKER Observation Console

The shared console is named **TENDIES TRACKER Observation Console**. Its desktop
shortcut is **TENDIES TRACKER**. RUG DESK remains the Solana strategy name; existing
task identifiers, executable paths and account identities remain compatible.

Open **http://127.0.0.1:8765/** on the execution PC. This is a live view of the existing paper accounts, not a demonstration dataset or an order-entry terminal.

The Windows **RUG DESK Console** task runs the installed `console_server.py`, starts at sign-in, restarts after failure and has a five-minute recovery trigger. `IgnoreNew` prevents overlapping service instances. The PC must remain awake, connected and signed in for the paper workers. Closing the browser does not stop those workers. The service binds only to loopback; it is not remotely hosted.

The console uses Windows Normal process priority (Task Scheduler priority 4). Its previous default BelowNormal priority caused repeated HTTP timeouts under the observed host load; raising only this process restored responses. This setting affects interactive read-only service responsiveness, not trading-worker priorities, polling cadences, risk limits or execution latency. The desktop installer sets it for newly created console tasks and preserves existing task definitions. For an existing installation, update only the console task's priority while preserving its action, triggers and other settings.

To run manually from an installed project with its existing runtime:

```powershell
python console_server.py
```

To install/reinstall the Windows service task:

```powershell
.\scripts\install-console.ps1
```

## What the console shows

- Bot selection separates the Robinhood exploratory account from the setup-blocked Solana desk. It never includes either isolated acceptance account.
- Robinhood equity, cash, remaining basis, realized and unrealized P&L use the same Decimal accounting and net-liquidation calculation as the engine. Missing position marks retain holdings and use the engine's conservative valuation, visibly labeled; they do not create sales.
- Open positions show quantities, cost basis, quote age, modeled liquidation value, entry price, stop threshold and take-profit/trailing state. Thresholds use the same cost-adjusted entry-price basis as `PaperEngine.watch`.
- Trade history lists recorded automatic paper fills, their costs, cash movements and realized P&L. No bot transaction has been signed or submitted on-chain.
- Selected-coin charts read stored observations for the entry's pinned pool. Markers use actual recorded **paper fill prices**. Hover distinguishes paper fills from market observations. Range buttons end at the most recent recorded sample, which can be old for a closed position.
- EMA 9/21 and RSI 14 use the last observed close in each five-minute bucket. Missing buckets are not fabricated; indicators restart after gaps over fifteen minutes. Warm-up needs 9/21 closes for EMAs and 15 for RSI. These are descriptive indicators, not extra entry gates. There is no complete tick, volume or exchange OHLC feed.
- Decision reasons, recorded safety checks and their timestamps remain visible. Historical provider passes do not establish current safety, verified controllers or profitable wallets.
- System health reads Windows task status, actual application completion times, notification queue acknowledgements, collector lag, monitoring gaps and policy revisions. Scheduler triggers can be skipped while a preceding worker is still running. Telegram acceptance is not a human read receipt.
- Wallet discovery shows the Robinhood research queue and evidence-only addresses separately. Search by public address or role; the exact searches `qualified` and `unqualified` select the saved qualification status. Recipient, transaction-sender and payer roles do not establish common ownership or independent control. History completeness and qualification are the saved assessment at export time, not permission to trade.
- Selecting an address opens its recent public transaction observations, history assessment, costs when history is complete, and qualification blockers. Each observation shows its own canonical-check timestamp: a local-journal reconciliation may reflect a lagging collector, while an RPC check at capture is historical. Neither is made current by generating a new export. Checks and exports over one hour old are highlighted. Observed pool input is explicitly distinguished from verified direct wallet expenditure; missing amounts remain unknown.
- **Download evidence CSV** downloads the research worker's aggregate public export. **Download displayed address JSON** downloads only the bounded detail visible in the console, including normalized amounts and public funding roles. The UI reads `runtime/research/wallet-evidence-index.json` and its fixed address exports; it does not generate research or make provider requests. Missing exports show an unavailable state rather than an invented zero-candidate result. This view does not combine Robinhood research with the separate Solana account.

The view refreshes every five seconds while visible, without making market-provider calls. The Robinhood worker is scheduled every two minutes; individual cycles may take longer. A five-second browser refresh is **not high-frequency trading or a five-second market feed**. Ledger reads are transactionally consistent; worker JSON and delivery journals have their own timestamps and may advance independently.

Solana and NEON share a read-only position, fill, cost and observation adapter. Open positions are counted by positive remaining quantity. Readiness comes from timestamped worker evidence and the recorded trial, with stale or missing evidence shown explicitly. Without a recorded capital baseline the view reports net paper P&L and leaves cash/equity/return unknown. NEON retains its existing recorded baseline. See [the event/view contract](NEON_EVENT_CONTRACT.md) for durable candidate stages, initial snapshots and the incremental JSON/SSE stream. Existing paid-model restrictions still apply.

## Boundaries and performance

The HTTP server exposes fixed assets, read-only account/chart/health/evidence endpoints, linked Jev assessments, the JSON/SSE `/api/events` stream, and the fixed public `/api/wallet-export.csv` download. Address detail accepts only a valid public EVM address; `download=1` adds an attachment disposition. It does not serve arbitrary runtime paths, raw provider proofs or credentials, accept writes, send Telegram messages, change configuration, call paid APIs, or expose signing controls. Database access is `mode=ro` plus `query_only`, with no writable account/outbox constructors. Host and Origin validation, no CORS, and a restrictive content-security policy protect the loopback service from unrelated web pages. Other programs running as this Windows user can access local resources; localhost is not a multi-user authentication system.

Snapshot requests are cached for three seconds, chart requests for five, and Windows task inspection for thirty. Display limits: latest 500 fills, 200 closed-position records, 80 decisions, 4,000 equity observations and 12,000 selected-pool marks. Aggregate account P&L and decision reason counts are computed from the full ledger. The seven-day experiment at its existing entry limits fits inside these bounds. Historical data remains in the authoritative databases.

The wallet index is fetched only while its tab is selected and cached for five seconds. Its API is capped at 10,000 addresses; the table shows the first 200 matching addresses, prioritizing research candidates and recent observations. Narrow the search to find another address. Address details and their JSON downloads contain at most the newest 200 observations. JSON input is bounded to 16 MB and the aggregate CSV download to 64 MB. Limits are visible; the full journal and exports remain on disk. Export age, observation time and canonical-check time remain separate.

No trading settings, account identity, policy revision, trial deadline or paid-service authorization change when the console starts. Profitable paper results do not authorize a real-money switch. Automatic live signing, submission, recovery and reconciliation remain separate unfinished work.
