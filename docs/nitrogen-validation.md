# NITROGEN implementation and validation

Scope: independent slot-seven paper account, $500 initial virtual capital, four-question
TypeSafe judgment, durable WATCH and operator-staged approval. RADON is slot eight.

## Implemented

- Own configuration, account identity, SQLite ledger, immutable candidate cycles,
  assessments, orders, factual supplements, paid request attribution and reporting.
- Supported public-chain SEARCH/WHALE decoder, deterministic RISK, fact-only dossier,
  exact article gate, one fresh WATCH retry, no entry before explicit approval.
- Atomic/idempotent approval and fill; independent proportional partial exits, stops,
  trailing exit and retained rent accounting. Missing prices remain unresolved holdings.
- Existing console, desktop job registry, Windows worker registration, health and public
  packaging integration. Three independent bounded jobs; Berlin 21:00 report and Sunday
  probability cohorts; 24h hypothetical rejected-candidate observation.
- Disabled initial paid authorization; shared and local reservations; explicit bound
  invoice reconciliation. No model calls, real-data workers, wallets or trading enabled.

## Offline acceptance

Local result: **1,183 tests, OK with 16 optional-dependency skips**; **51 focused
NITROGEN tests passed**. NITROGEN and RADON real Chromium desktop/mobile checks
passed with zero page errors. Python compilation, JavaScript syntax and diff whitespace
checks passed. GitHub CI results are attached to the PR separately.

`PYTHONPATH=src:. python -m unittest discover -s tests -p 'test_nitrogen*.py'`

Focused tests cover exact probability/score boundaries, unchanged shared Jev policy,
RISK/WHALE admission, sourced/missing facts, WATCH persistence and exhaustion, fixed
sizing across different probabilities, no preapproval fills, duplicate/concurrent
approval, expired orders, refreshed risks/prices/exit size, cash and pauses, model
outage exits, partial accounting, stale marks, isolated ledgers, separate paid authority,
shared ceilings, request attribution, invoice reconciliation and report cohorts.

`python run_tests.py` runs the full repository regression suite.

`python tests/verify_nitrogen_console.py` uses real Chromium on a loopback server with
a separate synthetic account. It verifies slot 7 and slot 8, desktop/mobile rendering,
NITROGEN/RADON switching, pending-order instructions, no JavaScript errors and rejected
POST writes. Screenshots are private runtime artifacts in `runtime/nitrogen-ui/`.

`python nitrogen_bot.py demo` is a separate synthetic acceptance scenario. Its profits,
responses and trades are fixtures, not strategy-performance evidence.

Windows CI validates Python 3.11/3.12/3.13 separately. Windows scheduled jobs and a desktop
installer were not launched from the Linux development workspace.

## Changed-file map

| Files | Responsibility |
|---|---|
| `nitrogen_bot.py`, `nitrogen_bot/config.json`, `nitrogen_bot/README.md` | CLI, immutable public baseline and operation |
| `src/sniper/nitrogen/{judge,dossier,model}.py` | Exact TypeSafe contract, factual admission, authorization/costs |
| `src/sniper/nitrogen/{book,providers,service}.py` | Account/approval/exit transactions, public data and independent jobs |
| `src/sniper/nitrogen/{view,report,demo}.py` | Read-only projection, factual reports and isolated synthetic scenario |
| `console/nitrogen.js`, `console/nitrogen.css` | NITROGEN view within the existing shell |
| `console/{app.js,index.html}`, `src/sniper/console.py` | Slot-7 routing, assets and health |
| `desktop.py`, `scripts/desktop-jobs.ps1`, `packaging/build_windows.py`, `.gitignore` | Independent worker registration and public/private packaging |
| `tests/test_nitrogen*.py`, `tests/verify_nitrogen_console.py` | Focused offline acceptance and browser verification |
| Existing console/Radon integration tests | Update expected roster now that reserved slot 7 is occupied |
| Root README and `docs/nitrogen-*.md` | Roster, evidence contract and delivery record |

Shared Jev acceptance-bound files and all RADON strategy/account/configuration files
are preserved. UI routing updates the legacy reserved-slot subtitle without changing
RADON's own module. No account runtime or credential belongs in the PR.
