# Helium operations

Helium is internal key `helium`, the sixth standalone paper-trading surface.
Its internal team serves only its own candidate decisions and positions. The
selected unchanged Krypton baseline and operating commands are in
[the Helium guide](../helium_bot/README.md).

## Account and authority

`helium_bot/runtime/book.sqlite3` is authoritative. Account UUID, baseline
configuration hash, bound team policy, candidate evidence, internal handoffs,
model costs and paper results remain scoped to Helium. Shared adapters and
accounting code do not share balances or execution decisions.

Initialize explicitly with `python helium_bot.py init`. Installation, console
reads and the existing desktop initializer do not create this account. Repeating
initialization preserves cash, holdings and existing authorization files.
Configuration changes require the existing explicit migration discipline;
reinstallation cannot reset a study. This experiment does not inherit another
bot's trial clock.

Grok and Jev are required for new model-approved entries, with disabled settings
created by default. Paid calls, live trading, funding and signer provisioning
are not enabled by implementation or installation. There is no Helium live
adapter or automatic promotion. Existing account-specific and shared spending
gates continue to apply. Uncertain model billing cannot become zero-cost
performance evidence. Grok's baseline invoice requirement remains in force.
Jev requests retain budget reservations and a token-rate cost estimate; those
provisional costs reduce risk equity while complete net performance remains
unknown until the corresponding bills are reconciled.

## Independent jobs

| Desktop worker | Windows task | Cadence | Child limit |
| --- | --- | --- | --- |
| `helium-scan` | `HELIUM Paper Scan` | 1 minute | 50 seconds |
| `helium-watch` | `HELIUM Paper Watch` | 1 minute | 50 seconds |

`scripts/desktop-jobs.ps1 -HeliumOnly` limits operations to those two names.
Installing them requires the initialized Helium book. `-CheckOnly` and `-Remove`
can inspect or remove owned tasks even if the book is missing. General desktop
installation adds Helium tasks only after explicit account initialization.
Neither operation overwrites an existing task or changes other bot schedules.

Workers use the existing limited Windows user, S4U background execution and a
30-second boot-recovery trigger. This does not make an offline or sleeping PC
available. Each worker has a separate wrapper lock and application lock. Task
time limits and provider budgets are bounded work controls, not guarantees of
market or exit latency.

The position watcher does not invoke either model. It uses the policy bound to
the original account, so editable entry-config drift cannot alter its stop or
profit target. An unavailable quote retains the position and an unresolved mark;
it does not create an imaginary sale. An entry failure does not change the
watcher's schedule.

## Verification and reporting

Inspect actual finished scan/watch records, provider observations, candidate
progress, evidence-bound role outcomes, positions, fills and errors. Wrapper
completions are under `runtime/desktop/helium-scan.json` and
`runtime/desktop/helium-watch.json`. A fresh scheduler status alone is not proof
of completed work. An enabled authorization flag alone does not prove current
model readiness.

The shared console exposes Helium as a read-only sixth surface. Account data,
internal reviews and model-blocker information describe this account only.
Synthetic acceptance evidence stays isolated and does not enter the production
account or any other bot's performance.

## Trading cockpit

Open `http://127.0.0.1:8765/?bot=helium` while the existing console service is
running. The cockpit uses NEON's responsive layout and visual components, with
Helium's separate account, history and internal responsibilities. It updates
from the read-only account API every five seconds. The scan and exit workers
continue independently when the browser is closed.

| Area | Recorded evidence |
| --- | --- |
| Account performance | Conservative equity, reconciled net P&L when available, separately labelled estimates, return and persisted account observations. |
| Pipeline | Scout → Filter → Analyst → Execution → Guard, with the candidate, outcome, counts and blocking reason. Grok analysis and Jev evidence review belong to the Analyst stage; the Chief only coordinates Helium's work. |
| Market tape | Timestamped token/pool observations collected by the workers. An idle heartbeat is not a market quote. |
| Decision stream | Durable discoveries, checks, model outcomes, admission decisions, committed fills, market marks and worker outcomes. Rejection filters and candidate details expose the underlying evidence. |
| Positions | Entry and observed prices, quantity, basis, unrealized P&L, fixed stop and full take-profit target. Missing quotes retain the holding; they do not record a sale. |
| Operating status | Market freshness, paper execution and scan/watch health are distinct indicators. Model gates, provider failures and overdue workers remain visible. |

