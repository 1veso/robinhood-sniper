# Radon — Copy Trading Desk (slot 8)

Radon is the eighth bot slot in SNIPER. Slot seven remains reserved. Radon owns
its account, strategy, bankroll, event cursor, model attribution, and results.
The existing Rug Desk, Neon, Krypton, Helium and Radeon strategies are unchanged.
There are seven implemented console views, not eight funded accounts; Argon
continues to share Xenon's account.

This implementation supports **internal paper execution only**. It has no wallet,
signing, broadcast, CopyFomo bridge, or live-mode switch. No desktop workers or
paid-model services are enabled by opening its console view.

## Initialize and inspect

From the application root:

```sh
python radon_bot.py status             # Read-only; does not initialize anything.
python radon_bot.py --demo             # Isolated synthetic acceptance scenario.
python radon_bot.py init               # Explicit independent paper account.
python radon_bot.py model-status       # Read readiness; no inference.
python radon_bot.py report
```

The view is `http://127.0.0.1:8765/?bot=radon` in the existing console. Synthetic
demo accounts live under `runtime/radon-demo/<id>/`, never in the prospective
`radon_bot/runtime/` account. The demo prints its account/report location.

Configuration lives in `radon_bot/config.json` and is bound, together with the
shared execution-cost policy, to the initialized account. An edit cannot silently
reinterpret an existing ledger. Entry jobs reject policy drift; the exit worker
uses the original stored policy. Future retuning requires an explicit reviewed
migration, which is not implemented as an automatic reset.

Initial policy: $1,000 virtual cash, $10 fixed buys, $30 per-mint episode buy cap,
$300 deployed-capital limit including costs and retained rent, and $50 minimum
cash reserve. The candidate minimum is ten historical buys, a minute median
closed-position hold, and a 1.5 copy multiple after modeled costs. Defaults are
an experiment, not optimized parameters or advertised-return targets.

## Source availability and supported imports

**No authorized Fomo feed is connected.** There is no `FOMO_COOKIE` option or
automated browser/session extraction. The existing Neon leaderboard is not
presented as the Fomo universe. An absent export displays SOURCE_UNAVAILABLE.

A licensed/otherwise authorized producer can provide a normalized export:

```sh
python radon_bot.py import --source /private/authorized-events.json
python radon_bot.py import --source /private/authorized-history.json --history
python radon_bot.py research
```

Radon validates the schema and requires an authorization reference; this is an
operator attestation, not independent legal/source verification. Keep that
reference and upstream completeness evidence available for review. A transport
adapter for a particular authorized feed is still required for continuous intake.
The producer may atomically replace `radon_bot/runtime/source.json` to deliver
new batches. Private runtime files are excluded from packaging and Git.

See [export contract](../docs/radon-export.md). Source exports must retain stable
opening inventory and a contiguous per-leader sequence. Duplicated pages are
safe; changed IDs/payloads, sequence gaps, unknown inventory, impossible sells,
or out-of-order timestamps stop processing. The monitored source must continue
covering leaders with open positions even after removal from the shortlist.

Supported normalized route labels are `raydium`, `pumpfun`, and `pumpswap`.
These describe accepted export coverage and paper policy, **not live swap
execution support**. Transfers and token-to-token activity that cannot be
reconciled must be represented as `unsupported`; never omit them to imply a
complete inventory. Only classic SPL holder data is supported by the reused
integrity adapter. Unsupported Token-2022 evidence blocks new entries.

## Shared replay and accounting semantics

`Book.buy` / `Book.sell` are the internal paper execution interface used by both
the chronological replay and prospective service. They share these rules:

- A buy is exactly the configured ticket; no partial last ticket and no winner
  scaling. Fees/rent are reserved separately from the ticket cap.
- All leaders share a per-mint **cumulative buy-notional episode cap**. Sells do
  not replenish it while any Radon quantity in that mint remains. The cap resets
  only when the entire mint is flat. Positions retain their originating leader.
- A leader sell removes that same fraction of that leader's copied quantity,
  with proportional cost-basis allocation. Another leader's lot is untouched.
- Every supported leader buy updates leader inventory, including a skipped copy.
  An unknown opening balance never becomes an assumed full exit.
- Buys compete for one finite cash balance and one deployed-capital limit.
  Model bills and unresolved reservations reduce available cash. Replays of
  several shortlisted traders are combined chronologically, not added together.
- Fees, liquidity impact, adverse slippage, network costs and rent come from
  `sniper.execution_costs`. These are explicit scenarios with `executable=False`.
  Rent is retained capital, not an automatic refund or P&L expense. Embedded
  friction is already in fill prices and is not charged twice.
- Starting capital, buying turnover, peak deployed capital, realized P&L, open
  value and net account return are separate fields. Missing/stale quotes make
  open valuation and net return unknown. Missing exit quotes leave pending
  events; they never create a sale at zero or an old price.

