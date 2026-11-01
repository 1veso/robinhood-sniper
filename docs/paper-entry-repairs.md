# Paper entry repairs — 2026-11-01

Real execution remains disabled. Existing accounts, balances, trade history, risk thresholds, and paid model authorization are preserved. These repairs do not promise trades when no candidate passes its strategy.

## Implemented

- EVM RPC recognizes the observed Alchemy Free 10-block log limit and partitions the exact requested range. Batches preserve request identities and filters; incomplete or out-of-range results fail closed. A live read of 500 blocks returned 385 swap logs in 6.52 seconds after the old request failed with HTTP 400.
- Exploratory collection journals completed raw log chunks across bounded worker runs. These are not canonical signals. The original interval anchor, all selected event headers, and expected canonical cursor must pass before admission; a changed anchor discards unadmitted progress. Stale progress is bounded to 15 minutes, and skipped canonical blocks remain disclosed. The per-cycle collection slice is 20 seconds so already-admitted signals receive entry-evaluation time within the unchanged total worker budget.
- Krypton and Helium can use the authorized local `runtime/holder-rpc-url.txt` endpoint for holder reads. The configured endpoint is ignored by Git; no credentials are in this change.
- Holder enumeration accepts reviewed Token-2022 metadata-only mint extensions and immutable-owner token accounts. Transfer fees, hooks, authority-altering extensions, unknown extensions, mismatched token programs, and unreconciled supply remain rejected. The original enumeration bound and concentration limits remain in force.
- Unavailable pre-model evidence is retried after at least 60 seconds, with three total attempts and the original discovery expiry. Definitive risk failures remain terminal. Helium resumes only a task with no completed model/evidence artifacts. Uncertain paid requests are never automatically replayed.
- Nitrogen divides its existing four-receipt allowance among tracked wallets and rotates their order. No tracked wallet, supported trade definition, or approval rule changes.

## Live observations and remaining requirements

- The supported Token-2022 holder path was validated against finalized Solana state. The sampled token had a 12.903% owner lower bound and correctly failed the existing 10% maximum. This is evidence of a functioning rejection, not a fill.
- Sampled archived Pump instructions were fee distributions. They are not purchases and must not be promoted into buy signals.
- Xenon/Argon still require factual buyer-controller independence, aligned historical observations, and reviewed program/exit evidence. Distinct signed wallets alone do not prove independent human control.
- Neon/Radon still require complete, inventory-reconciled trader histories and historical cost/price evidence before leader qualification. Headline leaderboard P&L and a partial transaction archive do not meet those requirements.
- Nitrogen also retains its complete factual dossier and original explicit paper-order approval requirement.
- Krypton/Helium may remain idle when actual liquidity, pool availability, concentration, or LP checks fail. Oxygen retains the original article source and strategy.

The scheduled EVM worker subsequently collected 3,000 live blocks, admitted 53 swap legs across 32 sampled transactions, and attempted nine funded-purchase proofs. That is live signal processing, not proof of a strategy fill. No new production paper fill is asserted by this change. Fixture-based lifecycle tests use separate temporary demo accounts and are excluded from live-data strategy results.

## Validation

The complete pre-final suite passed 1,281 tests. Dedicated regressions cover exact log partition coverage, HTTP error handling, incomplete responses, restart/reorg cursor safety, metadata-only Token-2022 parsing, retry recovery, bounded attempts, no paid replay, and tracked-wallet fairness. Final CI runs the complete suite on the supported Python/OS matrix.
