# Solana trader selection and leader signals

This extends the existing paper RUG DESK; Dots remains the operator. It does not connect or fund CopyFomo, change initialized config.py, activate paid xAI, or alter Robinhood accounts.

## Existing and added capabilities

| Capability | Existing implementation | Addition |
|---|---|---|
| Numerical trader screening | sniper.wallet_analysis.analyse_wallet reconciles supplied trade histories and applies cost, concentration, recent-performance and holding-time checks | Solana record/provenance validation and explicit input freshness |
| Model integration | rug_desk.grok.Judge, configured grok-4.7, existing transport, credential, budget, request lock and audit | Evidence-only rank_traders with strict SELECT/SKIP, finite confidence and up to three eligible leaders |
| Token checks and paper entry | rug_desk.check, Rugcheck normalization, pinned Market quotes, Book limits, cost model and simulated fills | Leader-event admission and final selection check inside the entry transaction |
| Exits and reporting | Independent watch.py; report.py; existing fill/Telegram outbox | Selection status in the report and separate leader decision exports |

No second token filter, broker client, trading account or exit engine was introduced.

## Approved settings preserved

| Article value | Existing approved behavior retained |
|---|---|
| grok-4 | grok-4.7 |
| $50 ticket; five positions | $25; four positions; six daily entries; $50 daily realized-loss entry halt |
| Risk score <=50; confidence >=0.65 | Risk score <=40; confidence >=0.70 |
| -50% stop | -35% stop |
| Mirror leader sells | Existing +100%/50% and +200%/25% original-quantity ladder, 30% trailing exit, six-hour time exit and 40% liquidity-drop exit |
| One-hour additions/cooldown | Existing 24-hour re-entry cooldown, duplicate-open protection and seen-once mint policy |
| Auto slippage / 3% impact setting | Existing solana_cost_scenario_v1 paper fee, impact, slippage, network, rent and stress assumptions |
| Headline leaderboard figures | Complete supplied 30-day transaction history, required costs and trace references |

The reused numerical screen requires at least 20 closed positions, win rate >=55%, at most 50% positive profit concentration in one token, average holding >=30 minutes, positive net after costs/open-inventory/follower stress, and a nonnegative recent week after open-inventory stress. This preserves the existing stricter history model. It is a numerical assessment of supplied data, not independent proof of wallet ownership, controller independence or profitability.

Missing required history costs, transaction evidence, token safety fields or pool evidence means SKIP. Confirmed zero remains distinct from missing. Manual exports must describe net token flows plus network fees; the existing analyser applies another 100 bps follower stress. Fees included in token flows are not charged twice.

## CopyFomo connection assessment

