# Execution costs and live preparation — 0.1.9

The user's 2026-10-27 request authorizes adding execution costs and preparing a later live handoff. Existing trading thresholds, exits, wallet rules and `rug_desk/config.py` remain unchanged. The Solana account gets an explicitly versioned cost model before its first strategy fill. The existing Robinhood paper engine already includes same-block V3 pool fees/impact, 100 bps adverse execution stress, and conservative gas: twice the larger quoter estimate plus 100,000 gas units, valued at the current gas/ETH price. That is still a paper estimate, not a wallet-specific executed transaction.

## Solana paper accounting

The authoritative policy is recorded in the account database under `execution_cost_policy`. `runtime/execution-cost-policy.json` is a reviewable copy, not an untracked override. Activation requires an empty paper account before its trial. Existing fills are never retrospectively rewritten to look cost-aware. The archived article label in the unchanged `config.py` describes the legacy model; reports and alerts use the active ledger policy.

The initial cost scenario assumes:

| Component | Initial assumption |
|---|---|
| DEX fee | 30 bps per side |
| Platform fee | 10 bps per side |
| Adverse slippage | 100 bps per side |
| Liquidity impact | Constant-product depth approximation from reported pool USD liquidity |
| Successful network fee | 5,000 base + 100,000 priority lamports per fill |
| Failed attempts in baseline | Zero; no real transaction is submitted in paper mode |
| Additional stress | Another 100 bps per fill and two extra network-fee equivalents: a doubled successful fee plus one failed attempt |
| Token-account rent reserve | 2,039,280 lamports on entry; reserved capital, not an expense or an assumed refund |

These are transparent scenario assumptions, not measured fees for every pool/token program. Concentrated liquidity, transfer fees, changing priority fees, MEV and failed exits can differ. Current SOL/USD observations value native costs; missing, nonfinite or stale observations block the simulated fill instead of assigning zero costs. Paper prices already embed modeled DEX/platform fees, price impact and slippage. Network expenses are charged separately once. Entry network costs immediately reduce realized net P&L and daily loss capacity. Partial sales allocate the remaining cost basis proportionally; a full close consumes the exact remaining basis without rounding dust.

Open positions are marked at their modeled net liquidation value, including an exit fee. A missing/stale mark prevents a complete net-profit claim. The report shows realized P&L, open losses, billed xAI costs, unknown usage, extra execution-cost stress and rent reserves separately. It does not add embedded fees twice. The stress calculation keeps the observed trade path; it is not a rerun of strategy decisions under different fills.

Activation command, used only for the authorized empty Solana account:

```powershell
& .\rug_desk\.venv\Scripts\python.exe rug_desk/book.py --enable-costs
```

## Review before a live decision

The report's draft review criteria require seven days of the cost-aware trial, at least 30 completed positions, reconciled API costs, current open-position marks, positive net results after costs and open losses, positive results after the extra cost shock, profitable chronological halves, positive closed-trade results after removing the best trade, and review of operational errors. These are practical review gates, not statistically proven evidence of an edge. They can identify insufficient evidence; they cannot guarantee future profit.

Passing them sets **performance ready for human review**, never `real_money_enabled`. No profitable balance, deadline or configuration flag causes a wallet to sign. The user still reviews fresh safety/route evidence, approves capital and loss limits, approves the transaction, and signs in their wallet. The first live step should be a deliberately bounded, separately authorized execution validation; paper and real holdings remain separate.

The existing Robinhood trial keeps its account/deadline and its own results. The Solana trial still waits for funded xAI inference/X Search validation. Do not combine their P&L or count synthetic acceptance fills as performance.

## Public inputs and provider checks

Credentials stay in the parent application's `runtime` directory:

