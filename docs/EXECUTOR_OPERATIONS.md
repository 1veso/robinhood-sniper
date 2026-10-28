# Automatic executor operations

Use the installed directory C:\Users\User\Downloads\sniper-v0.1.0 consistently. The source checkout is C:\Dev\SNIPER\sniper-publish. The execution journal is separate from the existing exploratory paper ledger and from isolated acceptance accounts. Never point the executor at the paper ledger or copy its fills into an execution account.

## Commands

Run these commands from the installed directory:

    python executor.py status
    python executor.py status --mode live
    python executor.py acceptance
    python executor.py once --mode shadow
    python executor.py run --mode shadow --interval 10

`status` reads `runtime/executor/status.json`; `status --mode live` reads `live-status.json`. `once` completes one observation cycle; `run` repeats at the specified interval (10 seconds by default, allowed range 5–120). Shadow candidates and source checkpoints persist in `runtime/executor/shadow/opportunities.sqlite3`. `acceptance` creates a new isolated directory under `runtime/executor/acceptance/`, writes its own ledger and `result.json`, and makes no network/signing calls. It refuses to reuse an existing output directory. A key file or profitable paper report cannot change the default shadow mode.

Shadow operation may observe and evaluate saved real-market evidence. It must not read signing secrets, submit transactions, create real holdings or claim real fills. Expired or missing evidence remains unavailable rather than being refreshed by changing a timestamp. Its outcomes do not enter week-trial P&L.

The installed shadow job observes inputs only. A future authorized live deployment has two worker roles: `collect --mode live --allow-live-submission` updates its own event/opportunity journals, and `run --mode live --allow-live-submission` performs receipt recovery, position exits and eligible entry handling. Despite its shared authorization flag, the collector has no signer or broadcast capability. It must run independently of the execution process and research. Reconciliation and eligible exits precede entry handling, although one unresolved signed transaction still serializes the wallet.

Windows scheduling must use absolute installed Python/script paths, one-instance semantics and the runner's separate process locks. This PC must remain awake, connected and signed in. `runtime/executor/live-status.json` and `collector-status.json` are distinct from shadow status. Live initialization is a separate command requiring an explicit authorization flag and reviewed policy; it is never called by shadow operation. Existing live account directories are refused instead of reset.

## Evidence to inspect

Do not infer readiness from an enabled Windows task alone. Check:

- Actual cycle start/finish and status timestamps across separate processes.
- Account, source, policy, wallet and chain identity.
- Reserved, signed, submitted, unresolved, failed and confirmed orders.
- Persisted transaction identity before any submission attempt.
- Canonical receipt evidence and exactly-once costs/holdings updates.
- Fresh marks and eligible exits derived from actual execution holdings.
- Durable notification acknowledgement, separately from order settlement.
- Explicit mode and live_execution_ready=false while deployment prerequisites remain incomplete.

A Telegram acknowledgement means delivery was accepted by Telegram. It does not prove a swap settled, and a Telegram failure must not cause the same order to be submitted again.

## What is needed for a later initial live deployment

1. A separate **public EVM wallet address** for the Robinhood strategy. No private key or seed phrase belongs in chat, research exports or the repository.
2. Explicitly approved capital, maximum exposure, ticket/position limits, daily loss and network-fee limits, slippage, and emergency stop behavior. Current paper settings are evidence for review, not implicit permission to risk real funds.
3. A reviewed Robinhood deployment manifest: chain ID and genesis, supported router address/ABI/code identity, factory, settlement asset and permitted pool policy. Another chain's router address cannot be assumed compatible.
4. A separately approved execution policy bound to wallet, deployment and account. An initial activation record is not a per-trade approval mechanism.
5. Restricted signer provisioning locally after authorization and review, followed by wallet-specific validation with approved funding. Do not provide signing secrets or fund a wallet during the current shadow setup.
6. Complete strategy, receipt/recovery and operational evidence. Solana additionally needs its own reviewed instruction validator and funded/authorized X gate; configuring the EVM executor does not satisfy either requirement.

No Robinhood brokerage or Alpaca API is needed for this on-chain V3 executor. Existing RPC supplies public chain access; its limits and archive capability must still support the selected validation and reconciliation methods. Configuration must not purchase a plan or enable paid inference.

## Recovery rules

An HTTP timeout after submission is ambiguous. Keep original signed bytes, transaction hash, nonce and reservation. Reconcile against the approved chain before considering further activity. A returned hash is not a fill, an absent receipt is not proof of failure, and an increased pending nonce is not permission to invent a replacement trade.

An entry halt should preserve receipt recovery and permitted exits. A full stop must prevent new signing/submission without erasing unresolved orders or holdings. Receipt conflicts, a replaced journal, changed policy, unexpected wallet movements or noncanonical settlement require investigation; none may reset the account or silently clear exposure.

For an initialized live account, `python executor.py halt --kind entries` stops new entry signing/submission while exits and receipt reconciliation remain eligible. `python executor.py halt --kind all` prevents all new signing/submission while receipt reconciliation continues. These commands persist the halt without reading a signing key. Clearing a halt requires a deliberate reviewed recovery action; it is not a per-trade approval process.

Before restoring a failed worker, preserve its database and evidence. Never erase the journal to get a green status. Never reuse an acceptance account as a live account, and never count acceptance fills as market-strategy performance.

The detailed architecture and reviewed primary sources are in [AUTOMATED_EXECUTION.md](AUTOMATED_EXECUTION.md).

Version 0.1.18 adds a required finality-latency deployment review and fresh
entry-only latency check. Set `max_finality_lag_seconds` and
`finality_review_reference` only under an explicitly reviewed execution policy.
The example leaves them unapproved. An October 28 snapshot measured 978 seconds
of finalized lag; serialized settlement can delay exits. See
[live switchover preparation](LIVE_SWITCHOVER.md) for evidence and remaining work.
