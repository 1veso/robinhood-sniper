# Seven-day paper autopilot

The local supervisor runs all nine desks through their existing isolated workers.
Argon retains its designed relationship to Xenon's paper account. No signing,
wallet transaction, real execution or Telegram delivery is authorized by this setup.

## Installation

From the repository root, with existing restored paper accounts and dependencies:

```powershell
.\.venv\Scripts\python.exe -X utf8 paper_autopilot.py enroll --authorization-reference "Explicit operator request for seven-day paper trading"
.\scripts\install-paper-autopilot.ps1
```

Enrollment takes SQLite backups, preserves balances/positions/account identities,
and records the previous and new windows. Repeating enrollment preserves the window.
It does not fund accounts, fabricate fills, authorize inference spending, or waive
entry evidence and approval gates. Nitrogen still requires explicit order approval.
Historical comparison accounts retain their original deadlines and attribution.

The Windows task `SNIPER Paper Autopilot` owns one persistent hidden supervisor.
It starts isolated worker processes at existing cadences, with independent exit
workers, no overlap, durable next-run times, bounded timeouts, local logs and
native Windows job objects. A supervisor crash closes those job handles and
terminates its descendants. Task Scheduler checks every minute and restarts after
failure; logon startup is configured. Previous owned tasks are disabled and their
XML definitions saved under `runtime/desktop/previous-tasks`.

An administrator installation attempts S4U plus a boot trigger. A standard-user
installation falls back to InteractiveOnly: it runs while that Windows user is
signed in (including a locked screen), and resumes after sign-in. It cannot run
while Windows sleeps or the computer is off. The installer does not change power
settings or request/store a Windows password.

## Local activation on November 1, 2026

The common primary-account window is **November 1, 16:45:02 through November 8,
16:45:02 Europe/Berlin**. All entries are simulated. New entries stop at the deadline;
exits, marks, reports and the preview continue. Evidence gates can still prevent any
fills; worker uptime is not proof of strategy readiness.

Oxygen's four Codex schedules were replaced by local jobs: watch every ten minutes,
pulse hourly, scan every two hours, and report at 21:30 Europe/Berlin. Its eleven
article source blocks remain unchanged and are verified before every job. The root
wrapper enforces its paper scan deadline. Do not additionally run the former Codex
schedules or invoke the article's scan directly to bypass the wrapper.

Open <http://127.0.0.1:8765/> for the preview. Each desk displays the trial window and
observed supervisor freshness. `paper_autopilot.py status` provides per-job states.
Logs and trial manifests live in ignored `runtime/desktop`; never publish that
folder, credentials, or database backups. Paid request deduplication and existing
spending authorization remain in each strategy's provider layer.

## Validation

Tests cover preserved balances and positions, account-bound deadlines, repeat
installation, continued exits, original comparison deadlines, console observations,
and Windows process cleanup after a supervisor crash. Paper integration acceptance
uses isolated synthetic fixtures; those trades never count as market performance.
