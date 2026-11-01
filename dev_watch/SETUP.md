# DEV WATCH — article implementation tracker

Updated: 2026-10-31 (Europe/Berlin), after the user-authorized live exit fix.

Status: ARTICLE MANUAL LIVE MODE, PAPER=False. The user explicitly requested
"lets switch to live trading" before completing the paper week. All four local
Codex routines remain ACTIVE with updated live-mode instructions. There are no
open positions, no pending buys, and no wallet connected. No order was executed.
The code proposes trades for the user to execute manually; it has no swap signer.

This is a new standalone directory, separate from the existing rug_desk and other bots.
ARTICLE.txt preserves the entire supplied article. All eleven Python blocks were
extracted verbatim, with only line endings normalized and final blank lines trimmed.
article-manifest.json retains the ORIGINAL source and file SHA-256 values.
Approved original-code changes: config.py PAPER=True -> PAPER=False, and the
requested live exit confirmation fix in book.py and watch.py. No strategy,
model prompt, model, threshold, or rule priority changed. verify_article.py checks
the original hashes plus the exact approved book/watch hashes in
approved-live-exit-fix.json. live-exit-fix.patch preserves the full source diff.

The user requests local Windows operation. The article assumes a Grok Bot Linux
cloud computer at /workspace/desk. Local support files are clearly separate from
the article files. The four recurring jobs are local Codex heartbeat automations
attached to this chat, replacing Grok's cloud routines. They deliver the article's
requested output here. This is not a Windows service or a Grok cloud deployment:
the computer must stay awake and Codex must stay running. Grok cloud Auto-review
settings are not installed locally; the routine instructions forbid source edits
and wallet actions. The scripts have no wallet signing implementation.

## Progress through the article

| Step | Status |
| --- | --- |
| 01 prerequisites | Python 3.12.10; isolated .venv ready; requests installed; pip check passed; xAI key loaded |
| 02 DEV WATCH profile | Profile preserved below; cloud bot creation does not apply to the requested local host |
| 03 credentials and API proof | Key imported from user-specified local file without printing it; .env ACL restricted; RPC and grok-4.7 proof passed |
| 04 architecture | Reviewed; standalone directory created |
| 05 config.py | User-approved PAPER=False; grok-4.7, $25 ticket and all thresholds unchanged |
| 06 book.py | Live exit request/confirmation support; locked ledger mutations and atomic position writes |
| 07 facts.py | Copied unchanged |
| 08 grok.py and prompts.py | Copied unchanged |
| 09 scan.py | First authenticated scan completed at 17:44; 5 checked, 5 rejected, 0 buys |
| 10 enter.py and rpc.py | Copied unchanged; RPC getSlot verified; token snapshot and entry not exercised yet |
| 11 watch.py | Live exits create pending alerts only; paper fills and exit rule conditions unchanged |
| 12 pulse.py | Copied unchanged; empty-book smoke check passed, silent as expected |
| 13 report.py and routines | LIVE report verified; four Codex routines ACTIVE, updated for manual live mode at the same schedules |
| 14 paper week | Ended early on 2026-10-31 at the user's explicit request; the seven-day evaluation was NOT completed |
| 15 bill | costs.csv verified: first scan $1.09748, 87 X posts; setup proof separately $0.001428 |
| 16 troubleshooting | Preserved in ARTICLE.txt |

## Credentials (completed)

The user supplied C:\Users\benja\Downloads\xai-api-key.txt. Its key was read
without printing it and stored in this bot's own .env. The original key file was
not modified. .env has inheritance disabled and one explicit access rule for
the current Windows user. The key was validated with the article's model.

The following commands are retained for reference; do not rerun setup_env.ps1
against the existing .env (it intentionally refuses to overwrite credentials).

From a PowerShell terminal:

```powershell
cd 'C:\Users\benja\OneDrive\Documents\ChatGPT\robinhood-sniper\dev_watch'
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_env.ps1
.\.venv\Scripts\python.exe .\preflight.py --xai
```

Input is hidden. Enter your xAI API key, then an RPC URL or Enter for the public
endpoint. Do not paste credentials into chat. The helper restricts the .env file
ACL to your Windows user. This folder is inside OneDrive; filesystem permissions
do not disable OneDrive synchronization. No wallet credentials are needed.

The xAI proof call can incur a small API charge, just like step 03 of the article.
Do not treat an absent key or failed check as a completed setup.

## Commands after setup

Always run with this directory as the working directory: the original code uses
relative paths for .env, positions, trades, checks, costs, and pending entries.

```powershell
.\.venv\Scripts\python.exe .\verify_article.py
.\.venv\Scripts\python.exe .\scan.py
.\.venv\Scripts\python.exe .\enter.py <mint>
.\.venv\Scripts\python.exe .\watch.py --minutes 9 --every 20
.\.venv\Scripts\python.exe .\pulse.py
.\.venv\Scripts\python.exe .\report.py
```

