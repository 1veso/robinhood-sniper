# Oxygen — DEV WATCH desk, slot 9

Open http://127.0.0.1:8765/?bot=oxygen. The legacy devwatch URL remains an alias.
Nitrogen occupies slot 7 and Radon slot 8, using their merged upstream sources.

The user explicitly returned the system to paper trading on November 1, 2026.
Oxygen uses the article's original PAPER=True: entries and exits are simulated
from observed prices. The approved live-exit confirmation fix remains available
and tested. All article thresholds, prompts and rule priority remain unchanged.
The article does not model trading fees/slippage or define starting cash; the
console does not invent those values.

Source, credentials and ledger files remain in dev_watch/. The original article,
source manifest, checks and model cost history are preserved. oxygen_bot.py is
the new entry point; dev_watch_bot.py remains compatible. Both support status,
verify, scan, watch, pulse and report. The wrapper verifies sources, uses the
isolated interpreter and enables UTF-8 so Unicode token names can be logged on
Windows. Scan/pulse use the existing paid xAI authorization.

Example from the repository root:

    .\scripts\paper-python.ps1 oxygen_bot.py status
    .\scripts\paper-python.ps1 oxygen_bot.py verify
    .\scripts\paper-python.ps1 signals.py

The existing four Codex routines remain Oxygen's only scheduler. Their old IDs
are retained; do not add duplicate Windows scan/watch jobs. The console only
reads data and cannot grant model access or confirm fills.

## Runtime restoration

The supplied top-level runtime was restored from D:\SNIPER RUNTIME\runtime.
All 52 SQLite files passed quick_check, with database and WAL copies verified.
The existing local Grok key was preserved. Old environments, temporary files,
process locks and process IDs were excluded. Dependencies were installed from
the repository requirement files into this machine's environment.

The missing Xenon, Neon, Krypton, Helium, Nitrogen and Radon accounts were
initialized as fresh paper accounts at the user's explicit request. Radeon
retains its restored identity, balances, history and original trial deadline.
Argon reviews Xenon and has no separate bankroll. Its old journal is archived
under runtime/local-setup/archived-argon-20261101; a fresh shadow journal binds
the replacement Xenon and Neon accounts. Old comparison ledgers retain their
original identities and are not rebound or presented as fresh results.

Both transferred public wallet addresses were confirmed by the user. No signer
was configured. Credentials remain in ignored runtime files with restricted
permissions. The failed public observation RPC was replaced with the verified
restored RPC. The paper-python.ps1 launcher and desktop child workers use UTF-8
and the installed CA bundle; TLS verification remains enabled.

## Individual bot activation

Installed accounts and successful chain reads are not complete strategy readiness.
Xenon and Neon need current account-bound Grok inference/X Search proofs; Krypton,
Helium and Radon also need their own bindings. Argon, Helium and Nitrogen need a
separate TypeSafe/Jev credential and supporting evidence. Nitrogen needs tracked
wallets and preserves staged paper-order approval. Radon needs supported live
event/history exports. Never substitute synthetic fixtures or copied historical
acceptance records for current provider proofs.

The signals.py CLI and /api/signals are read-only setup inventories. Detailed
restore, confirmed-public-wallet and chain-check records are in runtime/local-setup.
Other bots' recurring workers and paid gates are activated during the subsequent
bot-by-bot phase, after their requirements pass. No real-money execution was enabled.
