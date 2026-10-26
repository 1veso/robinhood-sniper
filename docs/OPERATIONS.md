# Operating SNIPER

## 1. Start with the isolated acceptance scenario

```bash
python run.py demo --html runtime/demo-report.html --csv runtime/demo-trades.csv
python run_tests.py
```

This demonstrates software behavior using explicit synthetic data. It is not a
backtest. Keep it separate from any measured experiment. `demo` always uses an
in-memory account, even if a `--db` option is supplied.

## 2. Create a frozen experiment

Use a named account path, such as `runtime/experiment-001.sqlite3`. The default
policy starts with $1,000 in simulated cash. Copy `config/paper.toml` before
changing capital or rules, record the rationale, and use a new database for each
policy. The full policy is saved with each decision, and the account refuses a
changed policy. Keep the previous experiment for comparison.

The runner also freezes its wallet file on first use. A selection change during
a run records an error and halts entries while exit processing continues. A new
selection on restart requires a new paper account. This prevents quietly mixing
old and new selection rules in one measured track record.

The account pins the source journal's durable identity as well. Keep its
`--events-db` file with the paper account; silently substituting a new event
journal is rejected. A proper SQLite backup preserves journal identity.

Use UTC epoch seconds for all evidence timestamps. Engine decisions, position
watching and reporting cannot move an account backward in time. A historical
replay belongs in its own database. After running a wall-clock report, do not
append an earlier historical replay to that account.

## 3. Collect observations

```bash
python run.py doctor --network
python run.py collect --events-db runtime/events.sqlite3 --max-blocks 100
python run.py collect --events-db runtime/events.sqlite3 --follow --interval 15
```

Add `--rpc https://YOUR-WORKING-RPC` if the public endpoint is unavailable. A
chain-ID mismatch is rejected. Keep credential-bearing RPC URLs private.
`--confirmations 5` means a five-block buffer; the wallet quorum of five is a
separate policy setting. This buffer is not a finality guarantee.

The first poll starts near the current confirmed head. Set `--start-block` to
collect an explicit earlier interval. Subsequent polls resume from the saved
cursor and do not skip intervening blocks. The selected interval determines
which pools are known, so the default is not a complete market history.

`collect` only journals observations. `run` connects collection to the paper
pipeline and position monitoring. Both default to one cycle; `--follow` keeps
them running, and `--iterations N` bounds a test run. They are ordinary Python
processes and stop if the computer sleeps or the process is terminated. No
cloud service is provisioned by this repository.

## 4. Supply reviewed wallet identities

The file is a JSON object keyed by actual EVM wallet addresses. Each record has:

```json
{
  "qualified": true,
  "cluster_id": "reviewed-controller-identifier",
  "qualification_evidence": "dated-research-record-or-transaction-history-reference"
}
```

The example shows one **record**, not a ready-made profitable address. The
shipped `examples/wallets.json` is `{}`. Five addresses with one controller must
share a cluster ID and count as one confirmation. The collector validates swap
attribution, not the truth of the qualification or independence assertion.

A useful qualification record includes the historical observation cutoff,
complete realized and open P&L, costs, funding/transfer exclusions, concentration,
holding times, controller research, and the follower's likely entry delay.
Freeze the selection before measuring later outcomes. A leaderboard screenshot
or a bare `qualified: true` value is not independent verification.

## 5. Provide safety and route evidence

The runner accepts `--safety PATH` and `--quotes PATH`. Both files are objects
keyed by the canonical asset ID, for example
`eip155:4663/erc20:0x...`. Values are domain records. File producers should write
a temporary file and atomically replace the previous manifest so the runner
never reads a partially written JSON document.

A safety record has this shape:

```json
{
  "asset_id": "eip155:4663/erc20:ACTUAL_ADDRESS",
  "observed_at": 1793016000,
  "source": "evidence-producer-and-version",
  "checks": {
    "contract_controls": null,
    "sellability": null,
    "liquidity_controls": null,
    "holder_distribution": null,
    "deployer_history": null
  },
  "details": {
    "report_pool_id": "ACTUAL_POOL_ADDRESS",
    "references": []
  }
}
```

The addresses and timestamp above are placeholders. Unknown checks stay null.
An explicit false check or an unknown mandatory check prevents entry. Evidence
must be no more than five minutes old by default. The pool in the evidence must
match the quoted pool. Renounced `owner()` alone does not establish complete
contract controls; a displayed price does not establish sellability.

A quote record has this shape:

