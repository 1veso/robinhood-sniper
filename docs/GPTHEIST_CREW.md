# GPTHEIST crew evidence and comparison

This extension applies GPTHEIST's ten responsibilities to the existing Xenon/Argon Solana engine. It adds bounded evidence collection, an independent deterministic review, durable research handoffs and separate baseline/full-crew paper portfolios. The active Xenon book remains authoritative. Radeon, Neon, initialized Solana configuration, original costs, exits, risk limits, account identities and trial deadlines are unchanged.

The project specification is an architecture and validation blueprint. Its $52 to $11,345.95 claim is unverified. This extension does not create a new $52 bankroll, start a 13-hour mission, establish profitability or authorize paid models or live execution.

## Ten responsibilities

| Role | Implemented responsibility | Completion evidence |
| --- | --- | --- |
| Professor | Bind the packet, route failed criteria to their owner and bound research rechecks. | Immutable packet and durable handoff state. |
| Berlin | Preserve the existing strategy judgment and setup context. | Current validated Grok BUY with account/config binding, finite original confidence threshold, request identity, X evidence and known model charge. |
| Tokyo | Attribute discovery and current observations to the exact asset/pool. | Bound source identity and observation, receipt and snapshot timestamps. |
| Rio | Calculate the existing technical window from recorded observations. | Required actual endpoint observations; missing data stays unknown. |
| Denver | Preserve social evidence and its uncertainty. | Recorded required social observations for a complete crew; absent optional social data does not invent a new active strategy threshold. |
| Lisbon | Verify on-chain holder evidence and independently supported buyer-controller evidence. | Complete scoped holder proof and independent controller evidence, not a count of distinct addresses alone. |
| Stockholm | Recompute the original ticket, pool-fee math and bounded exit capacity. | Exact pinned state and a route covering the prospective paper quantity. |
| Nairobi | Package the evidence and decision without changing source facts. | Digest-bound immutable dossier, baseline and producer references. |
| Helsinki | Bind the account and retain execution/model cost attribution. | Existing book/cost policy and explicit unknown bills. |
| Palermo | Veto unsupported, stale, contradictory or out-of-scope conclusions. | Failed criterion, evidence references, responsible owner and recheck eligibility. |

Collectors and calculators are ordinary code. Existing Grok and Jev responsibilities retain their original authorization gates. Ten responsibilities do not mean ten model calls per candidate. Neither packet creation nor deterministic review makes any model call.

## Evidence scope

`runtime/crew/evidence.sqlite3` stores immutable hashed producer records, with a durable source UUID and account/config binding. Each record identifies the mint, pinned pool, producer revision, original observation time, receipt time, source references, normalized factual fields and limitations. A dossier can use only evidence received by its snapshot. Republishing evidence never refreshes its original observation time.

The producer uses the configured Solana RPC plus existing public Rugcheck/DexScreener interfaces. Each capture has a shared ten-second request budget and at most twenty RPC calls. A bounded SQLite wait and filesystem scheduling remain additional costs; these are cooperative limits rather than real-time guarantees. There are no paid model, signing, broadcast or funding calls on this path.

Holder coverage is deliberately narrow. The producer reads at most twenty largest token accounts and then the mint/accounts in one finalized snapshot. It counts distinct owners of positive balances only if those balances reconcile exactly to the same-snapshot minted supply. The reviewer independently checks unique accounts, positive-owner arithmetic, supply totals and timestamps. A larger or incomplete account set remains `UNKNOWN`. This definition includes infrastructure accounts and does not prove independent economic controllers.

A provider's reported total-holder count is retained separately with unknown original observation time. Its local receipt time is not a fresh on-chain holder observation. The required one-hour window uses actual price and holder observations at the exact endpoints, 3,600 seconds apart. There is no nearest-sample substitution, interpolation or invented history.

The supported venue proof is direct classic-SPL Raydium CP-Swap with USDC settlement. It requires canonical finalized state, independent pool/config/vault/mint/program checks and a current account/config-bound review manifest at `runtime/crew/program-review.json`. Its scope is `CREW_READ_ONLY_PROGRAM_REVIEW`; it records the review reference, validity interval and exact program pins. Missing or mismatched review evidence leaves venue and exit evidence unknown. Matching RPC block identity alone never upgrades provider assertions to verified safety.

Exit evidence records the USDC ticket input, calculated acquired quantity, indicative-paper quantity upper bound, covered sell quantity, calculated output and impact. Palermo recomputes the arithmetic and original 3% capacity boundary. The comparison transaction also checks its final modeled buy quantity against the immutable proved sell quantity. This is a state-based, pool-fee-inclusive calculation: it is not a funded-wallet simulation, signed transaction, fill, future liquidity guarantee or proof that network expenses are zero. The original paper cost scenario still applies all approved costs.

When the existing primary Jev path consumes these recorded exit facts, its final Book guard performs the same quantity check against the refreshed entry quote. A lower price cannot silently increase the prospective holding beyond the proved exit size. Both paths recheck the embedded review manifest's scope, account/config binding, exact observed pins and expiration at the actual decision time. A fresh record cannot preserve an expired program review.

