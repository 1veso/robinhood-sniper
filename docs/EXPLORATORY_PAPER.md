# Radeon exploratory paper strategy and recorded revisions

Radeon is the display name for the existing Robinhood exploratory runner. Its
active profile comes from `runtime/active-exploration.json`, the trial manifest
and the ledger-pinned policy together, never from a TOML file's mere presence.

| Profile | Entry signal | Count limits and re-entry |
| --- | --- | --- |
| Strict reference (`config/paper.toml`) | Five qualified independent groups, $50,000 liquidity, complete required safety | Four positions, six entries/day, 24-hour cooldown |
| Exploratory v2 (`two-wallet-30k`) | Two observed buyers, $30,000 liquidity, current GoPlus required | Four positions, six entries/day, 24-hour cooldown |
| Exploratory v3 (`two-wallet-30k-reentry`) | Same two-buyer/$30,000 requirements | No position or daily-entry count cap; fresh post-close buyers required |

The authorized account on this PC runs v3 under policy `6cb80503c7e835b0`.
See the [recorded re-entry revision](ROBINHOOD_PAPER_REENTRY.md). Distinct observed
recipients do not establish wallet profitability, funding ownership or independent
control. The strict account remains a separate research reference. Historical
legacy exploratory rules and fills remain in their original policy cohorts.

Run `python explore.py` (Python 3.11+) to resume the selected authorized profile.
The `--activate-v2` and `--activate-reentry` commands are explicit migrations, not
routine restart commands. Never activate another profile to repair a deployment
mismatch. Activation preserves account identity, cash, holdings and the original
deadline while appending its authorized policy revision. The existing Windows
task runs every two minutes; process locks and IgnoreNew prevent overlap. Keep
the PC awake and connected. Installed same-user restart recovery does not reset
the trial. The runner supports paper execution only and has no signing or order
submission path. The [entry-size correction and isolated A/B comparison](PAPER_POLICY_COMPARISON.md)
retain the active account's exits and keep experiment results separate.

Files live under `runtime/exploration/`: `trial.json`, `ledger.sqlite3`, `events.sqlite3`, `notifications.sqlite3`, `status.json`, `report.html`, and `trades.csv`. The active selector is `runtime/active-exploration.json`. Back up the directory and selector together. Missing or changed identity is an error, not permission to create fresh capital or resend messages. Earlier fills remain attributed to their recorded entry policy; they do not establish performance for the revised rules.

## Preserved monetary, signal and exit rules

