# Token screening, trader ranking, and optional AI evidence

This module provides read-only screening from the systems supplied in the project brief. It does not connect Copyfomo, open positions, submit swaps, or turn an AI opinion into an order.

## Public interfaces

```python
from sniper.adapters.solana import RugcheckClient, DexScreenerClient

safety = RugcheckClient().report(mint, now=utc_epoch_seconds)
quote = DexScreenerClient().quote(mint, now=utc_epoch_seconds)
# Keep the same pool for subsequent observations:
next_quote = DexScreenerClient().quote(mint, now=later, pool_id=quote.pool_id)
```

For the Robinhood namespace:

```python
client = DexScreenerClient(chain_id="robinhoodchain")
quote = client.quote(token_address, now=utc_epoch_seconds)
# quote.asset_id == 'robinhood:0x...'
```

Transport is injectable: `transport(method, url, body, headers, timeout)` returns decoded JSON. Unit tests use synthetic fixtures and do not call an API.

### Evidence semantics

- Explicit JSON `null` in an existing mint-authority field differs from an absent field. Absent fields remain unknown.
- Scores, percentages, amounts and liquidity must be finite, typed, nonnegative numbers. Invalid values reject normalization.
- LP evidence refers to the largest fully measured market in the report, with its pool identifier stored in `details.report_pool_id`. It must not be applied to another execution pool.
- A truncated holder list with fewer than ten relevant holders does not establish low concentration unless the report provides the complete holder population. AMM/locker exclusions rely on the provider's labels.
- Missing creator balances and insider indicators remain unknown.
- Generic `liquidity_controls` and `holder_distribution` map to the corresponding report checks. `contract_controls`, `sellability`, and `deployer_history` remain unknown. A token report does not establish complete control analysis or an executable exit route.
- Observed timestamps describe retrieval, not guaranteed freshness of the provider's underlying data. Production integration must establish source freshness separately.
- DEX Screener prices are indicative. Returned quotes always have `executable=False`; they cannot alone qualify for a paper fill. The default cost fields are explicit modeling placeholders, not measured route costs. A verified execution-quote adapter must supply actual quote/depth evidence.
- The latest-profiles feed supplies leads, not an exhaustive feed of token launches.

Official references, inspected 2026-10-26:

- https://api.rugcheck.xyz/swagger/doc.json
- https://docs.dexscreener.com/api/reference

The full Rugcheck report schema is not fully specified in its Swagger response. These adapters enforce the documented assumptions in tests. Newly observed incompatible payloads are rejected or marked unknown; never default missing risk evidence to a clean result.

## Deterministic trader ranking

```python
from sniper.ranking import rank_traders
result = rank_traders(records, decision_at=utc_epoch_seconds)
```

Each input record requires:

```json
{
  "wallet": "verified-wallet-identifier",
  "verified_history": true,
  "source": "reconciled-source-reference",
  "observed_at": 1793026000,
  "closed_trades": 60,
  "win_rate": "0.60",
  "top_coin_share": "0.20",
  "pnl_7d_usd": "100",
  "pnl_30d_usd": "200",
  "avg_hold_minutes": "120",
  "followers": 50
}
```

Defaults require twenty closed trades, 55% win rate, at most 50% profit concentration in one token, nonnegative last-week P&L, positive thirty-day P&L, and at least thirty minutes average holding time. These are experimental filters from the brief, not validated alpha. Eligible records sort explicitly by lower concentration, longer holding period (capped at a day for ranking), lower follower count, and sample size. At most three leaders are returned.

`verified_history=true` is an assertion supplied by the data producer. The ranking function does not independently establish it. A future wallet-history importer must reconcile transactions, exclude airdrops/allocations from copied-entry performance, account for open losses, and preserve source records. Freeze selections before measuring subsequent follower performance.

## Optional Grok annotation

No AI API is called unless the caller explicitly enables it. The API key is read only from `XAI_API_KEY`; it is never accepted in JSON evidence, written to logs, or stored in the budget database.

```python
from sniper.advisor import GrokAdvisor

advisor = GrokAdvisor(enabled=True, budget_path="var/advisory.sqlite3")
annotation = advisor.assess(
    request_id="asset:decision-unique-id",
    asset_id="solana:mint",
    now=utc_epoch_seconds,
    evidence=[{
        "id": "source1",
        "url": "https://example.com/source",
        "observed_at": utc_epoch_seconds - 1,
        "text": "A factual excerpt with source provenance."
    }],
)
```

The labels are `CLEAR`, `CAUTION`, `REJECT`, and `UNKNOWN`. None authorizes a trade. Every factual claim must reference supplied evidence IDs. References permit review; they do not independently prove that a claim follows from its source. Confidence is an uncalibrated annotation score.

The response must be one complete JSON object. Extra prose, duplicated keys, invented references, nonfinite confidence and BUY/SELL labels are rejected. Timestamp checks reject evidence unavailable at the decision time. External snippets are explicitly treated as data, not instructions.

### Cost behavior

The default daily reservation budget is USD 1.00, with USD 0.10 reserved before each bounded request. Reservations are persisted transactionally, retained on failure and retained on successful calls, so restarting or concurrent processes cannot reset the budget. Reusing an already reserved request ID is rejected. Output is limited to 1,000 tokens and source input to 20KB.

This is **reservation accounting**, not a verified vendor billing ledger. Vendor prices and reasoning-token treatment can change; reconcile actual invoices before relying on cost estimates. No X search or other server-side paid tools are enabled because `max_turns` does not bound fetched post/profile counts. Timestamped X evidence can be supplied through the same source interface after separate collection. The first version keeps research collection and paid annotation separate.

Official references:

- https://docs.x.ai/developers/model-capabilities/text/structured-outputs
- https://docs.x.ai/developers/tools/tool-usage-details
- https://docs.x.ai/developers/pricing

## Offline verification

```sh
python run_tests.py
```

Tests cover absent-vs-null evidence, unsupported routes, stable pool identity, wrong chain/mint, NaN/infinity/boolean values, largest-pool LP selection, partial holder data, strict AI schemas, nonexistent evidence, disabled AI, persistent budgets, repeat requests, failed-request accounting, future evidence, deterministic ranking and nonverified history.
