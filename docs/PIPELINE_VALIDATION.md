# Collection, qualification, evidence, and execution validation

Version 0.1.7 adds resumable derived wallet-history checkpoints. Each completed
transaction is committed atomically to SQLite under its wallet, frozen snapshot
and reconciliation version. Restarts reuse completed reconciliation rather than
replaying thousands of receipts. Index pages must still be complete, nonce and
boundary balances must reconcile, and missing costs or unsupported routes still
reject qualification. A corroborated unsupported transaction can end an eligibility assessment early,
with no partial P&L estimate and no claim of complete history. Raw evidence and
checkpoints remain available for a later full assessment. Work rotates after three batches so one large history
cannot monopolize the queue. Canonical funded recipients from the paper source
join the candidate queue; this grants no performance or identity qualification.

The main collector and the exploratory sampler have different coverage. The main
collector's cursor lag is in `runtime/status.json`; the exploratory report records
its bounded sample and skipped intervals explicitly. The exploratory log scan covers up to
3,000 blocks per cycle, in 500-block requests. This covers the usual roughly
1,200-block advance between two-minute runs; longer downtime remains a disclosed gap. A caught-up cursor is not a
claim that every exchange and every wallet on the chain is covered.

## Supplemental safety evidence

The existing GoPlus response and size-bounded V3 buy/sell quotes remain required
by the two-wallet paper profile. Enable supplemental Crius observations with
`runtime/supplemental-evidence.json` containing `{"crius":true}`. The public
security endpoint requires no key. A free key saved only in
`runtime/crius-api-key.txt` also enables the detailed liquidity endpoint.
No paid fresh simulation or subscription is requested.

Provider token, chain, block timestamp and canonical block identity are checked
against RPC. Known current honeypot, tax and changeable control flags can reject
new entries. Historical tax claims remain warnings requiring transaction-level
corroboration. Selected-pool LP owners and locks, when available, are retained
as provider evidence. A matching canonical block does **not** independently
prove provider classifications, locker semantics, beneficial ownership, complete
deployer history, or future sellability. Missing LP/deployer proof stays unknown;
the strict reference strategy stays blocked until those requirements are met.

Sources: [Crius security and liquidity schema](https://crius.sh/docs/reference/security/),
[GoPlus field semantics](https://docs.gopluslabs.io/reference/response-details),
[Uniswap quote scope](https://developers.uniswap.org/docs/sdks/v3/guides/swapping/quoting).

## Isolated end-to-end acceptance account

```powershell
python acceptance.py --runtime runtime --token TOKEN_ADDRESS --pool POOL_ADDRESS --symbol TOKEN_SYMBOL
```

This explicitly requested execution test uses a separate account under
`runtime/acceptance/`. Its trigger is an operator test, not invented wallet
confirmations. Entry risk limits, current safety evidence and live route checks
remain active. A one-second time exit exercises the regular engine close path
using a second live quote. It does not change any week-trial position or rule.
Provider errors preserve the holding for retry. Restarting after completion
does not create another trade. Missing or replaced account/outbox identities
cannot silently reset the test.

Telegram messages say `EXECUTION TEST — PAPER`, and the report explicitly excludes
the account from strategy performance. `COMPLETE` requires an opening fill, one
fully closed position, zero open holdings, and both Telegram message deliveries
acknowledged. Rerun to deliver a queued close alert if necessary; the durable
outbox retains duplicate and ambiguous-send protections.

An acceptance round trip demonstrates software plumbing. It does not replace a
natural strategy-triggered exit, a week of observations, wallet qualification,
or proof of profitability. No component signs or submits a real transaction.
