# Local autonomous research service

The corrected application is PAPER ONLY. `supervise.py` runs one paper cycle,
then a bounded wallet-discovery/history cycle, and writes status and an HTML
account report. `install_service.ps1` registers **SNIPER Paper Research** to run
every minute under your signed-in Windows user. It uses a hidden process,
ignores overlapping scheduler invocations, and also takes an OS file lock.
It resumes persisted cursors after restarts. It does not run while this computer
is asleep, powered off, or the user is signed out.
Each paper cycle processes up to 3,000 blocks, in smaller RPC chunks, to catch
up after outages even when a research cycle overlaps the next scheduled minute.
Position exits are checked before collection and entry-source requests. This
prioritizes existing exposure within each cycle; it is not a separate real-time
exit service or an exchange stop order.

The service pins `runtime/ACCOUNT.identity.json` to the account's UUID and uses
`--existing-account` for child runs. A missing, empty or replaced established
database stops the cycle instead of recreating starting cash. Restore the
matching SQLite account, its journal and identity record after a state loss.
Do not delete the identity record to bypass recovery.

```powershell
python supervise.py --once
.\install_service.ps1
Get-ScheduledTaskInfo -TaskName 'SNIPER Paper Research'
```

Without Task Scheduler, `python supervise.py` keeps the process running.
Do not use both modes. The loop and scheduled mode share the same lock.

Reports:

- `runtime/demo-report.html`: isolated synthetic demonstration.
- `runtime/account.html`: current observation/paper account, service state and research progress.
- `runtime/status.json`: last completed cycle; check `updated_at_utc` for freshness.
- `runtime/service.jsonl`: rotating service history, 5 MB plus one prior file.
- `runtime/research/wallet-candidates.csv`: automatically discovered addresses and source transaction hashes.
- `runtime/research/wallet-research.json`: discovery coverage, historical progress, performance results, controller groups and missing evidence.
- `runtime/research/wallet-shortlist.json`: wallets passing the performance screen, still subject to controller review.
- `runtime/research/qualified-wallets.json`: performance-qualified wallets with valid external controller reviews.
- `runtime/research/cohort-BLOCK/history-ADDRESS.json`: normalized, receipt-checked history.
- `runtime/research/cache/`: retained provider/RPC evidence, hashed filenames without credentials.
- `runtime/solana/latest.json`: latest free Solana screening batch.
- `runtime/solana/screens.sqlite3`: seen-once Solana research journal; no positions or trading account.

The Solana profile screen becomes due every 30 minutes and runs at the next
available service cycle. It attempts at most five unseen mints, with a 35-second
soft time budget and 8-second individual API timeouts (55-second child limit).
It never invokes paid AI. A skipped or interrupted mint remains seen; provider
outages are recorded and profile fetching retries at the next due interval.
This is an additional research feed, not the article's separate Solana trading
strategy, 10-minute watcher, or daily message-delivery schedule.

## Providers

