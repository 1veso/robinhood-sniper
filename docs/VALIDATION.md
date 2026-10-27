# Validation and scope

## v0.1.1 Windows acceptance, 2026-10-27

**140 tests passed on Python 3.12.10**, including the original Windows failure
case. The demo, CSV/HTML exports, offline doctor and network doctor completed in
the extracted release folder. Mainnet RPC returned chain ID 4663 with TLS
verification enabled. A locally supplied Blockscout key returned HTTP 200 from
the mainnet historical API. An integrated observation/research cycle completed
with healthy paper-source status and blocked entries pending required evidence.

Added checks cover SQLite cleanup on constructor failure, provider chain-name
mapping, RPC credential redaction, overlapping service prevention, source timeout
handling, FIFO partial exits, gas accounting, airdrop exclusions, open-inventory
stress, untouched old holdings, historical-price timing, receipt/balance
reconciliation, factory identity, failed pagination, funding clusters, and dated
controller-review conflicts. CI now covers both Windows and Linux.

Automatic discovery has produced real candidate addresses; no historical wallet
has yet been declared qualified in this acceptance run. History processing is
bounded and resumes from cached evidence. This validates plumbing and selected
failure cases, not strategy profitability or live order execution. Operational
details and limitations are in [AUTONOMY.md](AUTONOMY.md).

## What was verified

Local acceptance on **2026-10-26**: **119 tests passed on Python 3.12.14**. The
isolated synthetic demonstration, chronological replay, CSV/HTML exports,
disabled-advisor path and default configuration check also completed.

The offline suite exercises actual account transitions and source failures:

- Cash, cost basis, quantities, fees, partial exits and whole-position statistics.
- Atomic decisions, duplicate protection, concurrent SQLite connections and restarts.
- Full-loss entry sizing, aggregate risk, daily equity halts and immutable policy.
- Unknown safety, stale/future/mismatched quotes, failed feeds and gap-through stops.
- Correct V3 event layout, signed swaps, actual token decimals, direct purchase
  attribution, failed receipts, wrong chain and reorg recovery.
- Durable wallet quorum, controller clustering, dust, historical timestamps,
  evidence latency and retry after a missing provider recovers.
- Strict JSON, finite numbers, escaping of untrusted HTML/CSV metadata, advisory
  schema/source references and persistent paid-request reservations.
- End-to-end CLI demonstration, replay validation and runner recovery paths.

Run all checks with `python run_tests.py`. The synthetic demonstration is also
run in CI. Local verification used Python 3.12. The workflow additionally targets
Python 3.11 and 3.13; a configured workflow is not itself proof of a remote pass.

## Network verification

The read-only `eth_chainId` request to
`https://rpc.mainnet.chain.robinhood.com` returned HTTP 403 from the build
environment. This establishes an access failure from that environment only;
it does not establish a chain outage or a defect in the endpoint. Live feeds,
latency, on-chain coverage and rate limits still need verification on the
operator's host. No funded account, wallet signature or real order was used.

## What the implementation does not prove

1. **Profitability.** No historical or prospective strategy return is claimed.
   Fixture P&L tests accounting, not market performance.
2. **Wallet quality.** Qualification and independence assertions need external
   transaction history and controller research. The shipped watchlist is empty.
3. **Complete token safety.** Rugcheck and simple contract observations are
   screening evidence. They do not prove that a token cannot rug or become
   unsellable.
4. **Executable pricing.** DexScreener screens stay indicative. External route
   evidence is required to admit an entry, and the current fill model remains a
   price-plus-cost simulation with a simple liquidity proxy. It does not replay
   concentrated-liquidity ticks or MEV behavior.
5. **Complete market coverage.** The initial EVM collector covers one factory,
   discovered pools and attributable direct ERC-20 flows. It does not cover all
   AMMs, native wrapping routes or delegated smart accounts.
6. **A backtesting platform.** Chronological event replay is implemented. A
   complete point-in-time history, benchmark, holdout protocol and statistical
   evaluation are not supplied by this repository.
7. **An always-on deployment.** The loop runs while its Python process runs.
   Provisioned hosting, monitoring services and an operational service-level
   guarantee are outside this version.
8. **Live execution.** No signing, broker execution, copyfomo connection or
   QuantGPT subscription is implemented. The architecture exposes paper
   execution only and rejects live-mode configuration.

## How the article ideas map to this build

| Source idea | Implemented here |
|---|---|
| Copy-trader selection and filtering | Dated deterministic ranking, reviewed-wallet inputs and five-controller quorum; no invented copyfomo API bridge |
| Research/data/compute separation | Separate adapters, replay, policy, engine and ledger; no requirement to purchase QuantGPT |
| Rotate after two losses | Not adopted: the screenshot did not establish a validated rotation rule |
| Rug filter, ledger and exits | Strict screening, unknown-data rejection, persistent account and deterministic paper exits |
| Scanner/analyst/risk/executor roles | Python modules with explicit interfaces; no multi-agent vote treated as statistical evidence |
| New-launch and whale monitoring | Correct V3 decoding, read-only source journal and receipt-based direct swap attribution |

## Next evidence required for a measured experiment

Use a working read-only RPC, a frozen set of reviewed wallet/controller
identities, and independently sourced dated safety and size-specific route
observations. Collect timestamped outcomes and follower execution estimates
without retuning the rules. Build a comparable benchmark from the same
observation window. Only those data can support a decision about whether to
continue, revise or reject the proposed strategy.
