# Radon delivery validation

Base inspected: `a8b34ecf76c6732f544b58ce12662634798d66c7` on `main`.
The checkout was clean. Published branches were `main` and
`feat/helium-trading-cockpit`; no seventh implementation was available. The owner
explicitly requested reserving slot seven and using slot eight for the renamed
desk. Radon uses dedicated code/config/runtime identities throughout.

Validated in the managed Linux workspace on 2026-11-01:

- Full suite: `python run_tests.py` — **1,132 tests, OK; 16 optional-dependency
  skips**. Tests that bind loopback sockets require sandbox network permission.
- Focused Radon suite: **41 tests passed**, including financial reconciliation,
  concurrent cap enforcement, attributed partial exits, skipped leader buys,
  duplicate/restarted source delivery, unknown inventory, stale marks, dropped
  leaders, expired selection, model outages, entry changes during research,
  shared/own model budgets, proof attribution, account isolation and packaging.
- `python tests/verify_radon_console.py` — real Chromium smoke test passed using
  a synthetic account, with desktop/mobile screenshots, zero page errors,
  reserved slot verification, Helium/Radon switching and rejected POST writes.
  Local artifacts are under `runtime/radon-ui/` and excluded from releases.
- Python compilation, JavaScript syntax checks, and `git diff --check` passed.

Two pre-existing POST-rejection assertions failed on untouched `a8b34ec` as
well: Helium's journal and Jev's detail tests expected old 501/405 responses, but
the existing centralized local-session guard returns 403. Their assertions now
match that existing guard. The guard and existing bot execution behavior were
not changed; their no-mutation checks remain in place.

No real source, credential, wallet, paid inference, funded proof, desktop worker,
or live service was enabled. No real trading performance is claimed. The source
interface accepts authorized normalized exports; the continuous feed producer
and operator-run model validation are deployment dependencies. Windows task
definitions were reviewed and covered by registration/packaging tests, but were
not installed or executed on Windows in this Linux environment.
