# Pump / Jev Review — shadow specialist

Release 0.1.21 adds a fourth board seat, not a fourth trading account. The user explicitly chose to keep Jev calls disabled. Existing trading configuration is authoritative. Neither a Jev BUY nor installation authorizes entries, paid Grok, signing, funding or live deployment.

## Existing system and reuse

| Component | Existing owner | Integration |
|---|---|---|
| Robinhood chain candidates, research and two-wallet strategy | `supervise.py`, `src/sniper/engine.py`, exploration ledger | Unchanged; no Solana dossier is inferred from EVM events |
| Solana candidates and Grok research | `rug_desk/scan.py`, `check.py`, `judge.py` | Read completed candidate facts, pinned quote and separate baseline decision from `checks` |
| Neon leader candidates | `src/sniper/neon.py`, existing Solana checks | Read its separate account's candidate journal; never consolidate balances |
| Token safety and market limits | `rug_desk/rugcheck.py`, `market.py` | Reuse approved checks; missing/failed facts cannot become eligible through Jev |
| Risk, costs, duplicate entries and paper fills | `rug_desk/book.py`, existing check/watch paths | Remain the only authorities; Bot 4 never imports an executor, opens a position or writes these databases |
| Durable reservations and worker locking | `DailyBudget`, `process_lock`, desktop worker | Reuse mechanisms with a separate Jev reservation journal and process lock |
| Board and journal | existing loopback console | Fourth tab, candidate links, exact read-only assessment API |

The Solana and Neon books are opened `mode=ro` and `query_only` in SQLite snapshots, with ledger/marker account identity checks. Binding a source to a different UUID fails. Existing risk limits, cash, positions, cost policies, schedules, exits, seen-once behavior and kill switches are unchanged. A missing price remains the existing watcher's unresolved mark, never a Bot 4 sale.

## Added files and operation

`src/sniper/jev.py` implements the adapter and experimental gate; `jev_dossier.py` builds and validates allowlisted observations; `jev_review.py` owns persistence and evaluation. The root `jev_review.py once|status` exposes only shadow recording/readback. The new `Pump Jev Shadow Review` Windows task invokes `desktop.py worker jev-review` each minute. It has no signing or order interface and cannot block a baseline worker. The existing task installer supports `-JevOnly` without altering other tasks.

Authoritative review storage is `runtime/jev-review/assessments.sqlite3`. It contains source bindings, immutable snapshot IDs, separate baseline decisions, dossiers, results, application-owned WATCH counts and completed runs. It is not a strategy ledger. `status.json` is a view. The board shows the latest 200 snapshots; evaluation reads all completed assessments.

`settings.json` defaults to `enabled=true`, `mode=SHADOW`, `paid_authorized=false`, `daily_budget_usd="0"`. `enabled=false` stops collection without touching baseline execution. Current deployment retains disabled external calls. Future separately authorized calls would read `runtime/typesafe-api-key.txt` through the existing server-side runtime-file convention. The browser, dossier and logs never receive that file. A key alone does not enable spending. A local budget reservation is not a vendor-enforced cap.

## Verified TypeSafe contract