- Keep the original $1,000 simulated capital baseline and all accumulated results; restarting never credits it again. Preserve the 0.5% full-loss budget per position, 2% aggregate risk, $25 ticket ceiling, 2% daily equity-loss halt, fees, freshness and original exits. V3 removes count caps and the blanket cooldown; monetary limits still bind. Actual tickets are usually below $5 because risk includes costs. Entry needs two distinct recipients in different transactions buying the same token/pool within 900 seconds (15 minutes), each spending at least $10. After a full close, both purchases must be strictly later than that close. Distinct addresses do not prove independent control.
- Sample up to 32 transactions by deterministic hash across at most 3,000 newly scanned confirmed blocks, in 500-block requests, retaining all legs of selected transactions. Bounded follow-up selects at most eight pools and sixteen swap legs per pool in a scanned interval. Newly proved pools can revisit that same recorded interval; restart-safe follow-up does not extend freshness, move the main cursor backwards or bypass canonical receipt verification. Established registered Uniswap V3 pools are included, not just launches. Skipped blocks, incomplete follow-up and expired selected events remain explicit coverage limits. This is not a complete market census.
- Persist confirmations in the canonical source journal. Deduplicate addresses and transactions, expire old buys and exclude reorganized events. Recheck the counted block hashes before entry. Restarting cannot forget the first buyer, count it twice or reuse a reverted acquisition.
- Verify canonical receipt, successful status, registered factory pool, supported WETH/USDG base, matching swap and token transfers. Prefer direct wallet payment proof. For routed buys, prove the pool received the stated base amount and the recipient acquired the tokens net of outgoing transfers. Preserve payer, recipient and transaction sender separately: recipient control of the funds is unestablished. The $10 minimum uses the observed pool input, valued at the current base/USD observation, rather than claiming a personal-wallet expense or historical cost basis. Reject ambiguous repeated same-pool swaps and missing payments.
- Wallet profitability, controller independence, and unknown safety checks remain explicitly unestablished. Known failed safety checks reject entry and remain remembered through provider outages. An exactly factory-verified pool can be excluded from holder concentration; other contracts cannot. Bound missing top-holder coverage conservatively. The complete provider report remains in the decision record.
- A current GoPlus token-security response is mandatory for new entries. Transient provider outages block entry and retain the original selected event for retry while fresh; they never establish a safety failure or permanently blacklist a token. Check honeypots, buy/sell restrictions, taxes, minting, ownership and proxy controls, concentration, creator balance, known scam flags, counterfeit tokens, gas abuse and modifiable trading limits. Fixed transaction/holding limits are warnings, not proof of a rug; size-bounded route checks still apply. The report lists failed and unknown checks. Missing V3 liquidity-withdrawal control or full deployer-history evidence remains unknown, never a rug-free certificate. GoPlus's separate rug-pull detection beta does not document Robinhood support and is not used as a substitute. The existing Rugcheck API adapter serves Solana research only.
- Reconcile a provider's missing DEX listing only when a canonical funded swap proves the registered pool exists. This does not establish taxes or general sellability; those remain unknown unless supported separately. Honeypot, nonzero tax and permission failures remain failures. Check the provider-named creator's current on-chain balance and total supply even when its holder list is absent; this is not a controller-identity review. See [GoPlus field definitions](https://docs.gopluslabs.io/reference/response-details) for the provider's coverage limits.
- Obtain fresh pinned V3 buy and sell quotes with size coverage. When the market index lacks the exact pool, value its same-block ERC20 balances using the on-chain spot ratio and current base/USD price. Total pool value includes inactive V3 liquidity; executable depth must still pass the size-bounded quoter. Charge quoted pool costs, 100 bps adverse execution stress, a further depth proxy and modeled gas. No executable-route evidence means no fill. Quoter calls are independent, not a real round trip; this does not prove wallet approvals, future liquidity, absence of MEV, or an ability to sell later.
- Monitor holdings before and after entry work. Partial profit, trailing exit, stop loss and time exit use the same accounting engine as the strict strategy. Unavailable quotes keep holdings open with unresolved valuations. Reorganizations halt new entries for review while exit monitoring continues.
- Stop new entries after seven days. Continue managing any remaining holdings and report CLOSING until closed. Record monitoring gaps over three minutes; calendar duration is not proof of continuous coverage. Restarting with `--start` cannot reset the deadline.

## Bounded evaluation and coverage audit

Selected fresh events are evaluated oldest first before another collection pass.
Entry work shares an absolute deadline 80 seconds after cycle start, including
the initial position watch. Provider timeouts and response reads use the remaining
allowance. The final position watch has up to 25 seconds for quotes, leaving a
15-second publication allowance within the existing two-minute cadence. These
are cooperative work budgets, not a hard real-time guarantee: OS scheduling,
DNS, local database work and notification delivery can still delay completion.
Optional comparison quotes stop by cycle start +110 seconds; deferred marks are
recorded as gaps in those separate portfolios. No scanner cadence or trading
threshold changes.

Funded-buy proof is retained before cheap account checks. Already-open tokens and
certain account blockers skip entry safety/route requests; existing position
management still runs. Conservative accounting never infers spare capacity from
stale marks. Final fee-inclusive sizing and the transactional risk checks remain
authoritative. Entry routes cover the affordable input, bounded by the existing
ticket, cash and risk budgets. Excessive impact is `route_price_impact_exceeded`;
actual lack of affordable capacity remains `risk_or_cash_budget_exhausted`.
Position-sized sell-only exit quotes are unchanged.

Budget interruptions and transient provider failures leave original timestamps
and source events pending. Each cycle records up to 500 expired pending events as
`UNEVALUATED / signal_expired`, with their original observation time. Remaining
expired backlog is disclosed; neither expiry nor a skipped check becomes a
safety failure. A decision already committed before interrupted source
publication is reconciled from the ledger, never relabeled unevaluated.
Historical backlog summaries have a two-second read budget; if exceeded, their
totals are explicitly unavailable. They cannot consume an unbounded share of
exit/publication time. Complete historical counts remain inspectable in SQLite.
`status.json` includes entry-work counts, stage durations,
pending/expired counts and primary completion time. The ledger's
`exploratory_cycle_completed` audit is written after successful primary report
publication. Task state alone is not completion evidence.

Collection revision `same-interval-pool-followup-v1` is recorded separately from
the unchanged entry policy. New scan intervals save their source identity,
canonical anchor and bounds atomically with the main cursor. A pool with a newly
verified buyer can recover bounded swap legs from that same interval. Follow-up
resumes per 500-block chunk and rechecks canonical blocks before admission, then
uses the usual receipt proof and quorum checks. Eight pool slots include pools
already watched by the initial scan; each pool selects at most sixteen legs.
Legacy scans without saved interval metadata are not reconstructed. Compare
results before/after this collection revision separately from entry-policy cohorts.

Some observation RPC nodes prune historical state before a selected event's
900-second signal lifetime ends. For the specific historical-state-unavailable
response, funded-buy proof can use the already configured quote RPC for the same
historical `eth_call` operations. Both providers must agree on chain, genesis and
the event's canonical block hash. Original block tags and timestamps are retained;
current state is never substituted. Receipts, transactions and collection logs
still use the observation provider. The fallback shares the existing proof/entry
deadline and does not send broad collector queries to the quote provider. Other
RPC errors retain their normal failure handling, and missing proof still blocks
entry. The proof records when this fallback supplied historical state.

## Reports and Telegram

The HTML report identifies EXPLORATORY PAPER and includes fills, costs, unknown evidence, trial dates, gaps and provider errors. The CSV exports ledgered fills. Every decision retains its source receipt, pool, price and route evidence. These results never count as strict-strategy results.

If the previously paired Telegram bot and recipient files exist, actual paper fills generate clearly labeled informational messages. Delivery uses its own persistent outbox, verifies the paired bot, and sends at most one message per cycle. Ambiguous deliveries are not blindly retried. No private key is needed and no paid xAI call is enabled.

After the week, review net results after costs, unresolved holdings, sampling and downtime, concentration, fill assumptions and losses. A week with few or no eligible fills is evidence about this configuration, not a reason to manufacture trades or relax rules silently. Real-money trading requires a separate decision and implementation review.
