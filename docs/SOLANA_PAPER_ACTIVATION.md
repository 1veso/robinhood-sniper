# Local Solana paper activation — November 1, 2026

This installation is PAPER ONLY. No real-money order, wallet connection, signer,
or funding transfer is enabled. Public wallet addresses were retained as requested.

## Accounts and independence

The expanded D:\SNIPER RUNTIME backup supplied the original Xenon, Neon, Krypton
and Helium accounts. Their original identities, ledgers and histories are restored.
Temporary replacement accounts remain archived under
runtime/local-setup/solana-rollout-20261101/temporary-accounts-before-restore.
SQLite checks passed; synthetic acceptance accounts were not imported as trading
performance. The full build cache contains installer stages and test fixtures;
these are not complete trader histories or independently reviewed controller data.

| Slot | Desk | Account/data ownership | Current observation |
| --- | --- | --- | --- |
| 2 | Xenon | Original rug_desk account restored | Live profiles/market reads and its own Grok/X gate enabled; Argon factual enrichment still gates prospective entries. |
| 3 | Neon | Original neon_bot account restored | Own live discovery/RPC and model gate enabled; complete cost-accounted trader histories and eligible leader selection remain missing. |
| 4 | Argon | Existing design filters Xenon's shared paper account | Original journal and ACTIVE_PAPER state restored, original comparison source relocated with identity checks; worker healthy, incomplete dossiers remain ineligible. |
| 5 | Krypton | Original independent account restored | PAPER_READY; successful live RPC, Rugcheck, market and own Grok checks. No qualifying fill observed during verification. |
| 6 | Helium | Original independent account restored | PAPER_READY; successful live reads and its own Grok/JEV gates. No qualifying fill observed during verification. |
| 7 | Nitrogen | Own new account retained | Four public research wallets configured; finalized RPC scans running. No complete factual dossier yet. Paper orders still require its explicit approval step. |
| 8 | Radon | Own new account retained | Own read-only collector active; own Grok/X gate enabled. Raw archive is not yet a reconciled trading feed. |
| 9 | Oxygen | Original isolated dev_watch article runtime | PAPER=True; original rules and approved exit-confirmation patch verified. Existing four Codex routines retained. |

Radeon/slot 1 is outside this Solana rollout. Argon is the one intentional shared
account design already present in the repository. Other desks do not share
positions, orders, fills, strategy state or account-bound model proofs. Pure
parser/transport helpers, API credentials, read-only RPC access and installation
spending caps are shared infrastructure. Krypton, Helium and Radon no longer
require Xenon's model authorization/proofs. Nitrogen's JEV requests no longer
consume Grok's budget; historical reservations remain preserved.

## Approved spending and provider setup

runtime/model-budgets.json sets the approved shared Grok limit to **$5/day**.
The original strategy configurations were not rewritten to change spending.
All shared Grok callers use the override and the existing durable budget ledger.
Each request reserves at least $0.50; a smaller billed charge does not release the
unused reservation that day. Failed/unresolved calls are not treated as free.
Oxygen retains its separate article cost behavior, as specified in the rollout.

The latest explicit user instruction removes the JEV hard daily ceiling.
runtime/jev-spending.json authorizes uncapped daily JEV reservations for Argon,
Nitrogen and Helium. Their previous numeric settings remain fallback limits when
this override is absent. Per-request bounds, account authorization, duplicate
protection, usage estimates and invoice reconciliation remain enforced. Earlier setup-probe reservations
were carried into these ledgers rather than erased or mislabeled as invoices.
Grok retains its $5/day local reservation limit; it is not a vendor-enforced cap.

Xenon, Neon, Krypton, Helium and Radon now have successful current funded proofs
bound to their own restored/current accounts. Structured X probes validate
inference and X retrieval using the same real response only within that account.
Contract probes create no trading candidate, order or fill. Model proofs expire
after seven days under the existing rules; they are not renewed implicitly.

Existing TypeSafe access is retained for Argon, Helium and Nitrogen. API keys are
kept in ignored, user-restricted runtime files and are never included in source,
browser responses or reports. JEV token estimates remain estimates, not invoices.

The existing Alchemy credential now supplies the shared Solana mainnet RPC.
Genesis, finalized slot and largest-token-account reads succeeded. The RPC URL
is stored in ignored runtime/solana-rpc-url.txt. Public-node 403/429 errors remain
in history; fresh Krypton/Helium checks succeeded. Oxygen's local article RPC
configuration is unchanged.