The chart is observation history, not reconstructed tick data. Missing marks and
long monitoring gaps remain visible as gaps. Old accounts start recording the
new observation series when an upgraded worker opens them; the console cannot
invent past equity observations or migrate the book. Journal tables are added
by writable workers without resetting balances, positions or strategy history.

Stage highlights follow new journal records. Fill notifications require an
actual committed fill record; a model BUY label alone cannot trigger an entry
notification. Initial page load shows history without replaying old trade
alerts. Disconnects retain the last view with a visible stale/disconnected
state, and reconnects use account-bound journal cursors.

Helium keeps the exact inherited strategy: a 2% all-in position ceiling, a
−25% stop, a full +50% take profit and a 5% daily equity-loss halt. This baseline
has no partial take-profit ladder or trailing stop. The cockpit labels those
features accordingly. Radeon retains its independent whale-following **OR**
two-wallet V3 entry routes, and the other bots retain their own strategies.

### Paper now; live later

The current execution account is explicitly PAPER. A future live account must
have its own identity and execution journal. Paper fills and history cannot be
relabelled as live activity. The cockpit reserves the live-mode presentation,
but activation is unavailable until Helium has a configured, validated live
adapter and account. The existing disabled/shadow executor is not automatically
adopted as a working Helium executor by this UI change.

Once a live path is implemented and deliberately activated, the intended
workflow is automatic eligible entries, management and exits within the bound
account policy. Per-trade approval is not the intended live workflow. This
cockpit implementation does not activate a live account or enable paid model
calls. When required model access is disabled, scans display the blocker;
independent paper-position monitoring remains available.

### Read-only interfaces

- `/api/state?bot=helium` — account snapshot, cockpit projection, market tape and recorded equity series.
- `/api/helium-journal?after=<account-cursor>&limit=200` — bounded durable journal catch-up; optional `reason` and `stage` filters.
- `/api/helium-candidate?mint=<public-mint>` — candidate decisions, evidence summaries and recorded fills.

These endpoints retain the existing loopback Host/Origin boundary and never
call market/model providers, create accounts, place orders or change settings.

### Runtime acceptance

The isolated staged cockpit replay drives the real Helium scan/watch and paper
account code with fictional provider/model inputs. It tests rejection, entry,
deduplication, mark updates, quote outage, recovery and a full take-profit close.
The replay has its own account identity, displays a REPLAY banner and makes no
network requests. It verifies software behavior, not real-market performance.

From a source checkout, use a new empty path for the isolated replay:

```powershell
python tests/verify_helium_cockpit.py --app-root runtime/helium-cockpit-review --prepare
python tests/verify_helium_cockpit.py --app-root runtime/helium-cockpit-review --play --interval 0
python tests/verify_helium_cockpit.py --app-root runtime/helium-cockpit-review --verify
```

The replay deliberately advances a synthetic scenario clock by 72 seconds to
exercise stale prices without a long wait. Its saved `state-*.json` files retain
the exact observation time for each stage. They are acceptance evidence, not
current market snapshots.

Installed-PC verification is separate: start the console and both Helium
workers, then check their recorded completions, actual provider observations
and account-specific model gate status. A Windows scheduled-task registration
alone is not proof of a completed entry or exit. The PC must stay awake and
connected for continuous operation.

Windows packaging includes the Helium launcher, public configuration, guide,
source and console assets. The packaging allowlist excludes all runtime books,
provider authorization files, credentials and acceptance outputs. This source
change does not itself rebuild, install or run a Windows executable.