- `jupiter-api-key.txt`: a free key from [Jupiter Developer Portal](https://developers.jup.ag/portal), recommended for operational rate limits. Keyless public quotes are available and were separately checked; this is not proof of wallet-specific execution.
- `solana-trading-address.txt`: only the public address of the intended separate trading wallet. No seed or private key.
- Optional `solana-rpc-url.txt`: an HTTPS Solana mainnet RPC endpoint. Otherwise the public Solana mainnet endpoint is used. The Robinhood RPC URL is never reused as a Solana RPC.

From the installed application folder:

```powershell
& .\rug_desk\.venv\Scripts\python.exe prepare_live.py status
& .\rug_desk\.venv\Scripts\python.exe prepare_live.py doctor
```

`doctor` independently checks the complete Solana mainnet genesis hash and one public Jupiter WSOL/USDC quote. It does not request an assembled transaction, sign or submit anything. A successful public quote does not establish network/rent fees for the user's wallet.

## Prepare a specific transaction for review

Once the public wallet and free key are saved, the preparation command checks fresh Rugcheck evidence, pins its pool, fetches current market data, requests an ExactIn Jupiter V2 order, validates token amounts, minimum output, target pool, intermediate assets and fee fields, and optionally simulates the unsigned transaction. It restricts the target token's route to the screened pool and intermediate assets to WSOL/USDC. Unsupported gasless/sponsored fees or unresolved schemas are rejected. A quote can fail because the wallet has no balance; do not fund it merely to make setup look complete.

New entries must pass the unchanged safety and market filters. An exit preparation records safety deterioration or provider failure without applying entry-only filters that could strand an existing holding; its route still must match the specified holding pool and quantity, and manual review remains required.

```powershell
& .\rug_desk\.venv\Scripts\python.exe prepare_live.py quote --mint ACTUAL_MINT --pool SCREENED_POOL --side buy --amount-raw 25000000 --simulate
```

The example input is **25 USDC in raw units**, not a guarantee that USDC equals $1. Amounts and capital allocation need review before any real order. Sell preparation uses `--side sell` and the actual token's raw quantity. Both buy and exit routes must be checked for the exact intended size before authorizing a live entry.

The result is a review artifact in `runtime/live-preparation/intents/`. It records transaction and message hashes, short quote expiry, expected outputs, fee estimates, safety observations and simulation results. Simulation replaces the recent blockhash and does not verify signatures. It does not reserve liquidity or guarantee landing. Independent instruction/program/recipient review is explicitly unverified and must be completed before a signing handoff. There is no signer, `/execute`, `/submit` or `sendTransaction` method in this application.

## Actual fills and costs

After a user-authorized transaction really occurs, reconciliation requires finalized RPC status, matching parsed/binary receipts and matching message bytes from the prepared intent. The first signature must match. Actual wallet token deltas establish quantities and USDC spent/received. Output below the intended minimum, a different input amount or unclassified native outflows require manual reconciliation and remain visible in the receipt journal.

- Actual swap fees, transfer fees, price impact and slippage are already reflected in token amounts. They are not charged again as guessed percentages.
- `meta.fee` includes network priority fees. It is charged once.
- Token-account rent deposits/refunds are tracked as capital changes, not trading profit/loss.
- A finalized failed transaction records its actual network expense and no swap.
- Duplicate signatures cannot create duplicate fills or fees. A conflicting replay is rejected.
- USD conversion needs explicit time-matched SOL and USDC price evidence. Today's price cannot silently revalue an old fill. Native/USDC raw amounts remain the canonical evidence.

```powershell
& .\rug_desk\.venv\Scripts\python.exe prepare_live.py reconcile --intent PATH_TO_SAVED_INTENT --signature ACTUAL_SIGNATURE
& .\rug_desk\.venv\Scripts\python.exe prepare_live.py reconcile --intent PATH_TO_SAVED_INTENT --signature ACTUAL_SIGNATURE --valuation-json PATH_TO_VALUATION --record
```

The first command only journals the finalized evidence. `--record` books it into `runtime/live-preparation/confirmed-account`, an independent manual account. It cannot mutate the paper ledger. Valuation JSON contains positive `sol_usd`, `usdc_usd`, integer `observed_at` within 120 seconds of the fill's block time, and a descriptive `source`. This is evidence supplied for bookkeeping; naming a source does not independently verify its price. Unresolved account movements must be reconciled before further live activity.

## Remaining live prerequisites

The current setup does not claim profitability, a completed Solana week, verified wallet-specific execution, independently reviewed transaction instructions, a funded xAI gate, or authorization to move money. The public wallet/key, actual two-sided routes/simulation, reviewed capital/kill-switch limits, independent instruction review and explicit signing approval are still required. The prepared code and tests are reusable when those inputs arrive; no strategy filters are lowered to manufacture a passing trade.

Primary sources checked 2026-10-27: [Jupiter order API](https://developers.jup.ag/docs/api-reference/swap/order), [Jupiter plans and keyless/free access](https://developers.jup.ag/pricing), [Solana fee structure](https://solana.com/docs/core/fees/fee-structure), [Solana simulation](https://solana.com/docs/rpc/http/simulatetransaction), [Solana finalized transaction retrieval](https://solana.com/docs/rpc/http/gettransaction), [full mainnet genesis hash versus abbreviated network identifier](https://solana.com/docs/payments/agentic-payments/x402).