After a proposed BUY, execute the trade yourself in your own wallet. Only after
an actual fill, register its amount and price using the article's command:

```powershell
.\.venv\Scripts\python.exe .\book.py add <mint> <actual_usd> <actual_fill_price>
```

This command registers a completed trade; it does not execute one. No private
key or seed phrase belongs in this bot.

For a live sell, wait for the actual wallet fill, then use the command printed
in the alert, supplying the actual token quantity sold and USD fill price:

```powershell
.\.venv\Scripts\python.exe .\book.py confirm <mint> <exit_id> <actual_token_qty> <fill_price>
```

See LIVE_EXITS.md for partial fills and confirmation IDs. Alerts do not reduce
holdings or write trades. The original book.py close <mint> <price> remains an
explicit manual confirmation that the entire remaining holding was sold; it is
not for partial fills and must never be run by a routine without a user fill.

## Active routines

| Routine | Schedule | Codex automation ID |
| --- | --- | --- |
| watch.py --minutes 9 --every 20 | Every 10 minutes | dev-watch-wallet-and-exit-checks |
| pulse.py | Every 60 minutes | dev-watch-hourly-x-pulse |
| scan.py | Every 2 hours | dev-watch-two-hour-x-scan |
| report.py, plus one questioned config number | Daily 21:30 Europe/Berlin | dev-watch-evening-paper-report |

All four were created through the Codex automation tool, viewed successfully,
and their saved ACTIVE states, schedules, and target chat ID were verified.
Target chat: 01a192b2-0af4-70b3-9e45-6fbefd40c8c7.
Scheduled delivery itself has not yet been observed at setup completion.
Manage these existing IDs rather than creating duplicates. Each routine verifies
the approved source checksums (PAPER=False and the confirmed-exit fix), and follows the article's
output rules. Silent watch ticks and scans without buys do not produce routine
status messages. Keep the computer awake and Codex running. API usage continues
on the two-hour scan schedule and for pulse/entry reads when positions qualify.

The Windows system timezone was verified as W. Europe Standard Time (Berlin).
The article's relative file paths and local-day bookkeeping are unchanged.
The watch's nine-minute window and ten-minute cadence retain the original gap.
With an empty book, watch.py exits immediately, exactly as written in the article.

## Article profile, preserved

```text
Name: DEV WATCH
Job: Solana shitcoin desk, paper first
Description: Build and run the desk in /workspace/desk from the article I paste.
Every token enters through enter.py. Nothing gets bought any other way.
You never buy, sell, sign or move funds. You never edit config.py unless I ask in this chat.
You never loosen a number to let a token through.
When watch.py prints nothing, you say nothing.
When a source is down, name it once and move on. Never fill a missing number with a guess.
```

## Original defects and current handling

- FIXED at the user's explicit request: live watch exits, including pool_gone,
  now store pending_exit in the position, without calling book.sell. Quantity,
  trade rows, realized P&L, took, and trimmed_on_fade change only on a confirmed
  fill. Partial fills leave a new pending ID and remaining quantity. Completed
  take/fade flags wait until their requested quantity is actually filled.
- FIXED as part of confirmation integrity: ledger mutations use a shared file
  lock so watch/pulse writes cannot overwrite a concurrent fill confirmation.
  Atomic position writes prevent readers seeing partially written JSON. The
  trade CSV schema and strategy calculations remain unchanged.
- watch.py fetches the pool before it checks wallets; a failed pool request skips
  the whole token's tick. "Wallets first" describes exit priority, not request order.
- Some missing API fields become zero in facts.py, despite the prose's broader
  "missing field is a kill" claim. Preserve the source; do not silently fix it.
- Source error deduplication and report commentary belong to the Grok routine
  instructions, not the eleven Python files.

Current API documentation confirms the model and X Search interface:
https://docs.x.ai/developers/models/grok-4.7
https://docs.x.ai/developers/tools/x-search
https://docs.x.ai/developers/cost-tracking

## Initial paper setup verification results (historical)

- All eleven source checksums and Python syntax checks passed.
- Windows secure-entry script passed PowerShell syntax parsing; interactive key
  entry has not been run, so credential storage has not yet been exercised.
- requests 2.34.2 installed with dependencies in this bot's own .venv;
  requirements-installed.txt records the installed versions. pip check passed.
- watch.py and pulse.py exited successfully and silently with no positions.
- report.py printed an empty PAPER report with zero checks, trades, and spend.
- Public Solana getSlot succeeded (latest setup proof: slot 463776394).
- xAI grok-4.7 OK proof succeeded: 14,280,000 ticks = $0.001428. As in the
  article's curl command, this setup call is not written to costs.csv.
