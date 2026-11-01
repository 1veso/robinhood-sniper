# Xenon / Argon factual collectors

The collectors are read-only inputs to the existing paper admission path. They
never sign, broadcast, create fills, change strategy thresholds, or grant entry
permission. Argon's original independent review and provenance checks remain.

## Holder coverage

`holder_evidence.py` enumerates all classic SPL or Token-2022 accounts matching
the mint, counts distinct owners of positive balances, and reconciles visible
balances to minted supply. It includes infrastructure owners; it does not call
owners independent controllers. Token-2022 accounts are not truncated with a
classic 165-byte filter. Confidential or withheld amounts that prevent supply
reconciliation leave coverage unknown.

Default RPC is `getProgramAccounts`. Optional `getProgramAccountsV2` supports up
to ten pages / 10,000 accounts. All pages must share a finalized context slot,
have unique accounts/cursors, and end with an explicit null cursor. A short page
is not completion. Slot drift, an unfinished cursor, a supply change, or a bound
exceeded fails closed. The limit is a collection bound, never an approximation.

To select pagination after confirming provider access, use the ignored file
`runtime/crew/collector-settings.json`:

```json
{"holder_rpc_method": "getProgramAccountsV2"}
```

On November 1, 2026 the configured Alchemy Free app returned a compute-capacity
429 for full enumeration and an explicit 400 / -32600 plan denial for V2.
No subscription was changed. The user subsequently supplied a Helius endpoint.
It is configured exclusively for holder reads: a live finalized snapshot at slot
464136490 enumerated 4,499 accounts, reconciled the complete supply and counted
1,276 positive-balance owners. The processing bound is now 10,000 accounts, with
the existing 3 MB response limit retained. This is a coverage bound, not a trading
strategy threshold. Controller independence is still unknown.

## Pool / exit evidence

The producer now supports direct Raydium CP pools with WSOL or USDC, and direct
PumpSwap pools with WSOL or USDC. PumpSwap validation checks owner/discriminator,
pool/LP/vault PDAs, token ownership and precision, initialized vaults, dynamic
SOL/stable fee schedules, creator fees, signed virtual quote reserves, retained
fee buckets, real quote liquidity and integer rounding. Only metadata-related
Token-2022 mint extensions and immutable-owner account extensions are supported;
transfer hooks/fees and unknown economic extensions fail closed. Mayhem/cashback,
custom quote tokens, multi-hop pools, and pre-migration bonding curves remain
unsupported, rather than being treated as normal PumpSwap pools.

Quotes use the unchanged 3% impact bound and intended paper ticket. Raydium and
PumpSwap deployment pins still require the existing account-bound, dated review.
The independent reviewer recomputes quote arithmetic and checks that the model,
program, quote asset and precision agree. Legacy `*_usdc_raw` route field names
are retained for compatibility; the explicit `quote_mint` and `quote_decimals`
identify their units. A quote is not wallet simulation or a fill, excludes
network costs, and does not apply hypothetical buy state to the sell quote.

Live validation decoded finalized pool
`6qfkD7d8RaFVAn1FC76M5MtqAr3kHrHpxPwMQvL6Ndxt`, including its nonzero virtual
reserves, and produced a fee-aware quote for the configured $25 ticket at slot
464132887. This establishes read-only quote functionality, not entry readiness.

## Buyer / controller evidence

`buyer_evidence.py` reads a bounded finalized signature page and up to four
receipts. It recognizes supported direct or inner PumpSwap buys and Raydium CP
base-input buys, binds the pool and mint, verifies the buyer's signer role and
matching token-account owner, and records the net acquired raw amount. Failed,
misbound, multi-buy or unresolvable receipts do not become buyer measurements.
Funding transfers, signers and fee payers are journaled as observations.

A live finalized PumpSwap receipt was collected with one positive acquisition
and matching signed authority. Other samples legitimately contain no buys.
These are public chain trades by others, not this bot's paper or wallet fills.

Distinct signers, owners and funders do not prove independent beneficial
controllers. Consequently `verified_independent_buyers` remains null and the
independence gate remains unknown without independently supported controller
attestations. A bounded sample is not the full one-hour buyer population.
No independent-buyer count or largest independent-buyer share is fabricated.

## Reproduce / operational limits

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/validate-solana-evidence.py --mint <mint> --pool <pool>
```

Use `--skip-holders` while the known provider failure is unresolved, or
`--holder-method getProgramAccountsV2` after enabling provider access. The command
makes bounded read-only requests and saves nonsecret diagnostic evidence under
ignored `runtime/crew/live-diagnostics/`. It does not initialize books or record
fills. Source outages do not trigger retry loops.

Original exact one-hour paired price/holder history, independent controller
review, deployment review, model decisions and other strategy checks are still
required. These collectors never erase seen-once state or manufacture historical
observations. A running clock or a successful quote is not a completed trial.

References (primary sources):
- https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json
- https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md
- https://www.npmjs.com/package/@pump-fun/pump-swap-sdk/v/2.1.0
- https://www.alchemy.com/docs/chains/solana/solana-api-endpoints/get-program-accounts-v-2

## Free RPC alternative: Helius

Helius Free currently lists 1,000,000 credits/month, 10 standard RPC requests/sec
and 5 getProgramAccounts requests/sec. The latter costs 10 credits. Use standard
getProgramAccounts with finalized withContext, not an untimed DAS holder total.
Actual successful supply reconciliation is required before marking coverage valid.

1. Visit https://www.helius.dev/ and choose Start for free.
2. In the dashboard API Keys section, copy the Mainnet HTTPS RPC endpoint.
3. Save only that URL in C:\Users\benja\Downloads\helius-rpc-url.txt.
4. Supply the file path to the local agent; never put the credential in chat/Git.
5. The holder-only destination is runtime/crew/holder-rpc-url.txt (ignored).
   Other RPC workloads continue to use runtime/solana-rpc-url.txt.
6. Validate before activating with --holder-rpc-file pointing to the saved file.

Provider docs: https://www.helius.dev/pricing and
https://www.helius.dev/docs/rpc/guides/getprogramaccounts . Free credits are finite;
no paid plan, automatic top-up, or excess entitlement is assumed. The supplied Helius endpoint passed full live supply reconciliation on November
1, 2026 and is now installed for holder-only reads.
