# NEON live paper workspace

NEON is Bot 3 in the existing TENDIES TRACKER console. Its original account,
workers, pinned pool observations, cost-aware Book and automatic exit rules
remain authoritative. The workspace reads those records; it is not a trading
runner, an additional strategy, or a model authorization mechanism.

The five pipeline rows describe responsibilities: SCOUT intake, FILTER local
limits/token facts/pool checks, ANALYST model judgment, PAPER EXECUTION quote
refresh/final admission/committed fill, and GUARD position monitoring/exits.
A judgment and a fill are different events. An early rejection does not imply
downstream checks ran. Socket health does not imply a fresh price or healthy
external provider. Missing marks retain holdings and do not manufacture exits.

Snapshot, journal and event contracts build on
[NEON_EVENT_CONTRACT.md](NEON_EVENT_CONTRACT.md). Financial values come from the
server's existing Decimal accounting. NEON retains its recorded capital policy;
an account without an explicit baseline displays net paper P&L instead of a
fabricated balance. Provider/model prerequisites and source scope remain visible.

## Read-only workspace contract

`GET /api/state?bot=neon` retains the existing snapshot fields and adds
`neon_workspace`. The selected `account_id` must match every journal, candidate
and event response. The view is derived from the existing NEON book; reading it
does not initialize an account, run collection, call a model or execute a trade.

| Field | Meaning |
| --- | --- |
| `worker` | Observed running/idle/blocked/degraded/disconnected state, running-job evidence, last completed scan/watch and next scheduled times when freshly known. Unknown schedule times remain null. |
| `data` | Source observations and their timestamps/age. Transport connectivity is separate from source freshness and paper execution mode. |
| `counts` | Account-to-date unique token candidates, separately counted trader research profiles, committed buy fills, closed positions, wins and their denominator, and explicitly scoped failure counts. Failure observations may describe the same incident; they are not unique failed trades. |
| `pipeline` | Five responsibility rows with actual substage, candidate identity, observed state, timestamps and a stated counter scope. A current early rejection leaves downstream checks not run. |
| `journal` / `rejections` | Bounded initial journal page and its complete matching-scope counts; these are not totals inferred from visible rows. |
| `replay` | Null for a normal account. A REPLAY label is exposed only for synthetic/excluded metadata bound to this account ID. |

`snapshot`, `fills`, position exit levels and persisted P&L/equity observations
continue to come from the shared Python view. JavaScript formats them and draws
the chart; it does not independently calculate cash, basis, P&L or exits.

### Decision journal

`GET /api/neon-journal` accepts these optional query parameters:

| Parameter | Accepted scope |
| --- | --- |
| `stage` | `scout`, `filter`, `analyst`, `execution`, `guard` |
| `token` | Case-insensitive mint substring, at most 100 characters |
| `outcome` | `running`, `passed`, `rejected`, `blocked`, `error`, `filled` |
| `since` | Nonnegative Unix observation timestamp in seconds |
| `category` | `strategy`, `missing_evidence`, `provider`, `portfolio` |
| `reason` | Exact public reason returned by the journal/rejection summary, at most 600 characters |
| `before` | Positive event sequence from `next_before`, for older records |
| `limit` | 1–200 records; default 100 |

The response contains `account_id`, `items` newest first, `total`, `next_before`,
`scope` and `rejections`. `total` covers all matching durable records under the
filters, independent of page size and the older-page cursor. Rejection bars count
distinct candidate/reason combinations, deduplicating the token and leader
projections of one rejection; provider bars count recorded source failures.
A committed fill and its admission decision remain separate journal records.
The `scope.count_basis` text states this distinction. Bar selection uses the same
category/reason filters as the decision stream. There is no invented historical
stage backfill before observation-stream installation.

### Candidate evidence

`GET /api/neon-candidate?mint=<Solana mint>` returns the bound account/candidate,
exact pool when established, observation time, `checks`, `judgment`, positions,
committed fills and linked events. Fourteen configured checks comprise nine
Rugcheck fact checks, four existing pool checks and the existing Grok judgment
criterion. The model criterion is explicitly separate from deterministic facts.
Each row supplies its observed value, configured threshold, result, source,
observation time and missing/not-run explanation. Missing or malformed numbers
do not become passes. Available X post references are allowlisted public links;
raw provider errors and credentials remain server-side.

### Event resume and timing

`GET /api/events?bot=neon&after=<snapshot cursor>&limit=200&wait_ms=1000`
adds an optional bounded wait to Prompt 1's transport. `wait_ms` is 0–10000;
`limit` is 1–500. Every inspection releases its SQLite read transaction before
waiting. Concurrent waiters are bounded; HTTP 429 means retry, not a worker
failure. JSON and `Accept: text/event-stream` remain read-only alternatives.