```json
{
  "asset_id": "eip155:4663/erc20:ACTUAL_ADDRESS",
  "pool_id": "ACTUAL_POOL_ADDRESS",
  "observed_at": 1793016000,
  "price_usd": "1.00",
  "liquidity_usd": "100000",
  "buy_cost_bps": "100",
  "sell_cost_bps": "100",
  "gas_usd": "0.02",
  "executable": false,
  "source": "quote-provider-and-observation-reference"
}
```

Prices and costs are decimal strings. One hundred basis points equals 1%.
`executable` is false until supported by a suitable external route observation.
The application validates freshness, pool identity, shape and policy; it cannot
authenticate a manually asserted route or prove future exitability. It also
models impact itself rather than replaying V3 ticks. This interface is for
audited paper experimentation and still needs a size-aware route adapter before
realistic execution-performance claims are possible.

For entries, the pipeline obtains safety evidence first, records decision time,
then requests a quote. An entry quote observed before that decision or more than
30 seconds before processing is rejected. A static quote file will quickly
become stale; it needs a continuously refreshed evidence producer. Existing
positions also require current quotes for simulated exits.

Run the connected pipeline once providers are available:

```bash
python run.py --db runtime/experiment-001.sqlite3 run --follow --wallets runtime/wallets.json --safety runtime/safety.json --quotes runtime/quotes.json
```

Without those providers the system can collect and report, but cannot declare
a candidate eligible merely to generate activity. That is an intentional data
boundary, not an API or model failure.

### USD value of leader purchases

`run` requests indicative USD prices for USDG and WETH to estimate purchase
size. It does not assume a stablecoin peg. Alternatively, pass `--base-prices`
with a mapping from base-token address to
`{"price_usd":"1.001","observed_at":UNIX_SECONDS}`. Each observation must be
fresh within 30 seconds. The file is reloaded each cycle. Stale or unavailable
values cannot satisfy the minimum-wallet-spend rule. These current valuations
must not be presented as historical execution prices.

## 6. Monitor the account

```bash
python run.py --db runtime/experiment-001.sqlite3 report --html runtime/account.html --csv runtime/fills.csv
python run.py --db runtime/experiment-001.sqlite3 halt --reason "review source reliability"
python run.py --db runtime/experiment-001.sqlite3 resume
```

Keep the runner active after an entry halt so it can process exits. A source
failure does not fabricate a zero-price sale. Unpriced positions remain in the
ledger with conservative zero liquidation value until an acceptable quote is
observed. This may trigger the day's equity halt; the halt stays latched for that
UTC day even if the quote later recovers. The daily reference is the first equity
observation that day, not a reconstructed midnight valuation after downtime.

The paper stops execute against the next valid observed price after modeled
friction. A gap through −35% can therefore produce a worse simulated exit.
Polling cannot guarantee an immediate stop. The first +100% profit take sells
half of the original position; after that, a 30% drawdown from the observed peak
closes the remainder. Time exits use the current valid quote. This version does
not implement the article's second +200% ladder or mirrored leader sell orders.

The runner replays recent canonical source events after restarting. This closes
the crash window between the source journal and the account ledger. Saved reorg
notices are acknowledged together with signal invalidation. Reorgs invalidate
evidence and halt entries when affected confirmations exist; they do not erase
past paper fills or pretend a real trade has been reversed.

## 7. Optional research tools

```bash
python run.py rank --input runtime/traders.json
python run.py advise --input examples/advice.json
```

The second command returns `UNKNOWN` with an "AI advisory is disabled" message
without an API call. Paid annotation needs
the explicit `--enable-paid-ai` flag and `XAI_API_KEY` in the process environment.
`.env.example` documents the variable; this application does not silently load
`.env` files. Never commit a key or include it in an evidence file. The advisor
uses supplied timestamped references and does not enable X search tools.

Grok outputs `CLEAR`, `CAUTION`, `REJECT`, or `UNKNOWN`. These are annotations;
the deterministic engine does not treat them as buy orders. The configured
daily cost amount is a persistent reservation budget, not verified vendor
billing. See [Solana and AI adapters](SOLANA.md) for schemas and limits.

## 8. Measurement before deployment

Preserve every losing trade, rejected candidate, source outage, revised policy
and frozen watchlist. Review net results after estimated execution friction,
open exposure, concentration in the largest winners, drawdown, missed fills and
the difference between leader and follower prices. Compare a frozen strategy
with a time-matched baseline once a valid benchmark dataset exists.

An initial week tests operations. It does not establish repeatable profitability
or justify changing parameters after individual losses. The software supplies
an audit trail; statistical validation and credible route data are still needed
to establish whether the proposed strategy has an edge.