Run conditional research without touching the prospective ledger:

```sh
python radon_bot.py replay --source /private/authorized-history.json
```

Replay requires archived quote/SOL-cost observations at the simulated delayed
fill time; it never substitutes a leader fill or today's prices. Numerical
market gates are applied identically. Historical X posts and historical safety
state are **not reconstructed**. The result is labelled conditional historical
research, not a historically fully gated strategy. Candidate selection is
in-sample; survivorship and selection bias remain explicit limitations.

Research stores immutable snapshots, each with source hash, ranked results,
largest-winner concentration, KOL identity status, and overlapping leader mints.
It then freezes a versioned shortlist with an effective timestamp. Only later
signals can enter the prospective account. Incomplete histories cannot qualify.
KOL-marked leaders require wallet identity evidence; holding time is computed
from closed episodes rather than copied from a profile average.

## Entry and exit workers

Entry sequence:

1. Import complete source observations and check current shortlist, timestamp,
   opening-inventory completeness and supported route.
2. Obtain pool volume/liquidity/age, classic SPL mint/freeze and complete-holder
   evidence, and pinned-pool LP evidence through reused adapters.
3. Obtain required, authorized X evidence. Reuse `rug_desk.grok.validate` for
   strict output schema, completed search evidence, citations and billed usage.
4. Refresh the source and the same pool quote. If the leader sold during
   research, the signal expired, the source is stale, authorization changed,
   or capital/cap limits fail, skip before committing a paper fill.

Pool age is explicitly a pool timestamp, not verified token launch time. These
checks and price estimates do not establish a sell route for the intended
quantity. There is no claim of sellability verification.

Exit jobs never call the model or require the current shortlist. They continue
processing known sells during model, entry-policy and source-refresh outages.
An unresolved older exit blocks new entries for that mint until reconciled.
Observed-at, research-completion and fill timestamps are retained in evidence.

Run workers independently (separate terminals/process supervisor units):

```sh
python radon_bot.py run --job entries
python radon_bot.py run --job watch
python radon_bot.py scheduled --job research
python radon_bot.py scheduled --job report
```

The default loop polls every ten seconds; actual completion latency is measured,
not promised. Slow providers/model calls can exceed the signal lifetime and
cause skips. Research and report jobs check configured UTC hours (03:00 and
09:00), persist successful day completion, and catch up missed daily slots.
Use a scheduler to invoke the `scheduled` commands regularly.

On a Windows installation, after explicit Radon initialization:

```powershell
powershell -File scripts/desktop-jobs.ps1 -AppRoot $PWD -Python (Get-Command python).Source -RadonOnly -CheckOnly
powershell -File scripts/desktop-jobs.ps1 -AppRoot $PWD -Python (Get-Command python).Source -RadonOnly
```

The installer defines separate research, entry, exit and report jobs. It preserves
existing task definitions and checks ownership. Minute triggers start bounded
entry/exit polling loops; they do not represent hourly copying. Restarted
workers deduplicate events transactionally. A killed model request remains
reserved and its interrupted candidate is not automatically billed again.

## Paid research authorization

Initialization creates **disabled** `radon_bot/runtime/paid-ai.json`. Having an
installation key or another bot's authorization is insufficient. A Radon request
requires all of:

- The existing installation paid-model gate and funded proofs remain valid.
- Radon's own explicit `enabled`, `operator_authorized`, `approval_reference`
  and binding to bot/account/policy/model/credential.
- Radon-bound `inference-test.json` and `x-search-test.json` funded validation
  proofs within seven days. Proofs must come from an explicitly authorized
  validation workflow, not hand-labelled assumptions or another bot's account.
- Reconciled prior Radon request billing and available Radon **and shared** daily
  spending reservations. Every request has Radon account attribution.

After the operator explicitly authorizes this account, obtain its binding with
`python radon_bot.py model-binding` and place it in Radon's authorization file
alongside the actual approval reference. This command only reads the configured
key to compute its digest; it neither prints the key nor spends money. Then use
the account-specific funded validation commands:

```sh
python radon_bot.py model-test --test-kind inference --allow-paid
python radon_bot.py model-test --test-kind x-search --mint PUBLIC_SOLANA_MINT --allow-paid
```

Each command makes one billable request only after both installation and Radon
authorizations pass. It records actual usage, request attribution and bound
proofs. Validation does not grant authorization or bypass budgets. This delivery
does not execute these commands or enable services. Without proofs the entry
gate stays closed while exits remain available. Keys never enter prompts or reports.

## Verification and limits

Run `python run_tests.py`. Focused tests are `tests/test_radon.py`; the browser
smoke test exercises the existing shell, reserved slot, Radon state and switching.
The synthetic demo is not a performance backtest.

The authorized live source producer, operator-run funded model validation,
production desktop deployment, and any future live executor remain explicit
integration dependencies. There is no automatic messaging or external copier
configuration. Reports are local and read-only through the console.