Validate `account_id`, stream UUID, response `prev_cursor`, and each event's
`previous_cursor` against the last accepted ID. Verify `event_count` and final
page cursor. SQLite sequence numbers may have legitimate holes after ignored
duplicate inserts: never require `seq + 1`. Deduplicate by stable event ID;
resume after the last accepted cursor. An inconsistent checkpoint or HTTP 409
requires a new snapshot. `has_more` indicates catch-up pages. An empty response
is a transport heartbeat; it does not establish fresh market data.

`observed_at` is source evidence time, `recorded_at` is event recording time in
seconds, and new records also have `recorded_at_ms`. Legacy millisecond fields
remain null rather than manufacturing precision. The UI records its receipt and
render timestamps separately. `server_time_ms` permits an explicit clock-offset
estimate, but it is not proof of price freshness or provider latency. The
millisecond emission timestamp is recorded inside the transaction; use the
isolated post-commit sidecar below when measuring actual commit-to-screen timing.

## Isolated presentation replay

`tests/verify_neon_experience.py` exercises the existing engine with deterministic
offline providers and judgments. It refuses to prepare an existing directory,
and every later command requires a replay marker bound to that newly created
account. The authoritative book metadata contains `ui_replay` with label
`REPLAY`, `synthetic: true`, `excluded_from_strategy: true`, and the account ID.
A separate console server must point at this isolated root. There is no runtime
switch that replaces NEON's production account with fixture data.

From the repository, using its existing Python environment:

```powershell
$env:PYTHONPATH="$PWD\src;$PWD\tests"
$replayRoot="$PWD\runtime\neon-experience-acceptance\new-review"
python tests/verify_neon_experience.py --prepare --app-root $replayRoot
python tests/verify_neon_experience.py --play --app-root $replayRoot --interval 2 --judge-delay 3
python tests/verify_neon_experience.py --verify --app-root $replayRoot
```

The root must be new. Reopening it does not reset its account or replay stages.
Individual stages may instead be invoked with `--stage <name>` in this order:

| Stage | Existing-engine behavior verified |
| --- | --- |
| `reject` | Missing mint-authority evidence ends at FILTER; no model call or fill |
| `failed_refresh` | Mocked BUY judgment followed by unavailable refreshed quote; no entry |
| `entry` | Delayed mock judgment, original admission checks, one cost-aware paper entry |
| `duplicate` | Original leader event replay; no second entry |
| `missing_quote` | Watcher retains quantity and marks valuation unresolved; no sale |
| `recover` | Fresh pinned fixture quote updates the original holding |
| `partial` | Original first take-profit threshold, exactly one ladder step |
| `close` | Original trailing exit closes the remaining quantity |

There are no network calls, paid model calls, Telegram sends, signatures or
broadcasts. The fixture trial is explicitly synthetic and has no evidentiary
relationship to the production seven-day trial. The ordinary paid authorization
files remain disabled even in the fixture. Successful presentation replay does
not prove model availability, real leader qualification, route execution or
profitability. All acceptance fills and observations are excluded from every
production strategy's results.

The final `verification.json` contains ledger fills and checks. Each stage writes
its result to `stage-<name>.json`. The replay reopens the same Book for every
stage, proving persistence across processes. Repeating a completed stage returns
its saved result and does not add an order or another event sequence.

## Timing evidence

In the isolated replay only, a wrapper records the wall-clock time immediately
after each real SQLite transaction commit returns. `replay-progress.jsonl`
contains the stable event `id`, `account_id`, event type, replay stage and
`commit_returned_at_ms`. It includes intermediate commits before the delayed
mock provider responds. The measurement never changes ledger timestamps or
production code. Browser telemetry can join on event ID and compare this value
with its UI receipt and rendered frame time. Report that measured scope and the
number of joined events; do not infer live provider or worker latency from it.

On Windows, a console reader can briefly prevent replacement of a JSON view.
`rug_desk.book.atomic` now retries only replacement of the same already-fsynced
bytes for Windows sharing/access errors, bounded to eight attempts and one
second. It never repeats the trading operation that produced the view. An
exhausted retry raises and retains the previous published view. The authoritative
SQLite fill may already be committed; recovering publication must not execute
that trade again. `test_atomic_publication` covers a native held-reader release,
bounded exhaustion and unrelated permission errors.

`tests/test_neon_experience.py` verifies the complete fixture sequence,
cost-bearing entry/partial/close, BUY without a fill, missing-price holdings,
identity binding, ordered events, duplicate replay, disabled paid gates and
refusal of existing or mismatched accounts. Browser layout, interaction and
latency evidence are separate from these engine tests.

## Verification scope

The first complete regression run executed 583 cases: 578 passed and five
cases were blocked by sandbox loopback-socket restrictions. Those same five cases
were rerun with local loopback access and all passed in 9.564 seconds; see
`runtime/neon-experience-review/loopback-tests.txt`. Two final focused backend
checks and three new source-observation checks also passed in separate runs.
Do not add these separate counts together as a count of unique tests.

These are code/HTTP/engine checks. Browser screenshots, recording, visual checks
and measured event-to-screen latency require their own dated evidence; no
latency or visual pass is inferred from the offline test counts above.