## New local setup and collection work

Nitrogen's configure-wallets command permits only initial configuration of an
empty watchlist before any lead, model or trading activity. It preserves account
identity, cash and all other rules, and records explicit source/authorization
references. The four current addresses are independently rechecked public research
candidates, not qualified leaders or verified independent controllers.
Reading transaction version 1 now advances past unsupported receipts; the swap
decoder still admits only supported legacy/v0 direct classic-SPL Raydium CP swaps.

Radon's collect command has its own account-bound authorization, database,
head/backfill cursors and scheduler. It performs at most eight read requests per
run, records gaps and unsupported receipts, and cannot write trading events,
shortlists, orders or fills. Its first live run archived 200 signatures from a
10-candidate research cohort. See radon-export.md for the exact remaining work:
opening-inventory reconciliation, complete token-account/route coverage,
historical quote/cost reconstruction and validated export production. Raw archive
collection alone will not satisfy those gates, even after running longer.

The Argon comparison now supports an authorized source-location.json relocation.
It verifies the original account, configuration and observation-stream identities;
the original manifest and historical observation tape are not rewritten.

## Workers and verification

Eighteen hidden Windows tasks now cover the Solana desks, including Radon's
separate collector. All use desktop.py worker with --no-send and Python UTF-8.
They run while this Windows user is signed in. Existing tasks outside the rollout
are untouched; Oxygen retains its four Codex routines without duplicate workers.
The console is http://127.0.0.1:8765/.

Checks cover shared budget persistence, per-account authorization, restoration
relocation, Nitrogen configuration/receipt handling, collector isolation/cursors,
paper accounting, desktop task behavior and Oxygen article fidelity. Argon's
current code-bound acceptance was regenerated using 41 paper tests and 21 offline
Solana fixture tests; synthetic results are excluded from strategy performance.

Workers running, paid access working and fresh chain reads do not establish entry
eligibility. Remaining factual producers and history reconciliation are tracked
implementation/data gaps, not instructions to fabricate missing numbers or reduce
strategy thresholds. None of this enables automatic promotion to real trading.

## Explicit seven-day windows started

At the user's November 1 request, Xenon and Neon began operational paper
observation windows at 2026-11-01 14:47:09 Europe/Berlin, ending exactly
2026-11-08 14:47:09 Europe/Berlin. Entry blockers were recorded at startup, not
waived. The console shows ACTIVE trial clocks alongside ENTRY_BLOCKED when
required factual evidence or qualified trader histories are absent. No fills
were manufactured. Repeating the start operation does not reset the deadline;
new entries stop at the deadline and exits remain active.

Xenon's stale X/readiness display came from a report refreshed only by the daily
report job. Every completed scan now refreshes the local report without sending
messages or making extra paid calls. The console also includes the separate
Argon factual gate rather than presenting X authorization as entry readiness.
Neon's isolated full replay passed all eleven checks, including rejected entries,
quote failure, one committed entry, duplicate prevention, recovery and exits.
That synthetic account remains excluded from live-data paper performance.
Oxygen completed one verified live-data paper scan with no BUY output.

## Holder collector implementation update

The Xenon/Argon collector now enumerates classic SPL and Token-2022 token accounts
(up to 4,096 records), counts positive-balance owners, and reconciles raw balances
to unchanged mint supply. It retains a finalized slot/block timestamp and rejects
partial enumeration, duplicates, stale state and changing mint metadata. This
replaces the previous largest-20-only holder coverage restriction. Token-2022
extensions are not silently excluded by a fixed account-size filter. Holder owner
counts still do not prove independent controllers or sellability.

The first live validation attempt against the existing Alchemy endpoint received
HTTP 429. It did not produce verified holder evidence or an entry. PumpSwap route
coverage, matched historical endpoints and buyer-controller evidence remain
outstanding; this component alone does not unblock Xenon/Argon fills.

## November 1 collector extension

See [SOLANA_EVIDENCE_COLLECTORS.md](SOLANA_EVIDENCE_COLLECTORS.md) for finalized
holder pagination, direct PumpSwap/WSOL/USDC quotes, bounded signed buyer
receipts, live validation, and the remaining provider/controller evidence gates.
These additions preserve paper-only operation and do not claim new strategy fills.
