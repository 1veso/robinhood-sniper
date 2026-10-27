# Required automatic execution workflow

The user's 2026-10-27 instruction, "NO manual trading ... only automatic system",
supersedes the article's manual wallet/signing workflow for both strategies.

## Required behavior

The user controls the initial live deployment, trading wallet, capital allocation
and risk limits. Once an independently validated executor is deployed under that
authority, ordinary entries, partial profits, stops, trailing/time exits, receipt
reconciliation and notifications must run automatically. Routine trades must not
wait for a Telegram approval, browser confirmation, pasted fill or human signature.
Telegram reports activity and operational exceptions; it is not an order queue.

This requirement does not activate real-money trading now. A profitable paper
week cannot enable signing or increase capital. The existing paper accounts,
positions, exits, policy history and trial deadlines remain unchanged.

## What is implemented

The Robinhood paper worker automatically makes decisions, records simulated
entries/exits and delivers notifications. Its observed wallet transactions and
route quotes are evidence, not transactions submitted by this account.

Solana paper scanning/watching is scheduled, but strategy entries still require
the unfunded and disabled X gate. `prepare_live.py` prepares unsigned evidence
and reconciles supplied finalized receipts. It cannot sign or broadcast.

Neither chain currently has a complete automatic live executor. The existing
`manual` ledger mode and `--manual-records` commands are legacy bookkeeping/test
utilities. They are not the intended live product and must not be reported as
completion of the automatic execution requirement.

## Remaining implementation and validation

1. A dedicated execution account and restricted signing service. Keep signing
   secrets outside research output, prompts, reports and source control. Bind
   authority to an approved network, wallet, capital budget and risk policy.
2. A deterministic transaction validator. Verify the actual programs/contracts,
   routes, recipients, token amounts, approvals, minimum output, fees and expiry;
   simulate the wallet-specific transaction. Provider descriptions and model
   judgments cannot authorize arbitrary instructions or transfers.
3. A durable order journal. Persist intent, account/policy identity, signed
   transaction identity and reserved exposure before submission. Track prepared,
   submitted, confirmed, failed and unknown outcomes across restarts. Reconcile
   unknown submissions before creating replacements; a timeout is not failure
   evidence and must not cause a duplicate order.
4. Automated receipt reconciliation. Read canonical/finalized receipts and actual
   wallet balance changes, account for fees and failed attempts, and update the
   live ledger exactly once. Submission, quotes and alerts never count as fills.
   Keep live balances and results separate from paper and acceptance accounts.
5. Unattended position management. Continue eligible exits independently of slow
   discovery/X work and entry halts. Bound retries, stale quotes, slippage and
   network spending. Preserve unresolved holdings rather than inventing a sale.
   Recovery alerts may require service intervention, not manual order placement.
6. An end-to-end verification run covering restart during submission, ambiguous
   acknowledgements, rejection/revert, partial execution, expiry, reorganization,
   lost providers and key/signing denial. Dry-run/acceptance results remain
   excluded from market strategy performance. Real-money deployment is a later,
   separate user-controlled step after prerequisites are reviewed.

The first validation stage must remain disconnected from real signing authority.
There is no request to fund a wallet, supply its seed/private key in chat, enable
paid X calls or switch the current paper accounts to live mode.
