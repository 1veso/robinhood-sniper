# Automatic execution preparation — 0.1.19

Both live deployments remain disabled. This release changes execution code and
readiness reporting, not paper accounts, initialized trading configuration,
cash, holdings, exits, trial deadlines or paid-call authorization.

## Solana supported path

`solana_executor.py` runs the separate automatic Solana executor. It reuses the
existing Rugcheck client, pinned Market checks, Judge, shared paid budget,
original exit function, durable execution journal and Telegram outbox.
Discovery is a separate process from exits. Profiles are claimed once and
decisions are recorded separately from orders and finalized fills. Slow or
disabled model calls cannot disable exits on existing holdings.

The initial adapter is deliberately limited to **one direct classic SPL Raydium
CP-Swap pool with USDC settlement**. Jupiter supplies a quote; its transaction
instructions are ignored. The executor decodes the pool, configuration, oracle,
mints, vaults and wallet token accounts, derives their addresses, checks live
program code/upgrade pins, independently calculates output and constructs the
documented exact-input swap locally. An exact second reconstruction rejects
extra instructions, transfers, different privileges, signers, amounts or
recipients. Only bounded compute instructions, an optional idempotent output ATA
creation and that swap are permitted. Target mint/freeze authority must be
revoked on chain before an entry. Token-2022, arbitrary Jupiter CPI routes,
multihop routing, native SOL wrapping, tips and cleanup are unsupported.

Real-account simulation, native fees, rent, source balances, fresh reserves and
blockhash validity are checked before signing. The restricted Ed25519 signer
checks durable authority before unlock and again afterward. Signed bytes and
their signature are committed before submission. Recovery reuses those bytes;
only matching finalized transactions in canonical blocks can update holdings.
Receipt token deltas, account ownership, decimals, actual fees and rent reconcile
exactly. WSOL token transfers are separated from rent movements.

An absent signature is not a failed transaction. After blockhash expiry a
complete bounded finalized-block signature scan is required to release a
reservation. A missing/pruned block or a validity interval beyond 512 slots stays
unresolved. A crash inside the signing boundary or an unexplained receipt still
requires recovery review; no new identity is invented. These conservative
exceptions can delay exits. This is not a guaranteed-stop or HFT service.