- First unchanged scan.py run completed successfully and silently (no BUY lines).
  It checked Hallowinu (danger: Low Liquidity), HUSS (thin_pool), PUMPKIN
  (insiders), ROYALE (danger: Creator history of rugged tokens), and DOBRIKCOIN
  (top10_heavy). All five were SKIP at the door. No thresholds were loosened.
- costs.csv recorded scan cost $1.09748 and 87 X posts. Total observed xAI
  setup-plus-scan spend: $1.098908. report.py correctly displayed about $1.10
  model spend, zero realized P&L, zero buys, and no open positions.
- No wallet transaction was made. Entry model judgment, a qualifying-token RPC
  snapshot, and exits on populated positions remain unexercised by live data,
  because no token passed the article's door. Do not force a trade to test them.
- The sandbox blocked package-server and RPC network access. Authorized
  out-of-sandbox retries succeeded. A writable runtime/tmp directory was used
  for pip because the sandbox's default temporary directory rejected writes.
- These checks establish authenticated scan operation and active schedules,
  not a successful end-to-end trade cycle or profitable results.

## Continuation instructions

Repository integration update: DEV WATCH is the seventh bot in the local
1veso/robinhood-sniper console at http://127.0.0.1:8765/?bot=devwatch. This added
a read-only projection and panel, a repository CLI wrapper (dev_watch_bot.py),
and a seven-bot signal-readiness inventory (signals.py). None of the eleven
article implementation files changed during integration. Existing Codex jobs
remain the sole DEV WATCH schedule; no duplicate Windows workers were installed.
The shared xAI key was securely placed at runtime/xai-api-key.txt, without
enabling any other bot's paid authorization or execution. The user is supplying
the original six-bot account backup; do not initialize replacement accounts.
Resume restoration and account-specific signal validation once its path arrives.
See docs/DEV_WATCH.md for the backup layout and remaining provider dependencies.

Read this tracker and verify_article.py output before resuming. The current
authorized mode is PAPER=False (manual live alerts). Do not restore paper mode
or refuse the approved config difference because older chat messages say True.
Do not request the article again: ARTICLE.txt is the complete local source.
Do not edit any original file or loosen settings without the user's instruction.
Record actual scheduled run results here when relevant. Do not confuse active
schedules with an observed background execution. Preserve the current schedules
unless the user requests changes. The paper week was stopped early by the user.
Never execute wallet transactions. Do not register hypothetical fills as real
buys or sells. Require the user's actual fill details before confirming a sale.
The mode flip and live exit confirmation fix are explicitly authorized; source
changes beyond those and threshold changes need a new user instruction.

## Live switch verification

- User authorization: "lets switch to live trading" on 2026-10-31.
- Original code change: only PAPER=True -> PAPER=False in config.py.
- Reason: user requested the article's live mode. $25 ticket, four open positions,
  six daily entries, $50 daily loss halt, and every filter/exit number preserved.
- No open paper positions, pending buys, or trade ledger existed to migrate.
  Existing scan/check/cost history was retained; no data was discarded.
- Offline verify_live_mode.py passed: a mocked BUY writes a pending approval and
  never opens a position; a snapshot-failed SKIP adds no pending order. Fixtures
  were isolated from the real bot state; no network or wallet calls were made.
- report.py prints LIVE; empty watch and pulse invocations exited successfully.
- All four existing automation IDs were updated in place, retaining cadence and
  target chat. They require PAPER=False and manual user execution. Reports and
  alerts must not imply that an exit alert proves a wallet sale.
- At the mode switch the exit defect was preserved; the subsequent explicit
  request to fix it superseded that restriction (see verification below).

## Confirmed-exit fix verification

- User authorization: "fix the exit issue so it only records a sale after I
  confirm the actual fill, and keep everything else exactly as written in the article".
- Only book.py and watch.py changed for the exit fix. config.py is still exactly
  the previously approved PAPER=False version. Eight other original Python files
  match the article byte-for-byte. Entry filtering, model prompts, schedules,
  threshold constants, and exit priority are unchanged.
- 12 offline regression tests passed, covering all ten exit reasons, absent or
  zero-price pools, actual-price P&L, partial fills, delayed take/fade flags,
  duplicate/stale IDs, invalid fills, interrupted partial targets, concurrent
  confirmation/update processes, CLI confirmation, and unchanged paper fills.
- The existing offline live-entry test still passes: BUY creates pending
  approval only, and SKIP does not create an order. Original checksum audit and
  LIVE report also pass. No real trades or paid API calls were made for testing.
- All four existing routines were updated in place to recognize the approved
  fix, prohibit automatic fill confirmation, and use confirmed-fill language.
  Their ACTIVE status, schedules, and target chat are unchanged.
- Confirmation trusts the actual fill details supplied by the user; it does
  not independently query a transaction receipt. Existing article fee/slippage
  accounting conventions are unchanged: P&L uses the supplied per-token price.