The public Robinhood RPC is the default. It currently works on this machine.
For a dedicated endpoint, create a Robinhood Chain MAINNET (4663) app with a
provider listed in the [official connection guide](https://docs.robinhood.com/chain/connecting/).
Save the full HTTPS URL on one line in `runtime/rpc-url.txt`. The service reads
it at each invocation and passes it through the environment, not command-line
arguments. The CLI also accepts `SNIPER_RPC_URL`; diagnostics print only its host.
Historical `eth_call` and transaction-count reads require archive support.
The public endpoint passed current-state checks but returned RPC error -32000
for the frozen 30-day history's state reads on 2026-10-27. An archive-capable
endpoint is therefore required for historical qualification. The research runner
checks archive availability before spending further indexed-history requests.

Optionally save a separate HTTPS endpoint in `runtime/observation-rpc-url.txt`
(or set `SNIPER_OBSERVATION_RPC_URL`) for current-market collection and wallet
discovery. Historical qualification still uses `rpc-url.txt`. This keeps a
historical provider outage from stopping current observations. Each provider is
checked for chain 4663 independently; wrong-chain history blocks qualification.
Research reports list only provider hostnames and explicit pending errors.
The local installation uses the public RPC for observations and Alchemy for
history. If Alchemy returns “ROBINHOOD_MAINNET is not enabled for this app,”
enable Robinhood Mainnet in that app's Networks settings. Verify actual current
and historical requests after enabling it; saving a key alone is insufficient.

Indexed histories use [Blockscout's unified API](https://docs.blockscout.com/devs/blockscout-api-responses-and-routes).
Create a key in [the developer portal](https://dev.blockscout.com/) and save it
on one line in `runtime/blockscout-api-key.txt`, or set `BLOCKSCOUT_API_KEY`.
Requests use an Authorization header. The earlier per-instance explorer routes
return HTTP 403 in this environment. No brokerage or wallet signing key is used.

Historical USD reference series use CoinGecko's public ETH and Global Dollar
endpoints. The previous timestamped price is used, never a later price, with a
two-hour maximum age. These are reference valuations, not historical executable
quotes. Missing prices prevent performance qualification. USDG is not assumed
to be worth exactly one dollar.

Default research limits are 60 uncached requests and 45 seconds per invocation,
plus a small fixed number of discovery-anchor RPC calls. Blockscout is capped at
1,000 requests per UTC day in a persistent counter. Cache hits do not spend
requests. These limits are local request limits, not guarantees of vendor bills.
No paid plan is automatically enabled.

On this Windows machine Python initially lacked an intermediate TLS certificate.
A normal, certificate-verified Windows `Invoke-RestMethod` request populated the
Windows certificate chain; Python then succeeded. Certificate checks were never
disabled. If it recurs, inspect Windows certificate trust and endpoint access.

## Automatic discovery and qualification

Discovery reads the V3 Swap topic across the chain, rather than requiring a
preexisting wallet list or only recently created pools. It samples up to 12
successful transaction senders evenly across each contiguous interval of up to
2,000 blocks. Dense queries retry progressively smaller prefixes down to 250
blocks; the cursor never skips the unprocessed suffix.
Receipts corroborate the sampled events. The sample is a candidate-discovery
mechanism, not a comprehensive market census or profitability ranking. The
bounded discovery universe is saved and its lag is reported.

For candidates, the historical adapter collects normal transactions, ERC-20
transfers and internal transactions over a frozen 30-day window. Pagination
must terminate; a rate-limit response is never interpreted as empty history.
Each transaction/receipt/block is checked against RPC. Outgoing transaction
counts reconcile against account nonces; token flows reconcile against opening
and closing on-chain balances. The asset universe includes lifetime indexed token transfers, so holdings
left untouched throughout the 30-day window cannot disappear from the assessment.
Direct buys and sells require factory membership
and exact whole-transaction token flows. Native-value transactions, complex
routes, unknown opening basis, missing archive data and inconsistent balances
remain qualification gaps. Other protocols and complex routes are currently
unsupported; the system does not claim to reconstruct them.

FIFO bookkeeping includes actual receipt gas, approvals and failed-transaction
overhead, partial exits, remaining basis and full-position outcomes. Pool fees
are already included in the observed net token flows. Incoming airdrops and
transfers never become zero-basis trading profits. For qualification, open
inventory is stress-marked at zero and an additional 100 bps of hypothetical
follower friction is charged on each side's turnover. This is a conservative
screen, not a simulated executable follower route.

The performance screen requires at least 20 fully closed positions, 55% wins,
at most 50% of positive token-level profit in one token, average holding time of
at least 30 minutes, nonnegative last-week realized P&L after open-basis stress,
and positive total P&L after costs and open-basis/follower stress. Failed screens
and missing evidence remain visible. These are experimental rules, not established
predictors. Unlike the old optional `rank` command's three-leader display limit,
the research shortlist is not limited to three; the entry policy still needs
five reviewed controller groups.

History work resumes from cached responses in a rotating queue. Daily historical
cohorts retain separate cutoffs and earlier results. Large histories may take many
cycles; no record becomes complete merely because a cycle deadline was reached.

## Controller checks and evidence

Automatic checks merge wallets with shared funders and direct funding links,
transitively. Contract/delegated accounts and missing funding coverage are flagged.
Internal-funding evidence without independently corroborating traces is treated
as suspected linkage and incomplete controller coverage. Exchange withdrawals can
overcluster unrelated wallets, while undisclosed common owners can evade these
heuristics. Absence of observed links is not proof of independent ownership.

The system therefore separates `performance_qualified` from `qualified`. To bind
an actual external controller review, use `runtime/controller-reviews.json` keyed
by public wallet address. Each record requires:

```json
{
  "chain_id": 4663,
  "history_cutoff": 0,
  "independence_reviewed": true,
  "cluster_id": "REPLACE_WITH_REVIEWED_CONTROLLER_ID",
  "reviewed_at": 0,
  "expires_at": 0,
  "scope": "Actual reviewed scope and unresolved limits",
  "evidence_urls": ["https://REPLACE_WITH_ACTUAL_EVIDENCE"]
}
```

The timestamps above are schema placeholders, not a valid review. Reviews must
match the report's cutoff, be currently valid for no more than seven days, and
cannot split a group linked by the automatic funding graph. This interface
records external attestations; it cannot authenticate a false manual assertion.
The application never invents such a review or promotes a candidate by activity
count alone.

The initial `observation-001` account freezes an empty watchlist. It collects new
pool observations while independent discovery researches the wider swap universe.
Once at least five reviewed groups and fresh safety/size-aware route evidence
exist, start a NEW named experiment using `qualified-wallets.json` as its frozen
wallet input. Existing experiments are not silently reselected. Required safety
and executable-route evidence remain the boundaries documented in OPERATIONS.md.

## Stop and resume

Graceful stop after the current cycle (retains all databases and evidence):

```powershell
New-Item -ItemType File -Path runtime/STOP -Force
Disable-ScheduledTask -TaskName 'SNIPER Paper Research'
```

To resume, remove only the `runtime/STOP` sentinel, enable the scheduled task,
and start it. Never delete an account or event journal to recover from an outage.
An entry halt is different from stopping the process: keep processing exits when
positions exist, using the `halt` command described in OPERATIONS.md.

All runtime files and credentials are excluded from Git. The research service
does not send Telegram, email, or other messages and does not submit live orders.
