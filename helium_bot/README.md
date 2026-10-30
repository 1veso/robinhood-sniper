# Helium — standalone trading team

Helium is Bot 6: an independent **Solana paper trader** with its own internal
research and decision team. It does not supervise or trade through Radeon,
Xenon, Neon, Argon or Krypton. Its account is
`helium_bot/runtime/book.sqlite3`; existing accounts remain separate.

## Selected baseline

The trading configuration is copied unchanged from Krypton's existing
`krypton_new_token_v1` policy. Keeping the original policy version identifies
the inherited trading rules; Helium's separate team policy, account identity
and evidence records identify the additional decision process.

| Setting | Inherited value |
| --- | --- |
| Mode / virtual starting cash | Paper / $1,000 |
| Minimum pool liquidity / age | $25,000 / 10 minutes |
| Maximum largest owner / top-ten concentration | 10% / 40% |
| Mint and freeze authorities | Both revoked |
| LP evidence | Pinned-pool provider report of 100% locked or burned |
| Grok analyst | `grok-4.7`, risk at most 4/10, no missing required facts |
| Maximum position cash commitment | 2% of conservative equity, including fees and reserved rent |
| Stop / full take profit | −25% / +50% |
| Daily loss halt | 5%, using the Europe/Berlin account day |
| Scan / independent position watch | Every 60 seconds |
| Quote / safety evidence age | At most 60 / 300 seconds |
| Candidate expiry / candidates per scan | 1,800 seconds / 1 |

These are inherited experiment settings, not optimized parameters or a promise
of returns. A provider report and a model's agreement do not strengthen the
underlying evidence into an independent safety guarantee.

## What the internal team adds

Discovery and integrity roles reuse the existing providers and deterministic
checks. The Grok analyst evaluates the supplied facts with the existing cited
risk schema. A separate Jev reviewer evaluates whether the supplied assessment
is supported by those facts and returns a constrained decision. Persistent
handoffs bind the candidate, account, policy, evidence, role and outcome.

The final paper transaction requires the current bound team approval in addition
to all baseline safety, freshness, sizing, cash and loss limits. Research cannot
override those checks. Position exits use the original policy and continue
without Grok or Jev.

The pinned reviewer is `jev-1.14.0`. Its `accept` probability and evidence-support
probability must each reach 0.70, reusing the existing Jev decision threshold.
Otherwise it returns `WATCH` or `SKIP`, both meaning no entry. In this version,
`WATCH` is terminal for that candidate under the inherited seen-once policy;
it does not schedule Argon's 30-minute refresh, which would collide with the
baseline's 30-minute candidate expiry. These scores are not profit probabilities.

Roles are responsibilities in one trading pipeline; they are not a requirement
to make a separate model call for every role. Model requests are bounded and
account-attributed, with at most one attempt per provider for a candidate in
this version. An ambiguous paid request is not automatically replayed.

## Run locally

From the application folder, using the existing application Python:

```powershell
python helium_bot.py init
python helium_bot.py status
python helium_bot.py --demo
python helium_bot.py scan
python helium_bot.py watch
```

`init` is explicit and preserves an existing account. It creates disabled
`paid-ai.json` and `paid-jev.json` settings only when they are absent. It never
initializes another bot, copies its funds, resets a balance or enables a provider.
`status` is read-only. `--demo` uses isolated synthetic evidence, excluded from
all strategy results. `--mode live` is rejected.

For independent Windows background workers after initialization:

```powershell
.\scripts\desktop-jobs.ps1 -AppRoot (Get-Location).Path -Python (Get-Command python).Source -HeliumOnly
```

This targets only `HELIUM Paper Scan` and `HELIUM Paper Watch`. Both run every
minute with separate locks. Normal desktop setup only adds these jobs when
Helium's own account has already been initialized. Existing task definitions
are preserved. The computer must remain awake and online.

`python helium_bot.py run` is a convenience foreground loop. The separate
scheduled workers are preferred because a slow scan does not occupy the watch
process. Watch workers read the account's bound original policy even if its
editable configuration changes.

## Model authorization and interpretation

The initial installation leaves both paid providers disabled. Required model
authorization and validation must be specific to Helium; another bot's account
approval is insufficient. Shared provider credentials or budget mechanisms do
not constitute permission to spend. Grok replies retain the baseline requirement
for verified billed usage. Jev's token-rate estimate or retained request
reservation reduces conservative paper equity, but is not presented as an
invoice. Complete net performance remains unknown while any bill is unresolved.
A stopped model does not disable exits.

An initialized account can therefore correctly show no entries while model
access or evidence is unavailable. A synthetic demo proves implementation
behavior, not market profitability. Paper fills, model costs and outcomes belong
to Helium's account and are not added to another bot's results.

See [operations](../docs/HELIUM_BOT.md) for worker and packaging details.
