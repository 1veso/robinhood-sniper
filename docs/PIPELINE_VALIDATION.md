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

## Market and paper-trade Telegram alerts

The two-minute exploratory worker sends actual ledgered OPEN, PARTIAL and CLOSE
events through `runtime/exploration/notifications.sqlite3`. Exit notices include
the realized P&L of that fill after modeled costs. Each fill is acknowledged and
deduplicated separately; a missing bound journal is an error, never a reset.

Version 0.1.10 adds MARKET WATCH notices to the same paired Telegram destination
through a separate `runtime/exploration/market-notifications.sqlite3` outbox.
These are dated observations, not entry approvals. They require two observed
buying recipients in different transactions within 900 seconds, a matching
saved pool proof, at least $30,000 observed liquidity, a current GoPlus response,
no known failed safety checks and an available executable quote. Unknown safety
fields, unverified wallet performance/controller independence, the bounded
sample and the actual paper decision are disclosed. Entry limits and all other
strategy checks still decide whether a trade happens.

Market notices expire after 15 minutes, are limited to one per pool in six hours
and three delivered/uncertain messages per hour. Reorganization halts and newly
recorded safety failures suppress queued signals. Fill alerts have an independent
queue and are processed first. Neither queue retries ambiguous delivery without
review. Telegram acknowledgement proves server acceptance, not that the user
read the message ([Bot API](https://core.telegram.org/bots/api#sendmessage)).

This notification change does not alter strategy settings, start a new account,
reset the week, add synthetic fills, or unlock the separate paid Solana X gate.

Position quotes may use `runtime/quote-rpc-url.txt` (added in 0.1.11), while log
collection retains `runtime/observation-rpc-url.txt`. Before selecting a second
provider, verify chain ID, genesis and a recent shared canonical block, then
test size-bounded routes for existing holdings. Keep both URL files private.
Without a separate quote file, the original single-provider behavior remains.
This allows a fast existing RPC app to serve quotes without sending broad log
queries that its plan rejects. It does not relax quote freshness or safety.
