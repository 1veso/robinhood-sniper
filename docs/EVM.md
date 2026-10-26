# Robinhood Chain read-only collector

This adapter observes Robinhood Chain and journals Uniswap V3 events. It does not
sign transactions, move funds, or establish a trading edge. Public network
connectivity must be verified in the deployment environment; offline test results
are not evidence that an RPC endpoint is currently reachable.

## Verified default registry

- Chain ID: `4663`.
- RPC: `https://rpc.mainnet.chain.robinhood.com`.
- V3 factory: `0x1f7d7550b1b028f7571e69a784071f0205fd2efa`.
- WETH: `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`.
- USDG: `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`.
- Explorer: `https://robinhoodchain.blockscout.com`.

Sources checked during the project audit, 2026-10-26:

- [Robinhood network connections](https://docs.robinhood.com/chain/connecting/)
- [Robinhood token contracts](https://docs.robinhood.com/chain/contracts/)
- [Uniswap Robinhood deployments](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments)
- [Canonical V3 factory interface](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/IUniswapV3Factory.sol)
- [Canonical V3 pool event interface](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)
- [ERC-20 transfer and decimal semantics](https://eips.ethereum.org/EIPS/eip-20)

The public RPC is rate-limited and is not a production service-level guarantee.
The collector refuses a chain-ID mismatch. A five-block confirmation buffer is
explicitly **not Ethereum finality**; an independent reorganisation policy still
applies.

## Public Python interface

```python
from sniper.adapters.evm import RpcClient, EvmCollector
from sniper.adapters.evm_store import EvmStore

store = EvmStore("runtime/evm.sqlite3")
collector = EvmCollector(RpcClient(), store=store)
result = collector.poll(max_blocks=500)
# JSON-ready keys: new_pools, swaps, verified_buys, errors, cursor, reorg.
store.close()
```

This first call starts at up to 500 confirmed blocks before the current head.
Pass `start_block` on the first poll to select an explicit historical starting
point. Later polls resume from the saved cursor. `scan(from_block, to_block)`
collects a specific range of at most 5,000 confirmed blocks, chunked into 500-block
requests by default. With an existing cursor it refuses to skip a gap. Use a
separate database when researching an unrelated interval.

The adapter monitors only pools created by the configured V3 factory and already
discovered in the recorded interval. It does **not** cover every token launch,
other AMMs, bonding curves, or historical pools outside the selected starting
point. It does not claim to know all smart-money wallets.

### Wallet attribution

`watchlist` is a mapping from validated EVM addresses to records:

```json
{
  "qualified": true,
  "cluster_id": "a-reviewed-economic-identity",
  "qualification_evidence": "reference-to-a-dated-wallet-review"
}
```

The outer key must be the actual 20-byte address. The default watchlist is empty.
Addresses are never invented or automatically declared profitable. Distinct
`cluster_id` values represent an upstream review, not an independence conclusion
made by this collector. `independence_verified_by_collector` remains false.

A `verified_buy` requires all of the following:

1. A decoded Swap event from a pool discovered through the configured factory.
2. A successful transaction receipt matching the event's transaction and block.
3. The tracked wallet is the transaction sender.
4. A transfer of the configured ERC-20 base **from that wallet to that pool**.
5. A transfer of the acquired token **from that pool to that wallet**.
6. Those amounts exactly match the pool's signed Swap amounts and the wallet's
   net transfers for the two assets in that transaction.

Unsolicited transfers, distributions, reverted transactions, and unrelated
payments in the same transaction are not purchases. Native-ETH wrapping routes,
complex multi-pool routing, fee-on-transfer tokens, and delegated smart-wallet
transactions are deliberately left unverified. A future adapter can support them
only with explicit, tested attribution logic.

This proves an on-chain event relationship. It does not prove that a token's
implementation is honest, that its transfer events reflect economic value, that
the wallet is independent, or that a follower could sell.

### Returned purchase fields

Important fields include `asset_id` (`eip155:4663/erc20:...`), `pool_id`, `wallet`,
`tx_hash`, `block_hash`, `amount_raw`, `amount`, `spent_raw`, `spent_amount`,
`spent_usd`, `symbol`, `cluster_id`, `qualified`, `qualification_evidence`,
`observed_at`, and `chain_timestamp`.

- `observed_at` is the local collection time.
- `chain_timestamp` is the transaction's block time. **Use this for the engine's
  wallet-confirmation freshness check.** A historical backfill must never be
  treated as a fresh wallet signal merely because it was downloaded now.
- Token amounts use validated on-chain decimals, never an assumed 18.
- Decimals outside the supported 0–36 range, missing decimals, or malformed
  amounts prevent normalization into a purchase confirmation.
- USD spend is unknown unless an explicit fresh base-price observation is
  provided. The adapter never assumes USDG is exactly one dollar.

An optional price input has this form:

```python
quote_usd = {
    base_address: {"price_usd": "1.001", "observed_at": unix_seconds}
}
```

Prices must be positive finite decimal strings and no more than 30 seconds old.
These are current valuation observations. They are not historical transaction
execution prices and must not be used to manufacture historical wallet P&L.

## Contract observations remain fail-closed

`collector.contract_safety(token_address)` reads bytecode and an optional
`owner()` value. It returns a SafetyEvidence-shaped dictionary. All policy-level
checks (`contract_controls`, `sellability`, `liquidity_controls`,
`holder_distribution`, `deployer_history`) remain `null`, because these simple
reads do not establish them.

Renounced ownership does not prove that separate permissions disappeared. An RPC
failure is unavailable evidence, never proof that minting is disabled. There is
no `mint()` probing shortcut in this implementation. EVM candidates require
separately obtained, dated safety evidence before the paper engine can admit
positions.

## Persistence, failures and reorganisation

`EvmStore` uses SQLite transactions with a full durability setting. Each block
chunk saves its event versions, block checkpoints, and cursor together. Failed
receipt retrieval does not advance the cursor. Repeated scans do not duplicate
canonical observations.

A saved cursor is compared to the RPC's block hash before polling. If it changed,
the collector locates a verified saved common ancestor, marks later observations
noncanonical, records the rewind, and replays the interval. Old evidence is
retained for audit. If no common ancestor is found within the configured horizon,
it raises `ReorgError` and stops. The chunk's end-block hash is anchored before
collection and checked again before commit, so a detected reorg during collection
cannot commit mixed observations.

The caller must consume only canonical events. Reorganisation recovery is an
observation correction, not an instruction to reverse an actual trade or to
rewrite prior simulated decisions without an audit trail.

RPC access is limited in code to read methods. Transaction submission, signing,
and wallet methods are rejected before reaching the transport. Retries are
bounded and HTTP errors are reported without printing a credential-bearing RPC
URL. Malformed events are listed in `errors`; they are not interpreted as valid
signals.

## Test coverage

Run:

```bash
python run_tests.py
```

Tests use a deterministic fake chain. They cover the original wrong-word pool
address bug, signed amounts, token decimals, wrong chain, RPC errors, disabled
transaction methods, real payment vs airdrop/transfer, attributable direct pool
flows, missing qualification evidence, stale USD evidence, restart/deduplication,
atomic failure, common-ancestor recovery, deep reorg halt, and reorg during a
chunk. No real transactions or funded accounts are used.
