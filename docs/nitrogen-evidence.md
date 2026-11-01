# NITROGEN factual evidence contract

The built-in public collector establishes current quotes, classic-SPL token facts,
pool-specific Rugcheck classification and supported finalized tracked-wallet purchases.
It cannot infer historical holder snapshots, controller independence, exact launch
age, developer ownership, social counts or a verified executable sell route.

Provide these through a reviewed factual source using `nitrogen_bot.py import --source`.
Imports are account-bound, immutable by mint/pool/snapshot timestamp, privately stored
in NITROGEN's ledger, and revalidated at judgment **and** approval. An imported assertion
is evidence supplied by its named producer, not independent verification by NITROGEN.
The producer must retain the raw source records referenced by the payload hashes.
Production imports require `demo:false`; synthetic records cannot authorize a real-data
paper account. No other bot's provenance prefix or account is impersonated.

An object has exactly these allowed fields:

| Field | Contract |
|---|---|
| `account_id` | NITROGEN's initialized account ID |
| `mint`, `pool` | Exact Solana addresses; must match the pinned candidate |
| `demo` | `false` for real-data evidence; true only in a separately initialized synthetic account |
| `snapshot_at` | Integer UTC Unix seconds; collection timestamp |
| `observations` | Metric-name map; values are explicit `"missing"`, or the observation object below |
| `source_refs` | Reference-ID map; records below |
| `window` | `{ "start": unix_seconds, "end": unix_seconds }`, exactly 3,600 seconds |
| `trade_windows` | `previous` and `latest`, each a start/end object, consecutive five-minute intervals |
| `social_windows` | `then` and `now`, consecutive one-hour intervals with the same collection definition |
| `independence_review` | Reviewed controller-evidence proof below, or `"missing"` |
| `exit_route` | Verified size-specific exit proof below, or `"missing"` |

Every available metric is:

```json
{"value":1200,"observed_at":1800000000,"source_ref":"your_source_record_id"}
```

Numbers must be finite JSON numbers, not strings, booleans, NaN or infinity. Counts
must be integers. Fractions and percentage units must follow the metric definitions.

A source record is:

```json
{"provider":"YOUR_ACTUAL_PROVIDER","reference":"YOUR_RETRIEVABLE_RECORD","observed_at":1800000000,"payload_sha256":"64_lowercase_hex_characters_of_actual_source_record"}
```

These are templates, not actual evidence. The source observation times cannot be in
the future; the one-hour historical observations must match the stated window start.
Current observations match the end. Count-window observations are collected currently,
with their aggregation intervals provided separately. No historical snapshot may be
backfilled using a current value. No missing count is treated as zero.

All these metrics are mandatory for this first implementation:

| Metrics | Units / scope |
|---|---|
| `age_s` | Actual token launch age in seconds, not earliest discovered pool age |
| `market_cap_usd`, `liquidity_usd` | USD; actual source scope retained |
| `holders_now`, `holders_then` | Complete comparable owner counts at one-hour endpoints |
| `price_now`, `price_then` | Matching exact token/pool USD price endpoints |
| `buys_previous_5m`, `sells_previous_5m`, `buys_latest_5m`, `sells_latest_5m` | Actual trade counts in the two explicit intervals |
| `top10_share`, `dev_share` | Fractions 0–1; verified developer identity for developer share |
| `social_mentions_now`, `social_mentions_then` | Counts under one comparable source/query definition; not sentiment |
| `independent_buyers`, `largest_buyer_share_pct` | Reviewed controllers, not raw distinct addresses; share 0–100 |
| `exit_capacity_usd`, `exit_impact_pct`, `exit_ticket_usd` | Real size-specific route evidence, impact 0–100, ticket matching the proposed notional |

A scoped proof is:

```json
{"verified":true,"observed_at":1800000000,"source_ref":"ACTUAL_REVIEW_OR_ROUTE_REFERENCE","mint":"EXACT_MINT","pool":"EXACT_POOL"}
```

Do not mark a proof verified merely because the paper cost simulator returns a number.
The current model request is blocked if a required metric/proof is missing, stale,
invalid, mismatched to its window, or sized for another ticket. Supply the proposed
notional from the bound sizing policy; it changes with account equity and modeled
network/rent costs, not Jev probability. Approval requires fresh evidence for the
already staged amount, not a newly larger amount.

The public worker can collect SEARCH/RISK/WHALE facts and persist specific blockers
while this enrichment is unavailable. A complete authorized producer is an operational
dependency; this implementation does not invent one or silently scrape session cookies.
