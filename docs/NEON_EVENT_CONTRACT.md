# NEON foundation audit and Prompt 2 view contract

The inspected checkout was clean `main` at `d94e458`, 15 commits after the
referenced ancestor `b4c6f6e`. No repository `AGENTS.md` was present. NEON was
already implemented: `neon_bot.py`, `NeonBook`, isolated `neon_bot/runtime`,
`NEON Paper Signals` and `NEON Paper Watch`, and a third console panel. This patch
does not initialize another account, assign new capital, register tasks or change
strategy settings. Existing runtime identities remain authoritative.

Reused: `rug_desk/check.py`, `book.py`, `watch.py`, `scan.py`, `report.py`, model
and market clients, strict token checks, leader selection/admission, pinned-pool
refresh, exact remaining basis, transactional fills, modeled costs, independent
exits and notification outbox. Dots remains the operator. NEON workers continue
to use `NeonBook` and the existing pipeline, not a second execution engine.

The missing pieces were the RUG DESK position adapter, a common Solana view,
dynamic readiness presentation, and durable candidate-stage/market/P&L events.
These are now shared by RUG DESK and NEON. Robinhood's adapter is unchanged.
The prior Neon view is a compatibility wrapper around the shared adapter.

## Root, credentials and policy

Worker arguments and cadences are unchanged. `neon_bot.py --app-root <app>`
uses `<app>/neon_bot`; the underlying Book/check/watch/scan/report interfaces
continue accepting explicit roots. Use the NEON runner for its trial/capital and
leader admission rules rather than invoking generic desk commands as a substitute.
Book checks the existing account marker and settings hash before additive schema
installation. The console independently checks that same binding without a
writable Book constructor.

The NEON model worker looks up the existing server-side key beneath its app
root, keeps paid authorization beneath its own runtime, and attributes permitted
requests to its own account within the existing shared RUG DESK budget. Disabled
authorization still prevents key loading and provider calls. Telegram pairing
uses the existing app runtime; its outbox remains per-account. No key or
authorization file is returned by these APIs. `/api/health` discovers the actual
Solana/NEON account IDs, last recorded worker completion and stream availability.

## Initial snapshot

`GET /api/state?bot=neon` (or `bot=solana`) returns the existing console envelope
plus:

- `snapshot`: positions with positive remaining quantity; realized/unrealized and
  net paper P&L; remaining basis; execution/model costs and rent reserves. Closed
  rows never count as open positions. Unknown bills make `net_pnl` null; the
  explicitly named `known_cost_net_pnl` is only a partial-cost calculation.
- `snapshot.capital_baseline_supported`: equity, cash and return are null without
  a bound recorded capital baseline. RUG DESK is not assigned NEON's bankroll.
  NEON retains the explicit baseline already stored in its existing policy.
- `policy` and `policy_binding`: unchanged settings and account-pinned hash.
- `readiness`: observed state, source timestamp/age, blockers and observed entry
  status. Stale/missing proof is explicit. A view never grants entry authority;
  `entry_enabled: null` means the worker report does not establish that answer.
- `fills`, `closed`, `assets`, `cost_policy`, `pnl_history`, `equity_history`:
  derived from the existing book. Historical NEON observations are retained.
  No synthetic history or acceptance fills are introduced.
- `event_stream`: version 1, durable cursor, availability and endpoint URL.

The book view and cursor use one SQLite read transaction. Worker JSON, provider
status, model billing and Telegram queues have independent clocks; readiness
does not pretend those external views are transactionally synchronized. Snapshot
responses may be cached for three seconds, with their original cursor intact.

## Incremental stream and reconnection

`GET /api/events?bot=neon&after=<cursor>&limit=200`

Only `solana` and `neon` are allowed. `limit` is an integer from 1 through 500.
The cursor is `<persisted-stream-uuid>:<sequence>`; it is account-specific.

```json
{
  "schema_version": 1,
  "available": true,
  "account_id": "the-existing-account-uuid",
  "events": [{
    "id": "stream-uuid:42",
    "seq": 42,
    "account_id": "the-existing-account-uuid",
    "observed_at": 1793227000,
    "recorded_at": 1793227001,
    "type": "candidate_stage",
    "data": {
      "candidate_id": "account-uuid:mint",
      "mint": "the-observed-mint",
      "stage": "facts",
      "status": "COMPLETED",
      "verdict": "SKIP",
      "reason": "required evidence missing",
      "entry": false,
      "leader_event_id": null
    }
  }],
  "cursor": "stream-uuid:42",
  "high_watermark": "stream-uuid:45",
  "has_more": true
}
```

Event types are `candidate_stage`, `market_observation`, `paper_fill`,
`position_changed`, `pnl_observation`, and `worker_state`. Candidate stages have
`STARTED`, `PASSED` or `COMPLETED` status. A BUY judgment can still have
`entry: false`; only a committed `paper_fill` indicates a simulated execution.
Leader rejections can occur before the token check. The optional leader event ID
links those stages to their original event. All external strings are untrusted:
render as text, never HTML or executable UI instructions.

Sequence is commit order, not market-time order. `observed_at` is evidence time;
`recorded_at` is append time. Fill/position observations commit with their
authoritative book mutation. Replayed candidate/quote events use stable unique
keys. There is no historical stage backfill; pre-installation stages remain
unknown. No event constitutes an order request.

Client procedure:

1. Read the initial snapshot and retain `event_stream.cursor`.
2. Fetch events strictly after that cursor; deduplicate by `id`.
3. Advance only after processing the returned page. Drain `has_more` pages.
4. On reconnect, resume the same cursor. After a server/process restart the IDs
   remain stable. A foreign/malformed/ahead cursor returns HTTP 409: discard the
   local projection and obtain a new snapshot. Missing account data returns 503.
5. Refetch the book snapshot after fill/position/P&L events to reconcile financial
   displays. Client event arithmetic never replaces the authoritative book.

For EventSource, use `format=sse` or `Accept: text/event-stream`. Each bounded
batch contains `event: observation`, stable `id`, and JSON `data`, followed by a
`cursor` control event and `retry: 3000`. The response closes without retaining
a database transaction. Automatic reconnection supplies `Last-Event-ID`, which
takes precedence over `after`. An empty batch is a heartbeat, not missing data.
The initial request still requires the snapshot cursor. Snapshot polling remains
supported; Prompt 2 can consume this stream without changing worker cadences.

## Prices, observations and verification

`GET /api/chart?bot=neon&asset=<mint>&pool=<pool>` also supports `bot=solana`.
Points are only stored, validated observations for that exact pool. Their
observation timestamps are preserved; gaps are not interpolated. A missing price
retains the position and records unavailable valuation, never a zero-price sale.

`tests/test_paper_events.py` runs the existing pipeline with isolated fixtures and
a loopback HTTP server. It covers isolation, positive-quantity open counts,
rejected/no-fill decisions, duplicate replay, transaction rollback, original
stops, pinned observations, unknown bills, configuration pins, disabled paid
access, snapshot/cursor consistency and JSON/SSE reconnect after server restart.
Existing pipeline, Neon, Jev, executor and console regressions remain separate
evidence; fixtures never enter production results. No paid request, signer,
wallet funding, chain submission or Telegram delivery is part of this patch.
