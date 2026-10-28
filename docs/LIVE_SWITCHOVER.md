# Live switchover preparation — 0.1.19

Both strategies remain paper-only. Free checks do not authorize real money, paid
X, signer provisioning or funding. Future routine trades must be automatic after
an explicitly authorized initial deployment. Existing configurations, accounts,
cash, holdings, costs, exits and deadlines are unchanged.

The earlier 0.1.18 checks below are dated preparation evidence. See [0.1.19 execution changes](EXECUTION_RELEASE_0.1.19.md). The free CP setup probe returned no route; refreshed EVM evidence showed 956 seconds of finality lag and zero balances. Draft settlement is Solana USDC and EVM WETH; neither capital amount, signer nor deployment is approved.

## Completed

- `live_check.py` reuses saved public wallets, server-key preference and read-only
  RPC clients. Timestamped evidence is separate from strategy ledgers.
- Solana has an explicit Metis `/swap/v2/build` preparation path. It does not
  silently replace `/order` or change allowed strategy pools. Restricted `/order`
  returned HTTP 400 `Failed to get quotes` even without the fee override; `/build`
  returned instructions with the same server key.
- A bounded v0 compiler/decoder rejects extra signers, writable programs,
  malformed vectors, out-of-range accounts, trailing bytes and oversized packets.
  Active lookup tables are read from RPC and compared with provider descriptions.
  The compiled message is decoded again to verify instruction/account identity.
- Preparation checks exact assets/size, unsplit continuous routes, USDC/WSOL
  intermediate assets, supplied target-pool identity, slippage and fee bounds.
  Extra transfers, tips and cleanup fail closed. Only idempotent classic SPL ATA
  creation for this wallet is supported. Token-2022 setup is unsupported.
- Both actual-wallet USDC/WSOL setup builds reached mainnet simulation without
  signatures or state overrides. Both returned `AccountNotFound`, zero execution
  units and a zero native balance. Observed network fees were 7,228 and 7,922
  lamports; rent, swap fees and funded execution are still unverified.
- Robinhood chain/genesis and router/factory/quoter/base bytecode pins matched.
  USDG proxy implementation matched. WETH implementation was observed separately;
  the example policy still selects USDG. Wallet native/USDG/WETH balances and
  pending nonce were zero. No key was loaded.
- A canonical snapshot measured **978 seconds of finalized-block lag**. This is
  not transaction inclusion latency, but serialized finalized-only settlement can
  postpone exits. Initial EVM deployment now requires a reviewed
  `max_finality_lag_seconds` and `finality_review_reference`. New entries recheck
  the bound. Receipt recovery and exit quotes remain independent. No acceptable
  threshold was invented or approved.
- Console Health now shows preparation checks, remaining blockers and evidence
  freshness/binding for each strategy. It cannot run probes or change authority.
- Console cache loads are isolated by view. Slow chart reads no longer hold the
  shared lock used by other accounts and task observations; repeated requests for
  one view still share a single load.

## Repeat free checks

From the installed app with its Python:

```powershell
python live_check.py solana
python live_check.py evm
```

From source, add `--app-root C:\Users\User\Downloads\sniper-v0.1.0` before the
chain argument. Results go to `runtime/live-preparation/<chain>-preflight.json`
and a unique evidence directory. These bounded commands are not scheduled polls.
They never call xAI or transaction-submission RPCs, and never load signing keys.
The Solana diagnostic restricts its setup pair to the observed `TesseraV` DEX label
to avoid unsupported intermediate assets. This is neither a strategy-token safety
pass nor an approved live DEX manifest.

## Remaining blockers, in dependency order

| Blocker | Required resolution | Boundary |
|---|---|---|
| Solana Grok/X | Funded access, explicitly authorized spending, successful basic/X tests | User chose paid calls disabled; do not bypass or start the trial |
| Solana instruction semantics | Independently review the implemented direct classic-SPL CP-Swap validator and deployment pins; verify an available supported route | Arbitrary provider instructions remain unsupported; old preparation artifacts remain `signable=false` |
| Solana automatic lifecycle | Restricted signer, durable signed-byte journal, expiry-aware submission/recovery, finalized receipt integration, original exits/costs/notices | Restricted lifecycle code is implemented and offline tested; funded mainnet lifecycle validation remains missing |
| EVM finality design | Review and approve the implemented optional confirmed-position exit semantics and measured latency | Final cash/fills/P&L remain finalized; provisional management is disabled |
| Wallet execution | Authorized validation of selected strategy routes, balances, rent, fees, setup and failed attempts | No funding during current paper setup; unfunded simulation cannot prove a successful mainnet swap |
| Initial live policy | Approve capital, settlement asset, exposure/loss/gas limits, emergency behavior and review references; bind a separate execution account | Example policy remains incomplete/unauthorized; paper cash is not a real allocation |
| Signer deployment | Approved local restricted provisioning, dependency validation and independent review | Source crypto tests do not mean the paper installer contains a ready live signer |
| Strategy evidence | Review complete cost-aware trial, policy segments, open losses, unavailable routes, downtime, API costs and sample size | Acceptance tests are excluded; profit alone cannot authorize deployment |

Each strategy must pass its own requirements. EVM readiness cannot start Solana,
and setup probes do not establish strategy profitability.

## Primary interface references

- [Jupiter Metis build](https://developers.jup.ag/docs/api-reference/swap/build)
- [Jupiter order](https://developers.jup.ag/docs/api-reference/swap/order)
- [Solana transaction structure](https://solana.com/docs/core/transactions/transaction-structure)
- [Solana RPC structures](https://solana.com/docs/rpc/json-structures)
- [Solana simulation](https://solana.com/docs/rpc/http/simulatetransaction)

These sources document interfaces, not an independent security audit of this app.
