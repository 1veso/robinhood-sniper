# DEV WATCH — seventh bot integration

Open `http://127.0.0.1:8765/?bot=devwatch`. The seventh panel displays the existing
DEV WATCH ledger: holdings, pending exits, pending buy proposals, checks, recorded
fills, API costs, original thresholds, and the seven-bot signal readiness table.
No synthetic cash balance, current valuation, or independently verified wallet
execution is inferred. Views only read files; they do not run models or confirm fills.

The standalone source remains under `dev_watch/`. Integration did not change its
eleven Python files. The previously authorized live switch and confirmed-exit fix
remain the only article deviations, checked by its source manifest.

```powershell
.\.venv\Scripts\python.exe dev_watch_bot.py status
.\.venv\Scripts\python.exe dev_watch_bot.py verify
.\.venv\Scripts\python.exe signals.py
```

The wrapper also supports scan, watch, pulse, and report, in the isolated working
directory and environment. Scan and pulse can spend xAI credits. Existing Codex
routines remain the scheduler; do not install a second overlapping set of jobs.
The console reports the last recorded ledger change, not a fabricated worker
completion. No HTTP control starts a trade, authorizes a model, or confirms a fill.

## Restoring the other six views

The user will supply the original account backup. Preserve these paths together:

- `runtime/` (Radeon, research, Argon, shared settings and account selectors)
- `rug_desk/runtime/` (Xenon)
- `neon_bot/runtime/` (Neon)
- `krypton_bot/runtime/` (Krypton)
- `helium_bot/runtime/` (Helium)

Argon shares Xenon's execution account. Preserve SQLite databases together with
their WAL files from a consistent stopped-worker backup. Do not replace or merge
the new `dev_watch/` ledger. Do not overwrite the new shared xAI credential with
an older backup credential. The local setup remains intentionally uninitialized
for those accounts until the backup arrives.

After restoration, validate each bot in order against its original identity and
settings. A successful DEV WATCH key test is not an account-bound proof for the
other bots. Xenon and Neon need their current Grok inference/X proofs; Krypton
and Helium need their own model proofs as well as the shared gate. Argon and
Helium's Jev reviewer use a separate TypeSafe API credential. Radeon primarily
uses on-chain purchase signals; Grok is optional research, not a missing entry rule.
Retain each existing budget, factual gate and paper/live boundary. Do not
manufacture paid proof files, waive missing facts, or start new accounts to get
past a missing backup. No unattended real-money executor was enabled.

`GET /api/signals` and `signals.py` report file presence and remaining validation,
never imply that a present API key proves all bots are operational.
