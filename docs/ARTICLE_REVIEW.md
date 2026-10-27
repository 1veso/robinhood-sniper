# Article review and integration — 2026-10-27

The operator supplied the complete “Grok Bot from zero in one night” article
and a linked ChatGPT adaptation review. The chat reader truncated each long
message at 20,000 characters; the subsequently supplied 38 KB article supplied
the missing source-code sections. The original article was reviewed in full.
The final portion of the earlier assistant review remains unavailable.

The operator selected **improve the current system and add Solana research
screening**, rather than adding a separate Solana trading strategy.

## Findings and implemented corrections

| Finding in supplied article or existing integration | Result in v0.1.2 |
|---|---|
| Article's watcher calls `book.sell` even in live mode, and treats a missing pool as a zero-price sale | Existing SNIPER remains paper-only; missing quotes retain holdings with conservative unresolved valuation. No live executor added. |
| JSON/CSV writes can diverge or race; article reports partial exits as separate green exits | Existing SQLite transactions and whole-position accounting retained. |
| Missing account file can silently reset a book | Service now pins an account UUID separately and refuses missing/replaced databases; CLI supports `--existing-account`. |
| Slow collection precedes exit checks in SNIPER | Existing positions are checked first, before base-price acquisition and chain collection. This does not create an independent real-time watcher. |
| Long collection cycles can time out before research runs | Collection uses 100-block commits and a 60-second cooperative RPC budget. Completed chunks persist; unfinished ranges retry. Budget exhaustion defers entries and still permits research. |
| Rugcheck and market code independently choose the largest pool | Solana screen pins the exact Rugcheck pool and refuses caller mismatch, unavailable pools and ambiguous duplicate records. |
| Missing creator/insider fields become zero in article; absent authorities resemble explicit null | Existing unknown-value handling retained; no invented zero or authority revocation. Rugcheck units/labels remain provider assumptions requiring audit. |
| Original market parser defaults missing transaction counts to zero and rounds age | Screen validates exact millisecond age, future timestamps and nonnegative integer hourly counts. |
| Article says prose fails, but extracts braces from prose | Existing strict JSON validation retained; incomplete/missing completion status now rejects explicitly. |
| A model's BUY/confidence is treated as an authorization | Advisory labels remain separate from the trading engine; confidence is not calibrated profitability. |
| Article cost formula omits fetched X profiles | Usage retains both fetched-post and fetched-profile counts; billed-cost ticks are recorded directly. |
| Search can be offered but never actually run | Optional X module requires a documented tool-call record and reported fetch counts; claims must reference returned X citation URLs. |
| “Latest profiles” described as paid fresh launches | Feed is treated only as leads. Free scan keeps the article's seen-once behavior, including failed/interrupted screens. |
| Seven paper days presented as a transition to live | No automatic promotion. Neither seven days nor a passing filter establishes profitability or executable returns. |

## Thirteen deterministic screens and the X stage

`python run.py screen SOLANA_MINT --screen-config config/solana-screen.toml`
applies these checks in order and reports the first failure. Later checks are
marked `NOT_RUN`; missing values are `UNKNOWN`, not passes.

| Stage | Checks |
|---|---|
| Facts (1–9) | Not already rugged; mint authority; freeze authority; danger flags; risk score; LP lock; top-ten concentration; creator holding; insider evidence |
| Pool (10–13) | Liquidity; reference ticket/liquidity share; exact pool age; hourly buy/sell pattern |
| X research (14) | Separate opt-in evidence research; absent by default |

The separate research configuration retains article screen thresholds: score
40, LP 90%, top ten 30%, creator 5%, no detected insiders, liquidity $30,000,
reference ticket $25 at no more than 1% of pool liquidity, age 30 minutes–72
hours, and rejection of 20+ hourly buys with zero sells. A screen pass never
creates a position. Indicative quotes remain non-executable. Report retrieval
time does not establish underlying on-chain data freshness; a screening pass
does not establish holder independence, complete control analysis or sellability.