No production source currently proves buyer-controller independence. Distinct signers, owners, payers or recipients cannot fill that gap. Required unknown history, independent-controller evidence, unsupported routes or absent program reviews continue to block a complete factual case.

## Review and rechecks

`crew_review.review` produces a deterministic packet bound to the original dossier, baseline, producer digests, revision and review time. Each veto identifies a criterion, its evidence references and the owner responsible for obtaining better evidence. Baseline hard rejections and existing risk limits outrank every role. A caller-supplied `COMPLETE` role flag cannot promote an unknown or vetoed role.

`PASS` means that Palermo found no deterministic factual veto. It does not mean that all ten roles completed, that a model approved an entry or that trading is authorized. `crew_complete` requires every role to be complete. Berlin additionally needs actual current model evidence under the existing model contract. Full-crew comparison admission requires both `PASS` and `crew_complete`, as well as the unchanged baseline admission, account, cost, portfolio, freshness and final-quantity guards. The current disabled paid gates leave the model-dependent path blocked.

Final validation recomputes the packet and checks fresh evidence again. Changing the verdict, roles, dossier, baseline, producer records or revision cannot be hidden by simply recomputing a hash. The digest establishes record consistency, not external truth; producer verification scope remains visible.

Immutable packets and handoffs are additive tables in `runtime/jev-review/assessments.sqlite3`. The recheck policy reuses the existing research interval of 1,800 seconds and at most one attempt per candidate/revision. Claims survive restarts; an interrupted ambiguous attempt is not automatically retried. A recheck needs a later snapshot and cannot edit the original packet. It never resets seen-once admission, replays an old entry, bypasses a veto or changes exits.

## Separate prospective portfolios

Explicit initialization creates `runtime/crew/settings.json`, a coordinator journal and `runtime/crew-comparison`. The immutable comparison manifest binds the current source account, source event stream, original configuration, cost policy and trial. Baseline and full-crew portfolios have separate new account IDs and are seeded from the same consistent source-book snapshot. Copied seed fills are excluded from new-comparison results. The comparison never initializes capital absent from the source or starts its trial clock.

The source check/watch paths capture complete candidate inputs and full market quotes in the existing source observation journal. Capture is opt-in and account/config-bound. The original observation and the capture commit or roll back together; stable keys prevent duplicates after restart. The compact console market event is not used to reconstruct omitted cost inputs. Malformed matching-account settings produce a durable gap while existing exits continue. Other accounts ignore these settings.

The original assessment dossier is immutable. Later evidence cannot make a past Jev decision look prospective. A research-only capture without the original mandatory model admission is explicitly ineligible, retains its actual capture/decision time and cannot create a comparison entry. Capture failures remain gaps or incomplete inputs.

Candidate start, source-check finish and crew decision times are recorded separately. Missing or invalid start times leave total decision latency unknown; a finish timestamp is never substituted for the start.

Both portfolios use the existing Book, cost model and independent watcher, with separate input cursors that commit with portfolio outcomes. Full-crew admission adds the packet veto/completeness requirement. The source account's entry policy remains the baseline. Comparisons do not deliver fills through the source Telegram outbox and their P&L is never combined with source or other-bot P&L.

`report.json` and `report.html` retain separate portfolio accounting, decisions, evidence coverage, costs, processing latency, cursors and gaps. Later outcomes of rejected candidates use only actually recorded same-pool observations. A missing or uncovered later quote is unknown, not an eventual winner, continuous price path or fictional sale. Model bills remain unknown until reconciled; complete net performance is unknown when costs are unknown.

Coverage is conditional on the existing source scanner, seen-once checks, early account/model gates and bounded follow-up. It is not an exhaustive market sample. A completed coordinator run is not proof that every provider succeeded. Trial entries stop at the unchanged source deadline and exits continue. If the source trial has not started, the comparison does not start one. No winner is automatically promoted.

## Operation and verification

The supported CLI is `crew.py init --authorization-reference <reference>`, `crew.py run` and `crew.py status`, with optional `--root`. Initialization is explicit and refuses to overwrite an existing or interrupted comparison. Status is read-only. The coordinator uses existing worker integration; there is no per-trade manual operation or new live authority.

The worker interpreter needs `requirements-crew.txt` for public-key parsing and PDA derivation in the read-only CP verifier. These pinned libraries match the existing executor dependency versions; installing them does not create or provision a signer. Missing dependencies remain a recorded unsupported-evidence condition. The existing Windows worker actions and schedules need no change.

Inspect actual completed coordinator runs, source/tape/portfolio cursors, producer errors, recheck claims, packet completeness and comparison gaps. Keep model authorization, source account/config, costs and original trial unchanged. Paid Grok/X and Jev remain disabled until separately authorized; no setup probe changes that decision.

Offline checks cover provenance contradictions, no lookahead, exact-size arithmetic, stale and changed packets, veto non-bypass, bounded restart-safe rechecks, source capture rollback/deduplication, original exits after capture failures and isolated comparison accounting. Synthetic acceptance remains excluded from every strategy result. Passing those tests establishes implementation behavior, not profitable market performance or live readiness.
