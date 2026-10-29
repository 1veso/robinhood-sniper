# Independent Radeon paper entry paths

`parallel-whale-v1` is an explicit entry-only revision of the existing
`two-wallet-30k-reentry` account. It is not selected merely by installing this
code. Activation requires explicit Strategy A wallet-eligibility and re-entry
choices. There was no separately configured one-whale trading policy: the older
reviewed-wallet monitor sent informational alerts, and the strict reference
account required five reviewed controller groups. Neither is silently used as
approval for a new trading rule.

The two signals are evaluated independently:

| Path | Trigger | Re-entry |
| --- | --- | --- |
| A `whale_follow` | One verified eligible purchase. Eligibility is explicitly pinned to `reviewed_tracked` or `observed_recipient`. | Explicitly pinned to one fresh purchase strictly after full close, or A's 24-hour cooldown. |
| B `two_wallet_v3` | Two distinct buying recipients in distinct canonical transactions on chain 4663, buying the same token contract within 900 seconds. Purchases may use different verified V3 pools. | Two fresh purchases strictly after full close; no blanket cooldown or count cap. |

Both retain the existing $10 minimum observed purchase. Reviewed A requires
current cost-accounted performance research and a current structured controller
review; its receipt must independently verify the tracked wallet's direct base
payment and token receipt. Empty, unavailable or stale research blocks reviewed
A only. Observed-recipient eligibility does not establish profitable history,
wallet/controller independence or whale status. It is never described as a
qualification promotion.

An entry requires the shared safety/risk checks **and** either qualifying signal.
Current GoPlus coverage, known safety failures, $30,000 execution-pool liquidity,
fresh position-sized routes, available cash, the existing 0.5% position and 2%
aggregate full-loss risk ceilings, daily-loss halt and original costs remain.
The candidate's triggering pool is selected separately for execution. Each
purchase retains its own pool, transaction, wallet, timestamp, receipt, factory
and canonical evidence. Pool liquidity and quote capacity never transfer from
one pool to another. Same-wallet buys in two pools count once; different token
contracts and different chains cannot combine.

The journal returns all recent canonical proofs before either strategy applies
its eligibility and re-entry filters. Distinct wallet/transaction matching runs
after those filters, so a newer ineligible purchase cannot hide an eligible one.
Each path selects a minimal sufficient witness (one for A, two for B), bounding
the union to at most three purchases for RPC rechecks. Additional original
observations remain in the journal and are not claimed as freshly checked buyers.
The worker rechecks counted block hashes, and the final account transaction
checks the unchanged source UUID, exact proof bytes in the canonical journal,
fresh process-local RPC-check receipts, pinned entry revision, current registry,
timing and shared gates. Restarted workers must obtain new canonical checks;
metadata asserting a recent check is insufficient.

Both signals are appended as `entry_strategy_signal` events. A stable signal key
links subsequent execution states without overwriting earlier evidence. A
qualifying signal can be blocked by safety, route, risk or an existing holding.
In particular, later B confirmation while A already holds the token is displayed
as **qualified; token already held**. Trigger qualification does not mean the
shared entry gates passed. Both signals are retained when they qualify together;
A is the deterministic initiating strategy and the duplicate-position rule still
allows only one position in the token.

Position and fill records retain the initiating strategy, and decision records
retain both qualifying strategies and the entry revision. These are shared-account
attributions, not independent strategy returns. The existing baseline/A/B exit
comparisons retain their original policies: only a provably compatible same-pool
two-buyer subset can enter them. Other new inputs become explicit comparison gaps;
existing comparison holdings keep their original exits. No comparison is promoted.

## Explicit activation

After the user's Strategy A choices are recorded, activate with the normal
controller and worker locks through `explore.py`, for example:

```powershell
python explore.py --runtime <existing-runtime> --activate-parallel `
  --whale-eligibility reviewed_tracked --whale-reentry fresh_post_close
```

The choices in this example are not authorization. Both arguments are mandatory;
there is no activation default. Supported alternatives are `observed_recipient`
and `cooldown_24h`. The normal scheduled worker action needs no new flags.

Activation appends a `policy_revision` with previous/new trial manifests and the
separately hashed entry rules. The monetary RiskPolicy hash remains unchanged.
The same account, source UUID, cash, holdings, original exits, deadline and prior
history remain. JSON publication can be recovered by repeating the identical
explicit activation after a committed journal transaction. Conflicting revisions,
accounts or selectors fail closed. Missing accounts cannot be initialized by this
revision. Existing monitoring gaps remain evidence gaps.

This change does not broaden the disabled live executor's admission policy,
authorize a signer/broadcast, use paid models, start another trial or modify any
Solana account. Synthetic tests remain isolated from strategy performance.
