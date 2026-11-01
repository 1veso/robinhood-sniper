# Oxygen / DEV WATCH — current local state

Updated November 1, 2026, Europe/Berlin.

## Current authorization

The user's November 1 instruction, PAPER TRADING FIRST, supersedes the October 31
manual-live switch. PAPER=True is required. Do not switch to live until the user
explicitly requests that later phase. Paper entries/exits are simulated and may
be recorded automatically by the article's existing strategy. No wallet signing,
fund transfers, real-money orders or actual-fill registrations occur in this phase.

DEV WATCH is renamed Oxygen, slot 9. Nitrogen is slot 7 and Radon is slot 8.
Source and data remain in dev_watch/. The console is at
http://127.0.0.1:8765/?bot=oxygen; the old devwatch URL remains compatible.

## Article fidelity and runtime

ARTICLE.txt preserves the supplied article. article-manifest.json records the
original eleven source blocks. Config is back to the original PAPER=True.
Only book.py and watch.py retain the approved live-exit confirmation fix, pinned
by approved-live-exit-fix.json. Every strategy threshold, prompt, model and rule
priority is unchanged. verify_article.py requires paper mode; stop on mismatch.

Always use Python UTF-8 mode. The November 1 scan failed because Windows cp1252
could not encode a Unicode token name. The -X utf8 runtime flag fixes logging
without changing the original article code or repeating that paid request.
verify_paper_mode.py proves paper entry/exit and Unicode logging offline.
verify_live_mode.py and test_live_exits.py use isolated fixtures to retain the
approved manual-live behavior; they do not register real fills.

From this directory:

    .\.venv\Scripts\python.exe -X utf8 verify_article.py
    .\.venv\Scripts\python.exe -X utf8 -u scan.py
    .\.venv\Scripts\python.exe -X utf8 -u watch.py --minutes 9 --every 20
    .\.venv\Scripts\python.exe -X utf8 -u pulse.py
    .\.venv\Scripts\python.exe -X utf8 report.py

The root oxygen_bot.py wrapper verifies sources and uses the isolated directory
and interpreter. dev_watch_bot.py remains compatible. Existing local .env and
shared Grok credentials are preserved. Never print them or put secrets in chat.

## Existing schedules

Oxygen now runs under the persistent local `SNIPER Paper Autopilot` supervisor.
Its four former Codex schedules were deleted to prevent duplicate paid scans and
watchers. Local cadences remain watch every ten minutes, pulse hourly, scan every
two hours and report at 21:30 Europe/Berlin. The root wrapper verifies the article
before each job and stops new scans at the enrolled seven-day paper deadline.

See ../docs/PAPER_AUTOPILOT.md for startup recovery, account preservation, logs
and the signed-in Windows session requirement. Do not recreate duplicate Codex
workers. Never invoke book.py add/confirm/close to manufacture wallet fills.

## Repository and recovery

Current main includes Nitrogen and Radon. The top-level runtime was restored
from D:\SNIPER RUNTIME\runtime, and all 52 SQLite files passed quick_check.
The subsequently expanded backup supplied the original Xenon, Neon, Krypton and
Helium account folders. Those identities and Argon's original journal are now
restored; the temporary replacement accounts remain archived under
runtime/local-setup/solana-rollout-20261101. Nitrogen and Radon retain their own
new accounts. Radeon retains its account/deadline. Both public wallet addresses
were confirmed; no signer was configured. Each desk retains separate strategy
state, except Argon's existing design as a filter on Xenon's shared paper account.
See ../docs/SOLANA_PAPER_ACTIVATION.md for current readiness and spending limits.

See ../docs/DEV_WATCH.md and ../runtime/local-setup/ for integration records.
The previous detailed tracker is archived at runtime/setup-before-paper-return.md.
Its PAPER=False instructions are historical and superseded by this file.
