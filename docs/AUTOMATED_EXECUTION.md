# Automatic execution architecture

The user requires automatic trading: after an explicitly authorized initial live deployment, routine entries, partial profits, stops, trailing/time exits, signing, submission, receipt reconciliation and notifications must run without per-trade human approval. Telegram reports events and operational exceptions; it is not an approval queue. Legacy manual-record tools are bookkeeping utilities, not the intended workflow.

Building this executor does not activate real-money trading. The current paper accounts, cash, positions, thresholds, exits, policy history and trial deadlines remain unchanged. A profitable paper week cannot authorize a key, increase capital or switch execution modes.

## Why a deterministic executor, rather than Strands

The system already makes its trading decisions with Python rules and persists its accounts in SQLite. Order handling must preserve exact amounts, nonces, policy identity and receipt identity across process failures. A deterministic state machine fits that requirement and adds no model API dependency or inference cost.

[Strands' agent loop](https://strandsagents.com/docs/user-guide/sdk/agents/agent-loop/) lets a model choose tools and continue reasoning over their results. Its hooks and structured outputs are useful for agent applications, but do not establish on-chain settlement or prevent a duplicate financial order. Our design judgment is to keep model-selected tools outside the signing boundary. Strands could later read health reports or investigate exceptions through restricted read-only tools; it is not required for this executor and is not installed as a trading dependency.

The existing paid Grok/X prohibition remains in force. Building or running the executor's shadow mode makes no paid model calls.

## Separate execution and observation accounts

The automatic executor has its own durable journal and holdings. It never books a paper fill into a real account, and a paper acceptance test cannot become strategy performance. An observed market opportunity is input for evaluation, not proof that this wallet purchased anything.

The active Robinhood strategy supplies public candidate, pool, safety and buyer evidence. Any future live entry must validate fresh evidence and size against the execution account's own cash, holdings, reservations and risk limits. Replaying a paper BUY verdict is not sufficient. Exits must use the executor's reconciled holdings and their original exit policy, independently of slow discovery, history qualification or model research.

## Order lifecycle and integrity requirements

1. Bind an intent to its account, network/genesis, wallet, policy and source event. Reserve the required exposure before signing. A repeated source event must not create a second order.
2. Build a supported transaction locally. Verify its recipient, assets, exact input, minimum output, fees, nonce, deadline and permitted contract calls. Provider descriptions never authorize arbitrary calldata.
3. Check the wallet-specific route and transaction. Signing authority must be restricted to the approved policy and wallet. Secrets stay outside source, research exports, model prompts, reports and Telegram.
4. Persist the signed transaction and its exact identity before submission. Submission timeout or a missing receipt means unresolved, not failed. Recovery must reconcile or rebroadcast those same bytes under bounded rules; it must not silently replace the transaction or spend another nonce.
5. Update holdings and costs exactly once from a canonical, sufficiently confirmed receipt and validated wallet cash flows. A submission acknowledgement, quote or simulation is never a fill. A reverted transaction incurs its actual gas expense and creates no purchased/sold quantity.
6. Continue eligible exits when new entries are halted. Unknown holdings, noncanonical receipts, unexplained account movements and unavailable routes remain unresolved; they must not be converted into fictional zero-price sales.

## Initial chain and route scope

The first automatic execution adapter targets **Robinhood Chain, chain ID 4663**, which is the active paper strategy. The restricted adapter supports the original V3 SwapRouter exactInputSingle ABI and a **SwapRouter02 deadline wrapper containing exactly one exactInputSingle call**. It constructs and validates the complete calldata locally; arbitrary nested calls, additional operations, Universal Router, Permit2 and native wrapping remain unsupported.

The official [Uniswap deployments registry](https://developers.uniswap.org/deployments) lists Robinhood SwapRouter02 at 0xCaf681a66D020601342297493863E78C959E5cb2. A read-only probe verified its runtime code and factory/WETH getters on chain 4663, along with the factory, quoter and settlement deployment. The public observation is stored in runtime/executor-deployment-research.json. Those observations supply candidate deployment pins, not user execution authority or an independent contract audit. No deployment address is guessed from another chain.

Both supported settlement assets are EIP-1967 proxies: USDG has 6 decimals, and wrapped native WETH has 18. The bounded public probe in runtime/executor-proxy-probe.json observed USDG implementation 0x68184c449e1a8f34fa18d289737129fd27b66f8f and WETH implementation 0xc6b81b429797e0f555440b70cd99e032d7ae947e at one canonical block. Each execution policy binds one settlement asset and must use that asset's correct implementation pins; switching the settlement asset is a separately reviewed policy/account decision. These are public deployment observations, not an approved live policy. A proxy-shell hash alone does not bind its implementation; the adapter checks both the implementation address and implementation runtime hash, and rejects unexpected beacon/delegation state. Dynamic target tokens with populated EIP-1967 implementation or beacon slots are rejected. These slot checks are not a general proof that arbitrary bytecode cannot delegate or upgrade; independent contract review and the required current safety evidence remain necessary. A code fingerprint cannot guarantee token safety or prevent a future upgrade after a check.

[Uniswap's original swap interface](https://github.com/Uniswap/v3-periphery/blob/main/contracts/interfaces/ISwapRouter.sol) defines the exact-input/minimum-output pattern. The [Router02 interface](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol) omits the inner swap deadline, so this adapter permits only its [deadline-checked multicall](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/base/MulticallExtended.sol) with exactly one swap. A compatible interface name is not proof that a deployed contract is trustworthy. The adapter validates its deployment and complete allowed call shape, including the simulation result.

The optional signing dependency is [eth-account](https://eth-account.readthedocs.io/en/stable/eth_account.html), which provides local Ethereum transaction signing. An installed package is not signing authorization: account policy and signer checks remain separate. No private key or seed phrase should be sent through chat.

## Solana boundary

The separate Solana RUG DESK remains blocked by its unfunded, disabled Grok/X gate. Its existing prepare_live.py and SolanaExecutionReader can collect Jupiter quotes, simulate unsigned transactions and reconcile supplied finalized receipts. They do not supply a reviewed automatic instruction validator.

[Jupiter V2 /order](https://developers.jup.ag/docs/api-reference/swap/order) returns quote metadata and an assembled transaction. Before an automatic Solana signer could accept it, the implementation must independently decode the message, resolve address lookups, validate every permitted program/instruction and account, check recipients/amounts/fees, and verify the intended pool. Matching JSON route metadata and a successful simulation do not perform those checks.

Solana automatic signing/submission is therefore **unsupported**, not silently routed through the EVM executor. The future adapter must preserve the same durable submission/recovery/receipt rules. The Solana RPC documentation explicitly notes that [successful sendTransaction](https://solana.com/docs/rpc/http/sendtransaction) does not establish confirmation. Its paper account and seven-day clock remain separate from Robinhood.

## Deployment boundary and current validation

A finalized-only settlement policy and one unresolved wallet transaction at a time prioritize conservative recovery. Settlement delay can postpone later orders, including exits; this is not a high-frequency or guaranteed-stop execution service. Receipt finality policy and operational latency require review before live deployment.

The first installed mode must be **shadow**, with no signing authority, no broadcasts and no real fills. Offline acceptance tests use a separate disposable account and a controlled transport. They exercise software behavior; they do not prove a mainnet deployment, wallet balance, profitable strategy or completed live execution.

The implementation separates the SQLite execution journal (`execution.py`), restricted EVM adapter (`execution_evm.py`), independent source/safety/risk orchestration (`execution_service.py`) and runner (`executor.py`). The live collector has its own events and opportunity journals; it does not depend on the paper trial's entry deadline. The installed shadow worker imports candidate inputs for observation and never imports paper fills.

Risk valuation includes size-bounded liquidation quotes, exact native expenses valued at current market rates, and conservative remaining transaction fees. New-position sizing reserves nine transaction fee ceilings: entry and partial/final exit reset, approval and swap operations. Existing positions reserve six ceilings before partial profit-taking and three afterward. Daily equity checks run each execution cycle and latch a 2% entry halt. These current-price stress marks are risk controls, not historically converted realized USD P&L. The ledger retains actual base-token amounts and native fees separately.

Release validation is recorded with the installed worker's status and the isolated acceptance result. A synthetic acceptance pass demonstrates software behavior, while source-only deployment observations establish public chain identities. Neither establishes an authorized, funded or validated live account: `live_execution_ready=false` remains authoritative until the explicit deployment prerequisites are completed.

Initial live deployment is a separate user-controlled action requiring a public wallet, approved capital and risk limits, reviewed route deployment/code identity, restricted signer provisioning, wallet-specific route/simulation validation, recovery testing and required strategy evidence. Ordinary trades then execute automatically under that authority. Do not fund a wallet, create a key or enable a paid service merely to make setup appear complete.

See [executor operations](EXECUTOR_OPERATIONS.md) for commands, mode boundaries, operational evidence and recovery checks.
