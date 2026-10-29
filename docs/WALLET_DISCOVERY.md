# Automatic wallet discovery and address evidence

The existing **SNIPER Paper Research** Windows task discovers candidate public
addresses and refreshes their evidence exports automatically. It does not need a
manually supplied watchlist. A discovered address is a research lead, not a
profitable wallet or a verified independent controller.

The research child has a 240-second process limit, including local publication.
Its provider budgets remain 45 seconds per pass and 1,000 Blockscout requests per
UTC day. The October 28 supervision repair replaces a 75-second child limit that
interrupted exports as the evidence journal grew. `export_seconds`, `finished_at`
and `duration_seconds` in `wallet-research.json` record completed publication;
`updated_at` and individual canonical-check times retain their original scope.
A child timeout is reported as `TIMEOUT`, not a successful research refresh.
The minute trigger skips overlapping runs; it is not a guarantee of one completed
research pass each minute. Existing seven-minute Windows task limits are retained;
new desktop installations allow five minutes around the bounded child.

Run commands below from the installed `sniper-v0.1.0` folder. The existing RPC and
Blockscout configuration is reused. No private key, seed phrase, funded wallet or
brokerage API key is required for discovery or these exports.

## Two observation sources

**Current funded-buy observations.** The research worker imports proofs already
recorded by the exploratory paper collector in
`runtime/exploration/events.sqlite3`. A proof must join its canonical source swap
and include the existing funded-acquisition and configured-factory checks. The
import processes up to 500 confirmation records per cycle, oldest first, and
persists its progress. A later cycle resumes the remaining records instead of
re-reading only a moving window of the newest observations.

**Independent swap-sender discovery.** The research worker also reads the V3 Swap
topic across contiguous historical block intervals. The normal bounded pass
samples up to three successful transactions from an interval of up to 2,000
blocks. Its 12-call discovery budget covers the log request and the sampled
transactions' transaction, receipt and canonical-block-header checks; additional
anchor checks establish the interval boundary. Dense log queries may reduce the
interval or exhaust the pass's budget. Actual completed sample counts, block
coverage and lag are recorded rather than inferred from the configured maximum.

This independent cursor is retained across runs. It can remain behind the chain
head; a small historical pass is not a full-chain live census. Current funded-buy
imports and historical sender discovery therefore report different coverage.
The worker does not skip the historical backlog or reset its cursor merely to
display a smaller lag.

Neither source measures wallet profitability by activity count. An observed
transaction can contain routing contracts or relayers, and a large swap does not
establish who owned its funding.

## Address roles and proof scope

Address-level evidence distinguishes roles when the source supplies them:

| Role | What the observation establishes |
|---|---|
| `transaction_sender` | The transaction's `from` address; it may be a relayer or router. |
| `funded_recipient` | Receipt transfers corroborate a funded pool buy and net token acquisition under the existing verifier's scope. |
| `base_payer` | A transfer into the pool names this address as a payer; it does not by itself prove the recipient owns that address or the funds. |

The raw swap event's recipient remains in its archived proof. It is not exported
as a fourth normalized role or assumed to be the final acquiring recipient.

Direct-wallet expenditure and recipient acquisition retain their different proof
scopes. Missing role information is not guessed. Shared routers or payment paths
do not automatically turn separate addresses into a verified common controller,
and absence of a link does not prove independence.

Evidence retains chain, source-journal identity, transaction, log index, block
identity, pool, token, observation time and its verification scope. Proof digests
and archived payload references make the address summary traceable to its source.
Successful imports are deduplicated across repeated runs and process restarts.

The source UUID is bound to the importer. Replacing a journal with a different
database is not treated as a continuation of the original source. Reorged
observations remain in the evidence history with their canonical status recorded;
they must not contribute as current canonical evidence merely because the
address was discovered earlier. Local export refreshes use the collector's
recorded canonical status, not a new independent RPC check of every old proof.

## Growing the research universe

The former 5,000-candidate lifetime limit could permanently block both historical
sender discovery and the oldest-first funded-recipient import. The October 29
capacity repair removes that lifetime limit; it does not evict old wallets,
delete histories, skip queued source records or grant qualification.

