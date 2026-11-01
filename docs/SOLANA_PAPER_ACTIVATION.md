# Local Solana paper activation — November 1, 2026

The user supplied the JEV credential and authorized the Solana paper rollout.
The credential is in ignored runtime/typesafe-api-key.txt with access restricted
to the current Windows user. It is shared by Argon, Helium and Nitrogen; it is
not included in source, browser responses or account evidence.

## Enabled locally

Seventeen Windows tasks cover Xenon, Neon, Argon, Krypton, Helium, Nitrogen and
Radon. All use desktop.py worker with --no-send. They run while the Windows user
is signed in, at the existing cadences, including independent exit monitoring.
Windows denied S4U/boot task creation, so these tasks use interactive-token login
triggers without administrator-only boot triggers. No duplicate Oxygen scheduler
was installed. Oxygen retains its four existing Codex routines and original rules.

The installer supports -SolanaOnly -NoSend -InteractiveOnly. It checks task
ownership before mutation and refuses to reuse an existing notifying task for a
no-send installation. Existing jobs outside this scope are untouched. Worker
markers record notifications_disabled; source journals remain authoritative for
provider failures and entry decisions.

Account-bound paid authorization is installed for the Solana desks. Required
funded provider proofs, current market evidence and strategy checks remain gates.
Argon was explicitly promoted to ACTIVE_PAPER against the replacement Xenon
account after the repository's isolated acceptance suite passed. The old crew
comparison still belongs to the absent original Xenon UUID; its coordinator is
disabled and its history/identity are preserved. It was not rebound.

## Provider verification and fixes

TypeSafe authentication and the pinned jev-1.14.0 endpoint responded successfully.
Helium's real two-question response passed validation. A Nitrogen response reported
score 3.99 with rounded probabilities implying 4.00. The adapter now accepts only
a discrepancy of at most 0.01 when every wire value has at most two decimal places.
Both desks use the higher of the reported and probability-weighted exit scores
for the unchanged cutoff. Larger mismatches, unexplained extra precision and
invalid distributions are rejected. Raw values stay in the journal. The saved
Nitrogen response was revalidated offline and still yields PASS/no entry.

See TypeSafe's [Score contract](https://docs.typesafe.ai/primitives/score).
The limited rounding accommodation comes from the captured provider response;
it does not claim the provider guarantees a particular rounding implementation.

Xenon's funded X Search completed, but the old validator treated username/status
and i/status URLs for the same X post as different citations. Matching now uses
the exact post ID on allowed HTTPS X/Twitter hosts and standard ports. Returned
text uses the provider-annotated URL, without claiming the model's username is
verified. Unknown post IDs, deceptive hosts and nonstandard ports are rejected.
See xAI's [citation contract](https://docs.x.ai/developers/tools/citations).

The original saved response was revalidated without another paid call. It proves
structured inference and completed X retrieval for the same bound account, so
both proof files reference that one response and its original request ID/time.
The earlier literal-OK inference probe failed and remains archived as a failure.
It was not rewritten as a successful response.

## Current limits and incomplete entry readiness

Shared Grok reservation ceiling remains $1/day pending the user's budget choice.
Each request reserves at least $0.50 even if billed less. Two setup requests have
consumed today's reservation ceiling. Their recorded xAI bill totals $0.054784.
The other accounts still need their own funded Grok validation. Proofs are valid
for seven days; having a key or authorization does not bypass their expiration.

JEV ceilings are $0.10/day shared by Argon/Nitrogen and $0.10/day for Helium.
JEV usage is an estimate, not an invoice; conservative reservations are retained.
These local limits are not vendor-enforced caps. Oxygen's separate article cost
behavior is unchanged.

Krypton and Helium recorded HTTP 429 rate limits from the public Solana RPC.
Their existing retry backoff is preserved. Successful model access does not
resolve chain-data availability; the dashboard continues to show these errors.

| Desk | Remaining entry dependencies |
| --- | --- |
| Xenon / Argon | Complete fresh factual enrichment, including reviewed controller evidence and size-specific exit evidence; trial preflight and setup-delivery acknowledgement. |
| Neon | Own Grok/X proofs, complete trader histories, qualified current leader selection, operational proof and setup-delivery acknowledgement. |
| Krypton | Own funded Grok proof and qualifying current on-chain/pool facts. |
| Helium | Own funded Grok proof and qualifying facts; JEV review access is enabled. |
| Nitrogen | Tracked wallets and complete account-bound factual producer; staged paper orders still require approval. |
| Radon | Event/history exports, qualified copier shortlist and its own Grok/X proofs. |

Workers running does not mean entries are eligible. Missing evidence is never
fabricated, old approvals are not reused for new identities, and failed entry
gates do not disable exit monitoring. No real-money execution is enabled.

Private diagnostic records and preserved settings are under
runtime/local-setup/jev-activation. The console is http://127.0.0.1:8765/.

Validation passed: 44 desktop/Grok/model-gate tests, 95 JEV/Nitrogen tests,
the Argon promotion harness (41 paper tests and 21 offline Solana fixture tests),
and Oxygen's original-source verifier. These checks are not proof of live fills.
