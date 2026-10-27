# RUG DESK observation console

Open **http://127.0.0.1:8765/** on the execution PC. This is a live view of the existing paper accounts, not a demonstration dataset or an order-entry terminal.

The Windows **RUG DESK Console** task runs the installed `console_server.py`, starts at sign-in, restarts after failure and has a five-minute recovery trigger. `IgnoreNew` prevents overlapping service instances. The PC must remain awake, connected and signed in for the paper workers. Closing the browser does not stop those workers. The service binds only to loopback; it is not remotely hosted.

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

The view refreshes every five seconds while visible, without making market-provider calls. The Robinhood worker is scheduled every two minutes; individual cycles may take longer. A five-second browser refresh is **not high-frequency trading or a five-second market feed**. Ledger reads are transactionally consistent; worker JSON and delivery journals have their own timestamps and may advance independently.

Solana screening and operational health are visible, but its unfunded/disabled paid-X gate still prevents its trial from starting. The empty Solana account is not assigned an invented bankroll. A future funded/active Solana account needs a dedicated position adapter before this console can value its holdings; the view explicitly reports unsupported activity rather than inventing financial figures.

## Boundaries and performance

The HTTP server exposes four fixed assets and three read-only JSON endpoints. It does not serve runtime files, accept writes, send Telegram messages, change configuration, call paid APIs, or expose signing controls. Database access is `mode=ro` plus `query_only`, with no writable account/outbox constructors. Host and Origin validation, no CORS, and a restrictive content-security policy protect the loopback service from unrelated web pages. Other programs running as this Windows user can access local resources; localhost is not a multi-user authentication system.

Snapshot requests are cached for three seconds, chart requests for five, and Windows task inspection for thirty. Display limits: latest 500 fills, 200 closed-position records, 80 decisions, 4,000 equity observations and 12,000 selected-pool marks. Aggregate account P&L and decision reason counts are computed from the full ledger. The seven-day experiment at its existing entry limits fits inside these bounds. Historical data remains in the authoritative databases.

No trading settings, account identity, policy revision, trial deadline or paid-service authorization change when the console starts. Profitable paper results do not authorize a real-money switch. Automatic live signing, submission, recovery and reconciliation remain separate unfinished work.
