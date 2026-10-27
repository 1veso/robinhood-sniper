# Two-minute Telegram monitor

`monitor.py` watches the configured Robinhood Chain Uniswap V3 factory, checks
new pools, and sends an informational message only when current safety evidence
passes and a reviewed watchlist wallet has a corroborated purchase. It places
no orders and does not change the frozen paper account or its five-wallet rule.
One reviewed buyer is enough for an informational alert.

## Pair Telegram

1. Create a dedicated bot through the official [BotFather](https://t.me/BotFather)
   using `/newbot`. Follow [Telegram's guide](https://core.telegram.org/bots/tutorial).
2. Save only its token on one line in `runtime/telegram-bot-token.txt`. Keep it
   out of chat, source code, command arguments and Git.
3. Open the new bot in Telegram, press Start, and send exactly `SNIPER SETUP`.
4. Within 30 minutes, run this from the extracted installation, replacing the
   username with the bot's actual username:

```powershell
python telegram_setup.py --expected-bot YourSniperBot --send-test
```

Setup verifies the bot identity and requires exactly one matching private chat.
It refuses an existing webhook, ambiguous chats or replacement of a paired
recipient. The token is never displayed. The recipient is saved locally in
`runtime/telegram-recipient.json`; delivery confirmation is recorded in
`runtime/telegram/setup-status.json`. A Telegram acknowledgement means Telegram
accepted the message; it does not establish that the human read it.

## Run

```powershell
# Collection and qualification without sending:
python monitor.py

# One collection/delivery cycle:
python monitor.py --send

# Register the separate two-minute Windows task:
.\install_monitor.ps1
```

The task `SNIPER Telegram Monitor` calls `pythonw.exe` directly for hidden
execution without waiting for a PowerShell launcher. It ignores overlapping
invocations. An OS lock also blocks concurrent writers. A slower run finishes
before another can acquire the lock. Task Scheduler runs only while Windows is
awake and this user is signed in; the schedule is not a latency guarantee.
Do not also run `monitor.py --send --watch` while using Task Scheduler.

The task may be installed before pairing: discovery and checks run, while
Telegram remains `SETUP_REQUIRED` and no messages are sent. Paid AI is never
used. Put a file at `runtime/telegram/STOP` to stop subsequent monitor cycles.

## Evidence gates

- New pool means a canonical factory event observed within the last 24 hours;
  it does not prove the token itself is newly deployed. First startup begins
  with a bounded recent 2,000-block window. Subsequent polls resume the cursor.
- The automatic reviewed-wallet output is `runtime/research/qualified-wallets.json`.
  Its research summary must be no more than five minutes old. An absent research
  output falls back to `runtime/wallets.json`. No example wallet is promoted.
- The existing collector corroborates purchases through successful swaps,
  transaction sender, receipt transfers and net token/payment flows. Holdings
  or incoming transfers alone do not count. Unsupported routes can be missed.
- Each new token receives bounded read-only bytecode/owner observations. These
  alone cannot establish safety. Unknown checks remain unknown.
- `runtime/safety.json` must contain a current matching `SafetyEvidence` record
  passing every configured paper-policy safety check. Any false, missing,
  future-dated or expired evidence blocks a market alert. See OPERATIONS.md.
- Purchases must match the current reviewed-wallet evidence and exact pool,
  and be within the policy's 15-minute confirmation window. Sending rechecks
  evidence, current watchlist membership and canonical block hashes.
- Collection failures or budget exhaustion defer all market alerts for that
  cycle. A pending connection test is explicitly labelled and is not a signal.

Missing verified safety or qualified wallets means zero market alerts even
when Telegram is correctly connected. This is a recorded blocked state, not
a reason to replace missing evidence with a passing result.

## State and delivery

`runtime/telegram/events.sqlite3` stores canonical discovery/purchase events.
`alerts.sqlite3` stores assessments and a durable outgoing queue. Their UUIDs
are pinned in `identity.json`; missing or replaced journals halt the monitor
instead of silently resetting duplicate protection. State commits before sends.
Source events can be replayed after an interrupted evaluation without duplicates.

The token address is unique in the queue across pools and ordinary restarts.
An acknowledged message is never resent automatically. Confirmed rejections
remain queued with backoff; at most one message is delivered per invocation.
Expired or invalidated messages are suppressed. The
[Telegram sendMessage API](https://core.telegram.org/bots/api#sendmessage)
has no request-idempotency parameter: a response timeout or crash during a
send is recorded as `uncertain`, and is not automatically retried. This favors
duplicate prevention and can miss a message that never reached Telegram.

After checking the chat, resolve uncertain delivery explicitly:

```powershell
python monitor.py --resolve-uncertain 0xTOKEN --resolution received
python monitor.py --resolve-uncertain 0xTOKEN --resolution suppress
# Only after confirming the message was not delivered; requires fresh gates:
python monitor.py --resolve-uncertain 0xTOKEN --resolution not-delivered
```

Use the saved `connection-test:BOT_ID:CHAT_ID` key instead of a token address
when resolving a setup test. Resolution itself sends no message. Never delete
the journals to fix a provider outage. Read `runtime/telegram/status.json`
for cycle health, blockers, cursor and queue state. Generic errors never print
credential-bearing URLs or raw Telegram error descriptions.

## Supplied article corrections

The pasted script sends every new pool regardless of safety or whale activity,
uses balances as purchase evidence, decodes the wrong PoolCreated ABI word,
and saves its cursor after sending. This implementation reuses the tested
decoder and purchase verification, applies the requested conditional gate,
and persists delivery state. The older prototype under `C:\Dev\SNIPER` has
separate thresholds and example wallets; it is not activated by this installer.
