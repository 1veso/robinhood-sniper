# Radeon runner ladder

The separately activated `runner-ladder-v1` paper exit revision targets stronger
participation in exceptional runners while adding protection before the first
profit take. Merely installing the code does not activate it.

| Condition | Action for a newly bound position |
| --- | --- |
| +40% observed gain before TP1 | Arm a cost-aware break-even condition; do not sell yet |
| +100% | Sell 10% of the original quantity |
| +300% | Sell 20% of the original quantity |
| +600% | Sell 20% of the original quantity |
| After TP1 | Keep the original 30% decline-from-recorded-high trailing stop |
| -35% | Original hard stop, with precedence over other exits |
| Six hours and gain below +20% | Original time exit |

The remaining 50% is a runner, subject to those exits, not an additional fixed
profit target. Percentages use the original cost-adjusted entry fill-price basis.
The protection condition compares modeled net sale proceeds for the remaining
quantity with its remaining entry basis, including entry and sale costs. It
applies only before TP1. Discrete quotes, costs, gaps and missing routes mean it
can close below break-even; it is not an exchange stop or a guaranteed floor.

Each new position binds the exact policy and revision inside its entry
transaction. Stages advance only with successful fills; stage, cash, basis and
journal changes roll back together. One target can fill per fresh quote timestamp.
A reused quote cannot take successive targets. Uncovered, stale, wrong-pool or
missing quotes retain holdings, with no fictional sale or alternative pool.

Activation adds an `exit_policy_revision` event and a position-binding table.
Existing holdings retain the original stop, one 50% take at +100%, trail and time
exit, including any already completed partial take. Original entry policies,
cash, holdings, monetary risk, costs, previous events and the trial deadline are
preserved. Exit policy is separate from monetary `RiskPolicy` and the active A/B
entry selector. Existing baseline/A/B comparison portfolios retain their original
hypotheses. Xenon, Neon, Argon, Krypton and all paid/live gates are unaffected.

Bound exits read each position's stored policy independently of later entry
configuration changes. Missing or corrupted bindings fail explicitly; they never
silently restore default exits. The console shows the new-entry ladder and the
original-exit label on inherited positions. No worker cadence changes are made.

## Historical evidence and limits

The October 30 review used a consistent snapshot of 30 actual entries: 26 closed
and four unresolved. Replaying the original rules reconciled all recorded P&L to
Decimal tolerance. Four closed losers had recorded gains of roughly 47–69% before
reversing; thirteen losing closed trades never reached +40% in recorded marks.
Exit selection alone therefore does not explain all losses.

The four unresolved holdings also retained $17.25 of remaining entry basis plus
about $0.25 of exit reserves. They consumed roughly $17.50 of the unchanged $20
aggregate full-loss risk allowance. Recent entries were consequently around
$2.45, versus AI's $4.92 entry cost. Smaller dollar wins partly reflect smaller
positions. This review does not increase that risk allowance or erase unresolved
holdings to free capacity.

AI earned $16.46 across its partial and final exits. The initially proposed full
40/100/180 ladder with 50/25/25 allocation would have produced about $5.01 on its
recorded quotes. A fixed 20% trail would also have exited AI much earlier. The
runner ladder with the unchanged 30% trail replayed AI at about $23.11 (+40.4%).

Among 25 fully covered completed trades, the proposed runner policy with early
protection replayed at $25.49 versus $18.67 actual, a $6.82 difference. Almost all
of that net difference came from AI. SIC's alternative larger holding lacked
sufficient recorded quote coverage; it is excluded. All four unresolved holdings
are excluded from that paired total, with their quantities and basis retained.

This is a fixed-entry, **in-sample** replay selected after examining the data. It
does not reconstruct a continuous price path, trade at an unknowable top, change
later entry timing, model a full alternative portfolio, or establish future
profitability. Original provider gaps and monitoring interruptions remain.
Private snapshot and complete tested alternatives are retained under source
`runtime/radeon-exit-review`, excluded from Git and all strategy performance.

The reusable offline checker runs the actual new exit implementation in isolated
in-memory ledgers and reconciles the legacy replay first:

```powershell
python scripts/review_radeon_exits.py --snapshot REVIEW/snapshot.json --output REVIEW/engine-replay.json
```

`revise_radeon_exits.py` prints the proposed policy without mutation by default.
Its explicit `--activate` path requires the exact current account/source/entry
revision and a reviewed evidence file, takes controller and worker locks, verifies
the journal identity and records the evidence hash. It does not run a trade cycle.
Repeated identical activation can recover the trial JSON after a committed ledger
revision; a conflicting revision requires a separate reviewed migration.
