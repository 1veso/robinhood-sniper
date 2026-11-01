# Radon authorized export contract

This is a normalized **input contract**, not a claim that Fomo offers this API.
Only feed data you are authorized to access and automate. Radon does not accept
session cookies or use a third-party leaderboard as if it were Fomo.

Top-level JSON fields:

| Field | Contract |
|---|---|
| `version` | `radon_export_v1` |
| `source_id` | Stable provider/export-stream identity; changing it requires reconciliation |
| `evidence_label` | `authorized_export`, or `synthetic` in isolated demos/replay only |
| `authorized`, `authorization_reference` | `true` plus nonempty operator authorization/evidence reference |
| `complete`, `coverage` | `true` plus specific source universe, route and inventory coverage description |
| `opening_at`, `through` | Integer UTC epoch seconds; completeness applies throughout this interval |
| `openings` | Map from exact public Solana wallet to verified opening record |
| `events` | Normalized complete supported activity, including uncopyable buys and unsupported events |
| `quotes` | Historical replay only: archived quote observations, never current substitutions |
| `kols` | Optional verified identity evidence for leaders marked `is_kol` |

Opening record:

```json
{"sequence": 0, "verified": true, "evidence": "source balance snapshot reference", "balances": {}, "is_kol": false}
```

An empty `balances` object means **verified zero opening inventory**, not unknown.
Otherwise map mint addresses to token-unit quantities encoded as decimal strings.
Opening records and the baseline timestamp must remain stable for successive
event-feed updates. Append/overlap events using the same sequence and IDs; do not
replace the baseline with today's balances. A moving research window is a
separate `history.json` export and must have its own verified opening state.

Event shape (replace placeholder addresses with exact validated public addresses):

```json
{
  "id": "canonical-chain-transaction:instruction-index:wallet",
  "leader": "PUBLIC_SOLANA_WALLET",
  "sequence": 1,
  "at": 1800000010,
  "side": "buy",
  "mint": "SOLANA_MINT",
  "quantity": "100",
  "route": "raydium"
}
```

The producer must supply stable canonical IDs rather than mutable provider row
IDs, deduplicate the same on-chain instruction, and retain contiguous per-wallet
sequence numbers. `side` is `buy`, `sell` or `unsupported`. `quantity` is the
actual leader's token quantity, not the copier's quantity. Every sequence step
must be present. Transfers and token-to-token routes that cannot be reconstructed
must be included as unsupported and make inventory incomplete, not silently
filtered away. Token decimals must already be applied by the producer.

Historical quote shape:

```json
{
  "mint": "SOLANA_MINT", "pool": "SOLANA_POOL",
  "price": "0.01", "liquidity": "50000", "volume_24h": "100000",
  "created_at_ms": 1799990000000, "observed_at": 1800000015,
  "sol_usd": "150", "sol_observed_at": 1800000015,
  "source": "authorized historical observation", "executable": false
}
```

Each replay quote must exist as of the delayed simulation timestamp and remain
within the configured freshness bound. The same constraint applies to SOL/USD
used to value network costs. Missing quotes mean skipped entries or unresolved
exits/marks. No historical X/safety decision is inferred from these observations.

Research exports cover `lookback_days` ending at `through`, with opening inventory
at that boundary (up to five minutes earlier is accepted). Research must not be
older than the configured shortlist TTL. Prospective event feeds have the much
tighter `source_max_age_seconds` watermark limit.

If `is_kol` is true, `kols[wallet]` must contain `verified: true`, exact matching
`wallet_on_fomo` and `wallet_on_kolscan`, a `source` evidence reference, and an
`observed_at` within one day of the history cutoff. This is reviewed identity
evidence, not an automated browser scraper. Median holding time is independently
derived from verified closed episodes. Existing opening positions with an
unknown original buy time are excluded from that duration sample.

The bundled `--demo` scenario generates valid, explicitly synthetic inputs in a
separate account. Keep real input files inside private runtime storage.
