# Krypton · Solana new-token paper scanner

Krypton is the fifth TENDIES TRACKER bot. It has its own account, cash, holdings,
configuration, decisions and worker history. It does not share strategy P&L with
Radeon, Xenon/Argon or Neon. It has no live execution implementation or wallet-key input.

## Existing Windows installation

Open **TENDIES TRACKER**, then choose **Krypton**, or visit
<http://127.0.0.1:8765/?bot=krypton>. The console is read-only. Its five-second
refresh is not a five-second market feed. Two background jobs scan and monitor
exits every minute; actual completion and provider timestamps appear separately.

Public scanning works without Grok spending. **Paid Grok remains disabled on this
PC**, so new paper entries are blocked even if the hard checks pass. Adding a key
does not enable paid calls. Existing global authorization, a separately approved
Krypton account/configuration/credential binding and a current successful funded
inference test are required before paid model use. This release does not grant them.

## Run from this repository

Use Python 3.12 or newer with timezone data on Windows. The existing app interpreter
is `rug_desk/.venv/Scripts/python.exe`. From the repository root:

```powershell
python -m pip install -r requirements-krypton.txt
python krypton_bot.py --demo
python krypton_bot.py init
python krypton_bot.py --mode paper
```

`init` creates the account once and never resets it. `run` runs a foreground
watch/scan loop; do not run this loop alongside the scheduled jobs. For one cycle,
use `python krypton_bot.py scan` or `python krypton_bot.py watch`. Read the account
with `python krypton_bot.py status`. Stop a foreground loop with Ctrl+C.

The installed console is already running. On a separate checkout, run
`python console_server.py` and open the same loopback URL (one server per port).
No separate Flask server, Claude installation, mainnet wallet or funding is needed.

Optional environment file:

```powershell
Copy-Item krypton_bot/.env.example krypton_bot/.env
```

Edit locally; never paste or commit credentials. `.env` is ignored by Git.
`MODE` defaults to `paper`; any other value stops before account/network work.
`SOLANA_RPC_URL` overrides the saved public RPC file, otherwise public Solana RPC
is used. The existing saved xAI credential is consulted only after authorization.
There is no automatic endpoint substitution or paid-provider upgrade.

## Policy

| Rule | Default |
|---|---:|
| Starting virtual cash | $1,000 |
| Main-pool liquidity / age | ≥$25,000 / ≥10 minutes |
| Largest owner / top 10 owners | ≤10% / ≤40% |
| Mint / freeze authority | Both disabled |
| LP status | Pinned pool reports 100% locked/burned |
| Grok risk | ≤4/10 with exact supplied-number citations and no missing data |
| Position cash commitment, including costs/rent | ≤2% of conservative net equity |
| Stop / full take profit | −25% / +50% of modeled entry fill price |
| Daily loss halt | 5%, Berlin calendar day, latched until next day |
| Discovery / exit schedule | Every 60 seconds |

Every hard rule must PASS. FAIL or UNKNOWN skips the token before Grok. A mint is
seen once; its chosen pool stays pinned. Old/rejected profiles are not replayed to
force admission. Expired pending candidates are logged as skips. No arbitrary
seven-day clock is inherited from another account.

Before `init`, thresholds are in `config.json`. After initialization the exact
configuration is ledger-bound. Changing it requires an explicit account migration;
editing the file alone stops workers safely rather than rewriting history. Existing
positions retain their original exits. There is no console policy editor.

## Evidence limits

DEX Screener profiles provide limited discovery, not a complete launch feed. The
largest eligible main pool is selected by observed liquidity, then pinned. No
alternative pool is substituted for a missing position quote.

Solana's top-20 response lists **token accounts**, not distinct holders. Krypton
aggregates owners. Partial samples can prove a failing lower bound, but cannot
prove a passing upper bound. Potential passes require a bounded complete classic-SPL
account enumeration, supply reconciliation and unchanged revoked-authority mint
reads around the finalized snapshot. Provider refusal, oversized enumeration,
Token-2022 or inconsistent facts remain UNKNOWN. Pool vaults are not silently
excluded. Owner identities do not establish independent controllers or deployer
history. No serial-rug-deployer database is connected.

Rugcheck LP evidence is **provider-reported**, bound to the token and selected pool.
It is not independent chain verification. Retrieval time does not establish the
provider's underlying state timestamp. Missing pool mappings/fields, contradictions,
danger flags or less than 100% reported locking block admission. Only the public
GET report is used; no paid fresh regeneration or simulation is requested.

Grok receives numbers only after hard checks pass. Strict JSON, exact numerical
citations, timestamps, account/configuration and evidence hashes are checked again
at entry. Citations do not independently verify the model's reasoning. Unknown bills
block new paid requests and leave full net P&L unknown. Failed/ambiguous model calls
are not retried automatically. The existing shared daily API budget is retained.

## Paper accounting and exits

SQLite commits cash, quantity, basis, fills and decisions atomically. Exits do not
depend on Grok or the scanner. Missing quotes retain the holding; an unknown mark
is conservatively valued at zero, never recorded as a sale. A stop is checked when
fresh data arrives, not a guaranteed execution price. Round-robin watching preserves
pending positions if the bounded job cannot finish every quote.

The inherited `solana_cost_scenario_v1` models 30 bps DEX fees, 10 bps platform fees,
100 bps adverse slippage, liquidity-based impact, 5,000 + 100,000 lamports network
expense and 2,039,280 lamports reserved rent per entry. SOL/USD marks must be fresh.
These are assumptions applied to indicative prices, not executable routes or actual
chain fees. Rent is unavailable capital with no assumed refund; conservative equity
excludes it. Extra stress costs and actual known API spending are displayed separately.

## Files and review

- `krypton_bot/runtime/book.sqlite3`: authoritative isolated account and journals.
- `runtime/account.json`, `paper_state.json`, `status.json`: derived views under `krypton_bot/`.
- `krypton_bot/runtime/logs/decisions.jsonl`: buy/skip/sell/error decisions with stable IDs.
  Export is at-least-once after a crash; deduplicate by ID. SQLite is authoritative.
- `runtime/krypton-acceptance/demo-*/`: three fictional tokens, mocked model and
  entry/full take-profit lifecycle. Zero network calls; excluded from all strategies.

The console shows the latest 50 decisions, all-time hard-check counts, actual paper
positions, completed-trade statistics, costs and worker/provider status. Inspect a
decision for exact checks and evidence. No Telegram messages are enabled by this
installation. Review a meaningful sample of natural closed trades before changing
one rule at a time; demo profits and a small profitable sample establish no edge.

The implementation is split into small modules and reuses the existing cost and
console components. Durable accounting and explicit failure handling take more code
than the guide's approximate 600-line prototype target.

## Official API references checked for this implementation

- [DEX Screener API](https://docs.dexscreener.com/api/reference): profiles (60/min), token pairs (300/min).
- [Solana largest accounts](https://solana.com/docs/rpc/http/gettokenlargestaccounts),
  [multiple accounts](https://solana.com/docs/rpc/http/getmultipleaccounts),
  [program accounts](https://solana.com/docs/rpc/http/getprogramaccounts).
- [Rugcheck public OpenAPI](https://api.rugcheck.xyz/swagger/doc.json): GET token report;
  paid `refresh` is not used. Undocumented/missing response fields fail closed.
- [xAI Chat Completions](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions):
  legacy supported endpoint matching the guide. Real funded model access remains untested here.

This is an educational paper experiment. The guide's viral return claim was not
verified and is not used as a benchmark or promise.
