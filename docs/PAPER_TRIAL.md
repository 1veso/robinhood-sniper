# Live-market paper trial

The chosen host is the user's Windows PC. It must remain powered on, awake,
connected, and signed in. The scheduled paper worker and two-minute Telegram
monitor operate locally. An hourly Codex follow-up supervises setup and results;
it is not the trading loop. Real orders and paid xAI calls remain disabled.

## Current stage

Preparation, not an active seven-day strategy trial. Collection catch-up was
verified on 2026-10-27: a bounded 12-cycle pass retained the original cursor and
finished 5–10 blocks behind the observed head, without source errors. No blocks
were skipped to create this result. Normal scheduling has been restored.

Wallet qualification is still required. Unsupported/oversized histories are
recorded as unqualified, not silently truncated. Discovery has a separate small
budget, and history resumes its active wallet with immutable cached evidence.
The worker can complete up to five small/rejected candidates per cycle. The
Blockscout limit remains 1,000 requests per UTC day; archive prechecks reject
unsupported accounts before consuming index requests. A budget pause is not a
qualification. No automatic assumption of independent ownership is made.

## Live adapters

`run --live-evidence` uses free GoPlus token screening and read-only Uniswap V3
QuoterV2 calls on chain 4663. The service enables this option. The Telegram
monitor also records up to two fresh GoPlus observations per cycle in
`runtime/safety-observations.json`.

The route adapter verifies the quoter's factory/WETH references, the pool's
factory registration, token precision, both quote directions, size coverage,
block freshness and an unchanged block hash. It records raw quote quantities in
the paper ledger. Entry size cannot exceed quoted coverage, and an uncovered or
wrong-pool exit cannot manufacture a sale. Pool fees are included in quoted
costs and excluded from the separate price-impact gate.

Fills remain simulations. Two independent same-block quotes are not an executed
round trip or proof of future sellability. Quotes include 100 bps additional
adverse-execution stress plus the existing liquidity-impact stress model. Gas is
estimated as twice (quoter gas + 100,000) at the current gas price and ETH/USD
reference. This is a conservative assumption, not a signed-transaction estimate.
No wallet, approval, signature or transaction-broadcast method is used.

GoPlus absent fields remain unknown. Proxy/mint/control flags, zero-transfer-tax
sellability screening and holder concentration are evaluated explicitly. Provider
retrieval time is known; the provider does not supply a canonical analysis block
or timestamp. A clean creator flag is not a complete deployer history. V3
liquidity control and deployer history still need separately verified scoped
evidence in `runtime/safety.json`. A current external review can resolve unknowns,
but cannot override an observed failure. This release does not invent those
reviews or remove the strategy's safety gates.

Verified public references:

- [GoPlus response meanings](https://docs.gopluslabs.io/reference/response-details)
- [GoPlus token API](https://docs.gopluslabs.io/reference/tokensecurityusingget_1)
- [Robinhood V3 deployment addresses](https://docs.hoodl.app/robinhood-chain/)

## Starting the measured week

Before recording a trial start, establish at least five qualified independent
wallet clusters with reviewed evidence, functioning safety providers/reviews,
fresh bounded routes, and a collector maintaining current data. Keep all
existing risk and exit settings. Create a separate named paper account and
source journal, freeze its wallet cohort/policy/version, and preserve the old
observation account. Expired evidence must continue to block new entries.

Record the actual UTC start and seven-day end, heartbeat gaps, provider failures,
signals accepted/rejected, every simulated fill and its evidence, fees, gas,
slippage assumptions, realized/unrealized P&L, and drawdown. A missed or sleeping
interval must not be reported as continuous operation. At the end stop new
entries and retain monitoring of outstanding positions. Zero qualifying trades
is an inconclusive strategy sample, not a profitable result.

Review the completed evidence before any separate live-trading decision. There
is no automatic switch to real money and this release implements no signer.
