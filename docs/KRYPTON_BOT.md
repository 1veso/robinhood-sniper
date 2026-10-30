# Krypton operations

See [the beginner guide and evidence limitations](../krypton_bot/README.md).

Krypton is internal key `krypton`, the fifth independent paper view. Its root is
`krypton_bot/runtime`, with `book.sqlite3` authoritative. No existing account,
policy, exit, deadline or paid authorization is inherited or changed. Grok shares
the existing global spending reservation journal only after both global and
Krypton-specific explicit gates pass. No paid access is enabled by installation.

Windows jobs `KRYPTON Paper Scan` and `KRYPTON Paper Watch` each run every minute
through the application interpreter and `desktop.py worker krypton-scan|krypton-watch`.
They use the same limited Windows user with S4U and 30-second boot recovery.
Separate process locks prevent duplicate scans/watchers; financial operations use
SQLite transactions. Each child has a 50-second limit; public reads share a
43-second cooperative budget. This is not a hard exit-latency guarantee.

The dedicated installer option `scripts/desktop-jobs.ps1 -KryptonOnly` creates only
these two task names and preserves existing tasks. The CLI requires explicit `init`;
console reads never create accounts. Optional `.env` support uses
`requirements-krypton.txt`. Initialized configuration changes require migration.

Supervision should inspect actual `runs.finished_at`, `providers` results/backoff,
`candidates` progress, positions/marks, decisions/fills and logs in SQLite, plus
`runtime/desktop/krypton-scan.json` and `krypton-watch.json` wrapper completions.
Fresh task inspection alone does not prove completed work. Interrupted runs remain
in history. Missing quote marks remain unresolved; never invent a fill or change pool.

Console `/api/state?bot=krypton` is read-only. `/api/health` lists the fifth view.
Worker, quote and browser-refresh times have different scopes. Krypton has no live
adapter, signing/broadcast capability, Telegram delivery or automatic promotion.
Synthetic `runtime/krypton-acceptance` records never enter production or another
strategy's returns. No existing trial clock is copied to this ongoing paper study.

Quiet supervision remains: notify only material failures, required action, recovery
or completed setup. Disabled Grok, unknown safety and ordinary rejected profiles are
expected states, not repeated alerts.
