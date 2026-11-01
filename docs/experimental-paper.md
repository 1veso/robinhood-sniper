# Experimental paper variants

Explicitly authorized on 2026-11-01. These are eight new deterministic proxy strategies, not a modification or validation of the original bots. Original ledgers, policies, paid model permissions, wallet configuration and live gates are untouched. No original live-readiness system consumes these experimental results.

Each variant owns `runtime/experiments/<bot>/book.sqlite3`, a unique account UUID and immutable policy hash. The initial trial lasts exactly seven days from enrollment; repeating initialization cannot reset cash, positions or dates. Nitrogen starts with simulated $500 and the other variants with simulated $1,000. No real capital is used.

## Explicit proxy replacements

| Variant | Observable experimental rule | Claims deliberately unestablished |
|---|---|---|
| Xenon | Hourly buys >=20, buy/sell ratio >=1.05, nonnegative hourly price change | Independent buyer controllers and complete history |
| Argon | Liquidity >=$100,000, >=10 hourly trades, hourly volume/liquidity >=0.02 | Jev/Grok review or original dossier approval |
| Neon | >=40 hourly trades, buys >=sells, hourly volume >=$10,000 | Skilled leader, performance history, or copied trades |
| Krypton | Sampled non-vault owner lower bounds <=10% single and <=40% top ten | Complete owner coverage or unseen concentration |
| Helium | Xenon and Neon proxy rules both pass, liquidity >=$50,000 | Paid team/model consensus |
| Nitrogen | Average hourly trade notional >=$100 over >=10 trades | Identified whales, actual individual purchases or controller independence |
| Radon | Hourly volume/liquidity between 0.1 and 5 over >=20 trades | Inventory-reconciled trader performance or copy qualification |
| Oxygen | >=5 five-minute buys, buy/sell ratio >=1.1, nonnegative five-minute price change | X sentiment and article strategy fidelity |

These versions make no paid model requests. They share a public read-only evidence feed to avoid duplicate calls, but cash, entries, positions, exits and outcomes stay separate. A common market observation can independently qualify several accounts.

## Evidence and limits

Discovery uses the latest DexScreener Solana token profiles. Activity, volume, creation time and percentage changes are provider aggregates, not independently reconstructed transactions. Pool and mint structures, pool-owned vaults, token programs, effective reserves and fees are read from finalized Solana RPC snapshots using the authorized local endpoint. Supported routes are reviewed-layout Raydium constant-product and PumpSwap WSOL/USDC pools; no wallet connection, instructions, signing or broadcasting occurs. USDC is valued at $1 in this paper model; native SOL/USD comes from a current DexScreener WSOL/USDC observation.

Every entry requires at least $25,000 effective liquidity, ten minutes pool age, disabled mint/freeze authorities, compatible token extensions, positive pool reserves, a bounded read-only exit estimate, a chain/market price discrepancy <=20%, current evidence, and no observed non-vault owner lower bound above 20% / top-ten above 60%. Krypton uses its stricter sampled limits. Verified pool vaults are excluded from this sample. Absence of a large sampled owner is not proof of dispersed ownership. LP locks, program audits, full holder coverage, controller independence and future sellability remain unknown. No missing measurements are filled with invented values.

Each entry uses at most 1% equity including modeled entry fees and reserved token-account rent. Maximum three positions per account; no repeat mint entries during the trial. Fresh marks are required before opening additional positions. The daily loss halt is 5% on conservative paper equity. Exits use a 20% stop, 30% take profit, six-hour maximum hold, or trial end. Stale/unavailable prices retain unresolved holdings; no fabricated exits. Rent remains reserved and is not automatically refunded. Conservative equity excludes reserved rent and labels stale marks.

Fill prices include pool fees (at least the conservative 30bps baseline), 10bps platform-fee assumption, 100bps adverse slippage, a liquidity-impact approximation and SOL-valued network-fee estimates. These are simulations, not executable quotes or verified wallet fills. Full evidence records bind each fill; public raw captures are archived by hash when collected. Evidence is never substituted retrospectively.

The worker rotates discovery, prioritizes oldest position marks, uses bounded request deadlines, and backs off for two minutes after a provider failure instead of retrying in a paid loop. The existing local supervisor runs it in the background. It requires the PC awake and, on this installation, the Windows user signed in; no claim of operation while powered off.

## Operation

- `python -X utf8 experimental_paper.py init --reference "explicit authorization"`: enroll separate accounts once.
- `python -X utf8 experimental_paper.py once`: bounded live-data paper pass; protected against overlap.
- `python -X utf8 experimental_paper.py report`: read-only current account report.
- Dashboard: `/experiments`; API: `/api/experiments`. Original nine desks remain in their original views.
- Supervisor job: `experimental-paper`, scheduled after each pass with a 60-second interval. Entry deadlines are enforced inside each account; watchers continue after trial end until holdings can be closed using fresh data.

## Validation

Temporary fixture accounts exercise all eight entry/exit lifecycles and cost conservation, idempotency, trial expiry, stale-data handling, distinct rules, original-file preservation and read-only transport. Fixture evidence cannot enter a live-data experiment account. Production fills must identify `source_mode=live`, `simulated=true`, their experimental account UUID and evidence hash. Unit-test fills are excluded from the production dashboard.