Source layout reference: Raydium CP-Swap commit
`f4b9b26342afe9122d5ff67cd7a4f19ded4ac0a5`, including
[swap_base_input](https://github.com/raydium-io/raydium-cp-swap/blob/f4b9b26342afe9122d5ff67cd7a4f19ded4ac0a5/programs/cp-swap/src/instructions/swap_base_input.rs)
and [oracle layout](https://github.com/raydium-io/raydium-cp-swap/blob/f4b9b26342afe9122d5ff67cd7a4f19ded4ac0a5/programs/cp-swap/src/states/oracle.rs).
Observed deployed byte hashes are review inputs, not proof of a verified build
or an independent security audit. The Raydium program is upgradeable; matching
its current identity cannot prevent an upgrade after a check.

## Model gate and spending

Funded inference and X-search proofs now bind the account, initialized settings,
loaded configuration file, model and credential fingerprint. Missing, failed,
changed or older-than-seven-day proofs block new entries. Paid authorization
must name the same binding and an explicit approval reference. Existing disabled
authorization stays disabled. No paid request was required to build this release.

Every live judgment preserves its supplied evidence and response. Admission
revalidates retrieved-X provenance, finite confidence at least 0.70, the original
mint/pool and current risk evidence. Source citations are not proof of wallet
relationships or a guarantee of safety. Live API charges are tagged to their
execution account, excluded from the paper account's bill, and imported into its
own journal. The shared $1/day local budget still applies across requests. It is
not a vendor-enforced cap. Missing actual billing blocks new live entries.

USDC and SOL are valued from current price feeds for USD risk checks; neither
native fees nor a stablecoin are silently treated as zero cost or a fixed dollar.
The ledger retains exact USDC amounts, lamport expenses, locked rent and billed
API dollars separately. Current-FX risk marks are not historical net USD P&L.

## EVM confirmed exit management

The default remains `finalized_only`. A separately reviewed policy can select
`confirmed_exit_management`, retaining `require_finalized=true`, the confirmation
depth, an explicit review reference and bounded unfinalized capacity (1–8).
Verified canonical confirmed transactions become `INCLUDED`. Their provisional
holdings may support exits at a subsequent nonce, while cash, fills and realized
P&L still wait for finality. Reserved capital and fees remain locked. Additional
entries are blocked while exposure is unfinalized; approval prerequisites for
the same order may continue. Finalization proceeds in nonce order.

Disappearing, changed or noncanonical provisional evidence halts new execution
and retains unresolved exposure. The final receipt must match the observed
provisional transaction, block, outcome and flows. Transaction inclusion delay,
confirmation depth, a pending nonce, the capacity bound, unavailable routes and
RPC errors can still postpone exits. No mode is enabled in the current trial.

## Local signer provisioning and deployment

`sniper.signer_vault` implements Windows current-user DPAPI encryption, owner-only
file ACLs, exclusive creation and exact policy/wallet binding for Solana and EVM.
The operator enters a dedicated wallet's private key only into a hidden local
prompt after explicit provisioning authorization. Never send a key/seed in chat.
No real signer is provisioned by this release. Disposable known test keys prove
the encryption and signing paths without real funds or mainnet submissions.
DPAPI does not protect against malicious software already running as the user;
Python cannot promise key-memory zeroization. Independent review remains required.

The paper installer includes the source and dependency manifest, but no private
runtime, keys or pre-enabled execution environment. The optional executor profile
uses pinned `eth-account==0.14.0`, `solders==0.29.0`, `jsonalias==0.1.1`. Install
and validate that profile separately before an authorized live deployment.

Read-only commands from the installed app:

```powershell
python solana_executor.py status
python solana_executor.py probe
```

Later, after an independently reviewed policy and explicit user authorization:

```powershell
python -m sniper.signer_vault --policy REVIEWED_POLICY.json --output LOCAL_SIGNER.dpapi.json --provision-authorized
python solana_executor.py validate-wallet --policy REVIEWED_SOLANA_POLICY.json
python executor.py validate-wallet --policy REVIEWED_EVM_POLICY.json
python solana_executor.py initialize-live --policy REVIEWED_SOLANA_POLICY.json --initial-deployment-authorized
python solana_executor.py run --allow-live-submission --interval 10
python solana_executor.py collect --allow-live-submission
```

The collector is separate and retains the original 30-minute discovery cadence;
the live exit worker's faster polling is an execution policy decision requiring
initial review. No live Windows jobs are installed automatically. Initializer
checks actual setup-pair buy/sell simulations, balances and signer ownership;
it refuses an existing account and does not submit a transaction. Setup probes
do not approve strategy tokens. Use `solana_executor.py halt --kind entries` to
retain exits/reconciliation, or `--kind all` to block all new signing/submission.

Both policy wallet-validation commands retain the explicit deployment-review
gate and do not start a worker. EVM validation distinguishes an exact allowance
prerequisite from a successful swap simulation. An unfunded wallet, absent target
balance or absent exact allowance cannot prove both swap directions. Such setup
transactions would need a separately authorized, bounded automatic setup run;
this release does not submit them or invent state overrides. The read-only
`live_check.py` and Solana `probe` commands remain available without live approval.
The reviewed DPAPI policy/source UUID is preserved during initialization.

## Remaining deployment requirements

- Funded xAI access, new paid-call authorization and successful bound tests.
- An available supported direct pool and funded wallet-specific validation. The
  free CP USDC/WSOL probe returned HTTP 400 `No routes found`; Jupiter confirmed
  the CP program label, so this is not treated as a bad server credential.
- Independent review of the new validator, deployed program identity and EVM
  provisional exit behavior; offline tests do not substitute for this review.
- Explicit capital, native-fee/rent reserves, policy and signer provisioning
  approval, then user-controlled initial deployment. No automatic promotion.
- Completed evidence review for each strategy. The separate Solana trial has
  not started while its paid X gate is disabled.

Solana's chosen draft settlement is USDC. Robinhood's chosen draft settlement is
WETH: the October 28 local 24-hour sample had 21 distinct validated entry-route
pools, all WETH, and 4,525 WETH versus 476 USDG funded acquisitions. This sampled
coverage is not proof of future liquidity. WETH carries idle-capital price risk;
USDG would need another conversion route to use those same pools. Native ETH gas
and WETH settlement remain separate. Neither capital amount is approved yet.
