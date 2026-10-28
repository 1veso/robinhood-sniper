# Windows desktop distribution

The launcher opens **TENDIES TRACKER Observation Console** from the
**TENDIES TRACKER** desktop shortcut. Existing `RugDesk.exe` paths and background
task identifiers are retained for compatibility with installed accounts.

Version 0.1.17 supplies an unsigned x64 `.exe` installer, native launcher and private
CPython 3.14.10 runtime. Install per user without administrator rights. The launcher
opens the existing loopback browser console; trading and credentials are never
added as browser endpoints. Windows Task Scheduler runs the independent workers
after sign-in. Sleeping, signed-out and offline periods are not uninterrupted trading.

New machines have separate accounts and credentials. The package allowlists source
files and excludes every runtime directory, ledger, key, address file and recipient.
The installer verifies its embedded ZIP and every installed file by SHA-256. These
hashes detect corruption; they do not replace a publisher signing certificate.
The installer refuses to overwrite a nonempty unknown destination. If an existing
console task is detected, its account is opened instead of creating a duplicate.
All task ownership is checked before any task registration or deletion. Existing
actions/cadences are kept. New jobs use the bundled interpreter, bounded execution,
independent OS locks and durable start/completion/exit-code files in runtime/desktop.

First-run setup imports only allowlisted local files. Existing differing credentials
are preserved. Pairing reuses telegram_setup.py's exact expected-bot/private-message
validation and durable delivery journal. A new Robinhood trial requires Telegram's
setup acknowledgement and verified chain connectivity, then reuses activate_v2 and
the unchanged approved risk profile. It starts with the existing $1,000 paper budget.
There is no balance reset or top-up command. Reopening an account never restarts its
deadline. The empty Solana account uses the approved execution-cost model, paid AI
disabled, and no trial start. Operational preflight remains necessary for Solana.

New installations check the report's Europe/Berlin clock each minute, sending once
from 21:30 through 21:59. Missed hours stay missed; previews never consume this marker.
The normal report outbox deduplicates crash recovery. This works independently of
the Windows display timezone. Existing installations retain the prior daily task.

Paper fills are simulated against real observations. Public wallet association
does not make them chain transactions. Mainnet SOL/ETH cannot be topped up with
paper money. Devnet or local forks can test transaction execution with valueless
balances in isolated accounts, but are not the mainnet strategy's performance.
Profitability readiness and live deployment authority remain separate.

Uninstall disables only owned tasks with an explicit warning about halted exits,
and preserves data/program files. No migration to another PC is implied. Copying the
installer installs software; copying a live runtime requires a separate controlled
backup/migration process. Do not run a shared Telegram bot or wallet on two hosts.

Build: place the official Python archive and the six pinned pure-Python wheels in
runtime/desktop-build/downloads, then run `python packaging/build_windows.py` on
Windows with .NET Framework csc. Python's archive checksum is pinned in the builder;
wheel checksums and all payload hashes are recorded in dist/windows-build.json and
payload.sha256. Install needs no pip, shell configuration or network download.
The package omits optional real-signing dependencies and cannot enable live trading.