Each confirmation import remains limited to 500 source records. Discovery keeps
its existing sample and RPC budgets, and history reconstruction retains the
1,000-request UTC-day Blockscout limit, frozen snapshot, transaction checkpoints
and three-batch rotation. `capacity_blocked` is retained as a compatibility field
and is false after a successful import; `candidate_limit: null` means there is no
lifetime count cap, while `import_limit` reports the bounded batch size.

Exports use eight file writers with at most 64 queued addresses. They preserve
the same evidence bytes, proof/qualification timestamps, fsync, atomic file
replacement and index-after-success publication. The 240-second research child
limit and scheduled task definitions are unchanged. Export cost still grows with
preserved evidence: inspect `export_seconds`, actual completion timestamps and
explicit timeout reports. More discovered candidates do not mean more funded
history requests or qualified traders. The console's separately disclosed
10,000-row display limit is not a research-admission limit; full CSV/JSONL exports
retain the recorded evidence.

## Files to inspect or export

All paths below are relative to the installed project.

| Path | Purpose |
|---|---|
| `runtime/research/candidate-evidence.sqlite3` | Durable address/evidence index, source bindings and import progress. |
| `runtime/research/addresses/0xADDRESS.json` | Evidence and research status for one normalized public address. Replace `0xADDRESS` with the actual address. |
| `runtime/research/addresses/0xADDRESS.csv` | Tabular observations for that address; the JSON file also contains its history and qualification assessment. |
| `runtime/research/wallet-evidence.csv` | Tabular evidence export for review and analysis. |
| `runtime/research/wallet-evidence.jsonl` | Machine-readable evidence records, one JSON object per line. |
| `runtime/research/wallet-evidence-index.json` | Export index and coverage information. |
| `runtime/research/wallet-candidates.csv` | Candidate-address summary; discovery frequency is not a performance rank. |
| `runtime/research/wallet-research.json` | Current history progress, provider budgets, qualification counts and pending work. |
| `runtime/research/cohort-BLOCK/history-ADDRESS.json` | Reconstructed history for the frozen cohort, when available. |
| `runtime/research/cohort-BLOCK/wallet-ADDRESS.json` | Historical performance assessment and gaps, when available. |
| `runtime/research/wallet-shortlist.json` | Performance-qualified candidates, still subject to controller evidence. |
| `runtime/research/qualified-wallets.json` | Candidates satisfying the existing history and current controller-review rules. |

Older discovery records may contain only a transaction or pool example. Those
remain **legacy references** until their supporting evidence is available; they
are not upgraded into verified trading histories by migration or export. Missing
P&L is represented as `null` in JSON or an empty CSV value, not a fabricated zero
or a profitable result. The address exports are research records, separate from
the strategy's paper-trade ledger and P&L.

To refresh local observations and exports without network requests:

```powershell
python research.py --export-only
```

This imports available local proofs and updates research/export files. It does
not query RPC, Blockscout or paid AI, and it cannot establish history that has not
already been collected. If a research pass already owns the process lock, allow
it to finish before running another pass.

For one bounded discovery/history pass using the configured providers:

```powershell
python research.py
```

Ordinary operation already invokes that work through **SNIPER Paper Research**;
no second recurring task is needed. Check its actual last completion and the
research report timestamp rather than treating an enabled schedule as proof that
data is current.

## Qualification and operating limits

History analysis keeps its existing frozen 30-day cohort, receipt-level SQLite
checkpoints and three-batch queue rotation. Partial work survives process exits;
a cycle timeout does not make an incomplete history complete. No daily reset
discards an unfinished cohort. Provider failures, unknown cost basis, unsupported
routes, missing prices and incomplete controller evidence remain visible gaps.

The local Blockscout budget stays at **1,000 requests per UTC day**. When it is
exhausted, history work waits for the next UTC day; local imports and exports can
continue. Cache hits reuse evidence without spending another request. Discovery
does not enable a paid plan or paid Grok/X calls.

The existing cost-aware performance screen and controller-review requirements
still determine qualification. An observation count, proof export, address role
or positive-looking partial history cannot set `qualified=true`. No API key or
export can guarantee profitable wallets or a profitable strategy.

Discovery does not change the active paper account, its cash or positions, the
two-recipient/$30,000 entry revision, safety/route checks, exits, risk limits,
policy history or original seven-day deadline. The strict five-reviewed-group
policy remains a separate reference; this work does not reapply that gate to the
active exploratory account. Real-money execution remains disabled.