`solana_research.py` runs a bounded free batch when due every 30 minutes within
the existing Windows service. Its SQLite journal claims each mint before
network work, preventing repeated judgments after restart. Results are in
`runtime/solana/latest.json` and `runtime/solana/screens.sqlite3`.

## Optional X research

The dedicated `research-x` command defaults to disabled. It is never called by
the scheduler, never writes the wallet qualification/safety/route manifests,
and never accesses an account ledger. It supports Solana and Robinhood tokens.

```powershell
# Disabled path: no key, API call or charge.
python run.py research-x SOLANA_MINT --request-id unique-research-id

# After deliberate paid opt-in and local key setup:
python run.py research-x SOLANA_MINT --request-id unique-research-id --enable-paid-ai --key-file runtime/xai-api-key.txt --output runtime/x-research.json
```

Create a key in https://console.x.ai/ and save only the key on one line in
`runtime/xai-api-key.txt`. Never put it in chat or a command argument. This
installation has not made a paid xAI validation request during this update.
The operator supplied a key but reported no funds for topping up. Paid
inference remains disabled. Authentication/model access can be checked without
an inference request:

```powershell
python run.py doctor-ai --key-file runtime/xai-api-key.txt --output runtime/xai-doctor.json
```

This only calls `GET /v1/models`. It does not establish credit balance, ability
to run paid inference, or successful X Search execution.
The actual model-list request returned HTTP 403: xAI reported the team has no
credits or licenses. `runtime/xai-doctor.json` records `CREDITS_REQUIRED` without
the private team identifier. No inference or search request was made.

The request uses `grok-4.7`, the Responses API, strict output schema, at most two
search rounds and 1,200 output tokens. The date filter uses UTC calendar dates;
it is not an exact rolling 72-hour filter or a point-in-time backtest source.
Tool activity and returned citation metadata must corroborate the response.
Zero fetched posts can only produce UNKNOWN. Citation matching does not itself
prove that a claim follows from a post; that still requires review.

Each X request reserves $0.50 against the existing $1 UTC-day advisory budget.
Subsequent checks count the greater of reservation or returned billed cost.
Failures retain reservations; responses missing valid billed cost block further
paid requests that day pending reconciliation. Provider charges can exceed a
reservation before the response arrives: these are local admission controls,
not a vendor-enforced monetary cap. Configure provider spending limits too.

The API's `usage.cost_in_usd_ticks / 10^10` gives reported billed USD, including
token/tool charges. Only validated numeric usage fields are retained. Actual
usage is recorded even when the subsequent model output is rejected.

## Existing strategy preserved

`config/paper.toml` is unchanged: Robinhood wallet following, five reviewed
controller groups, $50,000 minimum liquidity, 2% aggregate full-loss budget,
0.5% per-position budget, and the existing UTC equity-based daily halt and exit
rules. The article's $50 realized-loss halt, Solana ladder, liquidity-drop exit
and Berlin-day accounting were not substituted into this frozen experiment.

Remaining work includes qualified wallet histories/controller evidence,
current contract safety and size-aware executable routes. Actual alert delivery,
an independent exit process, live execution and a separate Solana trading
account are not implemented by this update. Local report files are not proof
of notification delivery.

## Primary sources checked

- [Rugcheck API specification](https://api.rugcheck.xyz/swagger/doc.json): report endpoint exists; complete response field semantics are not specified.
- [DEX Screener reference](https://docs.dexscreener.com/api/reference): token profiles and pair data, with no promise of executable pricing.
- [Grok 4.7](https://docs.x.ai/developers/models/grok-4.7): current model and supported capabilities.
- [Structured outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs): supported combination with tools.
- [X Search](https://docs.x.ai/developers/tools/x-search): function names, date behavior and per-post/per-profile charging.
- [Citations](https://docs.x.ai/developers/tools/citations): response annotation format.
- [Cost tracking](https://docs.x.ai/developers/cost-tracking): exact per-response cost units.

The article's profit screenshots, account-history claims and claimed filtering
edge are not independently verified evidence and were not used to loosen rules.