Reviewed 2026-10-27: [official product manual](https://www.copyfomo.com/docs).

| Connection needed | Documented capability / result |
|---|---|
| Leader buy/sell events | Telegram watch alerts are documented; no machine event feed contract verified |
| Our filter approves before copying | No external approval callback documented |
| Submit our approved purchase | No supported order API verified; copying follows CopyFomo's own armed rules |
| Confirmed fills and later sells | User-facing fill messages are documented; no external fill/reconciliation API verified |

Therefore there is no CopyFomo executor connection here. A printed verdict cannot place an order. No bot commands, referrals, deposits, wallet connections or copying were activated. A future connector needs an explicit supported event schema, approval boundary, order submission and verifiable fill identity.

Mirror sells are recorded as observations with mirror_exit_disabled. They never close a position, advance a ladder or change P&L. Selection removal, replacement, failure or expiry blocks new leader entries only. The existing watcher continues managing every open position by its recorded pool and original exit policy.

## Evidence and input formats

trader_acceptance.py creates complete inspectable examples named traders-replay.json and events-replay.json in its isolated output directory. Their addresses, histories, prices and model answers are synthetic. They are rejected by a normal strategy account because their source is replay.

A real manually supplied trader record has:

- handle, public Solana wallet, chain="solana", wallet_source_ref.
- source={"kind":"manual_export","reference":"export/message/report reference"}.
- Optional nonnegative integer followers. Missing remains unknown and cannot support a model's crowding judgment.
- history: matching wallet; complete=true; cutoff; window_start exactly cutoff minus 30 days; cost_basis="net_token_flows_plus_network_fees"; records; explicit gaps and opening_inventory lists.
- Each ordered history record supplies id, source_ref, timestamp, block_number (Solana slot is acceptable), transaction_index, kind, and gas_usd. BUY/SELL also supplies a Solana token address, positive quantity/value_usd, and verified=true to attest reconciliation by the supplying source. That source assertion is not independently verified by this importer.

Incomplete or unknown opening inventory/cost basis stays disqualifying in the reused analyser. Flat article statistics alone cannot qualify a record. History snapshots must be at most 24 hours old when selected. At most 100 input traders and 10,000 history records per trader are accepted, within a 10 MB input file.

A manual event is:
~~~json
{
  "chain": "solana",
  "handle": "actual_handle",
  "wallet": "ACTUAL_PUBLIC_SOLANA_WALLET",
  "mint": "ACTUAL_SOLANA_MINT",
  "action": "BUY",
  "transaction_signature": "ACTUAL_SOLANA_TRANSACTION_SIGNATURE",
  "occurred_at": 1793100000,
  "observed_at": 1793100001,
  "source": "manual_alert",
  "source_ref": "Reference to the supplied alert or transaction export"
}
~~~
Replace every placeholder and timestamp. The importer does not fetch source_ref or prove an alert is authentic. Events must be no more than five minutes old and cannot be future-dated. Handle and wallet must both match the active selection. This path remains Solana-only.

## Commands

From C:\Users\User\Downloads\sniper-v0.1.0, using the existing executor test environment:

~~~powershell
& .\runtime\executor-venv\Scripts\python.exe rug_desk\leader_signals.py status
& .\runtime\executor-venv\Scripts\python.exe rug_desk\leader_signals.py select --records PATH_TO_TRADER_EXPORT.json
& .\runtime\executor-venv\Scripts\python.exe rug_desk\leader_signals.py ingest --events PATH_TO_EVENT_ARRAY.json
& .\runtime\executor-venv\Scripts\python.exe rug_desk\trader_acceptance.py
~~~

select reuses the current model integration. A failed selection returns exit code 2 and clears admission immediately. Paid usage remains disabled: eligible records cannot obtain a funded model selection until the separately required authorization and xAI validation exist. No paid test is run during this integration. Empty numerical cohorts publish COMPLETE with leaders=[] without a model call.

The RUG DESK importer is a manual/replay input boundary, not the intended final automatic live workflow. No recurring CopyFomo importer is installed because no supported upstream machine connection has been established. The separate [Neon worker](NEON_BOT.md) now collects a restricted set of finalized, direct classic-SPL Raydium CP-Swap events automatically; it is not a CopyFomo bridge or complete trader history. Existing scan/watch/report schedules continue unchanged.

## Selection, event and fill integrity

The existing book.sqlite3 owns leader_selections, leader_events and leader_decisions. JSON files are views, never entry authority. A selection moves through UPDATING to COMPLETE or FAILED. Updates block previous leaders immediately; interrupted updates older than 180 seconds report UPDATE_STALLED. Completed selections expire after seven days, bounded by their history snapshot timestamps. Expired or mismatched policy evidence is blocked. A late completion cannot replace a newer selection.

Model responses permit SELECT or SKIP, require finite confidence in [0,1], unique eligible handles and contiguous ranks 1..3. Below the existing 0.70 threshold produces a completed empty selection. The model may cite only enumerated, present numeric metrics. It cannot insert narrative claims about controllers or unseen behavior. Rankings retain the original statistics and source references.

Token judgment still uses the existing validated BUY/SKIP schema, X evidence requirements and original confidence threshold; the article's COPY label was not introduced as a fill type. Supplied leader context is additional evidence, not permission to bypass token checks.

Events deduplicate by Solana transaction signature, wallet, mint and action, independently of alert/provider IDs. Duplicate and conflicting replays are logged but cannot enter again. The original seen-once mint rule additionally prevents the same token entering through two leaders or the scanner. Leader validation repeats after provider/model latency and inside the same transaction as final portfolio checks and entry insertion. Simulated fill, position, entry alert event and leader-entry decision commit together.

runtime/leaders.json shows selection validity. runtime/leader-decisions.json contains the latest 200 leader decisions; complete history remains in SQLite. Existing checks.csv records token checks, while trades.csv and the fills table record simulated fills separately. BUY can exist without a fill if refreshed market data or final limits reject entry. Export failures after a committed entry never erase or relabel that fill.

The isolated acceptance demonstrates a numerical leader buy reaching the existing cost-aware paper engine, risk/leader rejections, durable duplicate prevention, explicit empty selection, separate decisions/fills and unchanged partial/trailing exits after leader removal. It makes no network calls, paid model requests, real fills or Telegram sends and does not start or reset any strategy trial.

## October 28 integration recheck

The numerical screen, ranking, signal admission and paper execution were already
implemented; the repeated article does not require another bot or risk manager.
One integration defect was found after the model-readiness upgrade: `Judge`
returns `judged_at` and `binding` audit fields, but selection rejected that updated
envelope. Selection now retains those two fields while still independently
validating the permitted verdict, finite confidence, distinct eligible leaders
and evidence fields. No model-supplied statistics replace screened history.
The regression test reproduces the former failure through the actual `Judge`
client with an offline transport and verifies completed publication after repair.

The existing RUG DESK account's ACTIVE PAPER Jev filter also applies to leader
buys through the same `check` and final `Book` transaction. Integration tests
verify an isolated leader event with a prospective Jev assessment, restart-safe
deduplication, disabled Jev calls blocking entry, and original exits after leader
removal. Neon retains its separate account and does not inherit that promotion.

CopyFomo's [official manual](https://www.copyfomo.com/docs), rechecked on October
28, describes Telegram watch alerts, its own automatic copying and fill/sell
notifications. No supported external machine event feed, per-purchase approval
callback, approved-order submission or reconciled-fill API was verified. A
Telegram notification is not such a connection. The independent
[Fomo API](https://www.fomo-api.com/docs) documents a public ten-row leaderboard;
its wallet/trade endpoints require a server API key and do not establish a
CopyFomo execution bridge or complete cost/account-ownership evidence.