Checked 2026-10-28 against [System One](https://docs.typesafe.ai/api/system-one), [OpenAPI](https://api.typesafe.ai/openapi.json), [models](https://docs.typesafe.ai/models), [Choice](https://docs.typesafe.ai/primitives/choice), [Score](https://docs.typesafe.ai/primitives/score), [Noul](https://docs.typesafe.ai/primitives/noul) and [confidence](https://docs.typesafe.ai/confidence).

The adapter uses `POST https://api.typesafe.ai/v1/systemone`, Bearer authorization, and structured `state` plus named typed `questions`. It pins `jev-1.14.0` and requires the returned model ID to match. Noul is P(yes). Choice stores both the whole option distribution and its separate confidence. Five-level Score is fractional 0–4, with the full distribution and legend retained; adding one is display-only. Action-label probability is never a probability of profit. Required types, fields, finite values, enums, ranges and distribution consistency are validated. There have been no live Jev calls in this release verification.

## Experimental questions, not trading policy

`jev_shadow_v1` records its settings with each assessment. The article's action and organic cutoffs are experimental: P(buy label) >=0.70 and P(organic yes) >=0.65. We explicitly define the missing late cutoff as P(late yes) <=0.35. Raw exit Score must be <=2, corresponding to <=3 on a displayed 1–5 scale.

Organic means positive one-hour holder growth with at least five independently reviewed buying controllers and no controller above 20% of buy volume. Late means one-hour price gain >=100% and holder percentage growth no greater than that gain. These definitions do not change any strategy's admission rules. Arithmetic is computed in code on matching observation windows.

Exit levels use fresh, position-sized, pool-bound sell evidence: level 0 requires ticket/capacity <=0.1% and impact <=0.5%; subsequent non-overlapping levels allow <=0.5%/1%, <=1%/2%, then <=2%/3%. Missing capacity, failed routes or higher values are level 4. Indicative DexScreener liquidity and virtual reserves are not executable capacity. Without required evidence no model request is made.

The article's liquidity floor, younger tokens, equity sizing, cooldowns, night restriction, manual approval and exit rules are not imported. Jev's result is only a proposed extra filter. `filter_would_admit` can be true only alongside an already eligible baseline BUY. A model cannot override a hard rejection or the existing Grok gate.

## Facts, gaps and untrusted content

Dossiers carry candidate/account IDs, chain, mint, exact pool/curve, venue phase, timestamps, source references and evidence hashes. Only explicit observations are copied. Grok judgment, reason text and BUY/SKIP are held separately and excluded from the model state and evidence hash. External symbols are escaped inside untrusted tags; arbitrary descriptions/posts are not copied. Returned fields cannot choose settings, identity, clocks, size, permissions or orders.

Existing journals supply pinned indicative prices, reported liquidity and some provider-scoped safety facts. They do not establish historical holders, wallet/controller independence, social/follower growth, developer outcomes, verified Pump phase or executable exit capacity. These remain null/missing; provider LP classifications do not become independently verified passes. Infrastructure exclusion requires a specific corroborated account identity, role and pool binding. Program ownership alone never removes a holder, and identified accounts do not prove independent controllers.

There is no working Pump launch feed connected here. [PumpPortal's documented feed](https://pumpportal.fun/data-api/real-time/) offers creation and migration subscriptions; token/account trading subscriptions are metered and require a key with a funded linked wallet. This release neither subscribes nor purchases access. [Pump's program documentation](https://github.com/pump-fun/pump-public-docs) is a reference for future verification. Bonding curve, migrating, AMM and unknown are separate states. The current importer leaves phase unknown until independently supported; this board does not claim all-launch or PumpSwap trade coverage.

## Failure, deduplication and WATCH

An atomic snapshot+policy hash claim precedes any request. Completed and ambiguous in-flight attempts are not deliberately retried. Timeout, transport/API error, malformed response, invalid dossier, insufficient evidence, budget rejection, hard baseline rejection and disabled calls are distinct states. Error text is sanitized. Failed/ambiguous attempts retain spending reservations. At most one external attempt is scheduled per pass; the bounded transport has no automatic retries.

WATCH permits exactly one refresh after 1,800 seconds, with a later evidence window and snapshot, fresh required facts and still-passing baseline entry checks. Counts and due times persist across restarts. A second WATCH becomes PASS. The worker never resets seen-once checks or fabricates fresh evidence to make a refresh possible. No current history/enrichment source can supply all required fields, so this capability is verified with isolated fixtures, not claimed operational evidence.

## Comparison and traceability

The board prepares a hypothetical matched-fill subset comparison, separated by original account. Retained trades use identical recorded candidate, entry time, ticket, costs, exits and realized net P&L. Removed trades contribute zero; freed capital is not redeployed. It does not manufacture alternative fills, skipped-token returns or a reoptimized portfolio. Model-cost estimates are recorded separately and are not vendor invoices.

Only completed assessments available by the original entry time enter the prospective comparison. Open positions, unmatched trades and late assessments are counted as exclusions. The current asynchronous journal reader usually sees checks after completion: those assessments can explain disagreements, but cannot prove prospective profitability. A future pre-entry factual capture/enrichment source would be required to populate a valid timing-matched cohort without changing baseline timing. No performance conclusion is available while Jev and its required evidence remain disconnected.

`tests/test_jev.py` verifies the contract, fractional score, separate confidence, input isolation, invalid/missing facts, failures, concurrent duplicates, restart/WATCH limits, hard rejection, read-only APIs and baseline behavior when disabled. `tests/verify_jev_acceptance.py` saves a traceable synthetic integration example using the existing cost-aware paper executor and a mocked TypeSafe reply. It must be run only in a new isolated folder. Its fills and results are excluded from every strategy P&L.
