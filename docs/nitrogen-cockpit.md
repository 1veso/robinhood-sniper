# NITROGEN cockpit

Open the existing console at `/?bot=nitrogen`. NITROGEN stays in slot 7; RADON
stays in slot 8. This view is read-only and does not initialize an account or enable
models, workers, wallet access or live execution. The execution label comes from the
account projection; incoming market observations never switch it to LIVE.

## Arrangement and controls

The supplied 600 × 1068 video was inspected at 0, 6, 10, 16 and 21 seconds. The
cockpit retains its slim HEAD/session line, identity header, clipped ticker, oversized
equity amount with baseline-aligned cents, minimal chart, six colored role rows,
five-line decision log and two-column rejection card. Device/player chrome is omitted.
At 600 pixels, side gutters are 27 pixels, cards 546 pixels wide, desk height 321
pixels, decision log 171 pixels and rejection card 117 pixels. The central column
stays 600 pixels wide on desktop; phone rows use readable minimum sizes.

- HEAD/status opens worker freshness, blockers, coverage, policy and reports.
- Equity opens cash, costs, reserves, P&L and unresolved holdings.
- SEARCH/RISK/WHALE open recorded evidence and candidate dossiers.
- SNIPER opens pending/recorded orders, expiry, size and supported commands:
  `python nitrogen_bot.py approve --id <order-id>` and
  `python nitrogen_bot.py reject --id <order-id>`. No decorative approval control
  submits a transaction; the existing CLI performs the actual approval checks.
- EXIT opens positions and recorded fills. JUDGE opens all four answers, P(buy),
  the separate confidence statistic, raw/displayed exit score, distributions,
  timestamped dossiers and WATCH rechecks.
- Decision log opens cursor-paged history. Killed by rule opens the full category list.
- The compact N₂ selector preserves access to all bots when the sidebar is hidden.

The role order is visual. Actual admission remains RISK → WHALE → JUDGE → staged
order → explicit approval. All sizing, thresholds, questions and exit rules are unchanged.

## Data bindings

`GET /api/state?bot=nitrogen` returns the existing account plus the account-bound
`cockpit` projection. `GET /api/nitrogen-journal?after=<account-id>:<sequence>`
recovers new events; `before=<sequence>` pages older history.

| Display | Recorded source / accounting meaning |
|---|---|
| Mode, policy, bankroll | Existing NITROGEN `meta`, bound config and `summary` |
| Equity, net P&L | Existing snapshot, including model reserves and retained rent; unknown prices or bills retain unknown values |
| Multiplier | Current reported equity / starting paper capital; not buying turnover |
| Wins/losses, best/worst | Closed trade records; net trading P&L after modeled fills, excluding model invoices |
| Ticker | Recorded position/closed-position metadata and marks; factual dossier prices with comparable one-hour snapshots where present |
| Six role rows | Latest durable decision per responsibility, separate pending order count and current open positions |
| Role counters | SEARCH leads, RISK rejections, WHALE confirmations, recorded fills/exits and actual completed JUDGE vetoes |
| Log and toasts | Durable `decisions.id`, account identity, corresponding fill ID and order; no generated dialogue |
| Rejection bars | Distinct candidate/rule pairs over account lifetime; cumulative, not just the visible journal page |
| HEAD and worker state | Recorded worker starts/completions, error/paused state and configured cadence |

Worker `publish()` now also records exact account observations in
`nitrogen_bot/runtime/cockpit.sqlite3`, a presentation-only sidecar with its own account
identity. Errors writing that sidecar cannot change the strategy ledger, approvals or
exits. Console reads never append observations. The chart displays the latest 1,500
recorded points at actual timestamps; missing valuations and intervals exceeding
three worker cadences break the line. It uses straight segments, not smoothing or
reconstructed historical balances. DPR-aware Canvas and ResizeObserver handle resizing.

Notifications are limited to new recorded staging, fill, rejection, expiry and exit
events. Initial load, reconnect and bot switching establish a baseline without replaying
history. Account identity and sequence bind catch-up requests; switching cancels requests,
animations and timers. No clocks manufacture trades or prices. Reduced-motion preferences
disable ticker and event animation. Numerical changes fade directly to recorded values.

## Unavailable fields and operational state

- Existing accounts have no retrospective equity observations before this feature.
- Token images are not stored. The ticker uses initial fallbacks. Symbols absent from
  recorded position metadata use abbreviated mint addresses; full mint identity and
  observation time are available in token details. No external image fetches are added.
- Not every token has comparable hourly prices. Missing changes display a dash; stale
  observations retain their timestamps. Pending/rejected tokens may have only a mint.
- Unavailable source observations are not represented as zero rejections or invented fills.
- The real account remains uninitialized in this development workspace. Configured tracked
  wallets, authorized factual enrichment, model credentials/authorization and budget are
  still needed for operational admission. This UI does not resolve those provider gaps.

## Reproducible visual acceptance

```sh
PYTHONPATH=src:. python -m unittest discover -s tests -p 'test_nitrogen*.py'
python tests/verify_nitrogen_console.py
python run_tests.py
```

The browser check requires Playwright and Chromium plus loopback sockets. It creates a
separate temporary synthetic account and drives the real paper ledger through recorded
approvals, marks, partial takes, exits, WATCH and rejections. Every preview is labeled
**SYNTHETIC PREVIEW**. It makes no provider/model calls and never uses a strategy account.

Screenshots and machine-readable results are written to the ignored local directory
`runtime/nitrogen-cockpit-preview/`:

- `reference-600.png`: reference composition at 600 × 1068 (full content).
- `mobile-390.png`: 390 × 844 phone view; `mobile-pending.png`: approval details.
- `desktop-1440.png`: 1440 × 900; `desktop-full.png`: full scrollable column.
- `desktop-judge.png`: complete Jev details beside the desktop cockpit.
- `notification-600.png`: a new durable JUDGE rejection overlay.
- `empty-600.png`, `stale-unknown-600.png`, `negative-600.png`: honest edge states.

The check also verifies long symbols/large negative numerical widths, no horizontal
page overflow, uncropped role pills, reduced motion, no historical/duplicate/reconnect
toasts, account switching, slot preservation and rejected POST writes.

## Files changed for this UI

- `console/nitrogen.js`, `console/nitrogen.css`: cockpit, Canvas, drawers and event motion.
- `console/app.js`: activation, disconnection and request cancellation on switching.
- `src/sniper/nitrogen/cockpit.py`: read-only account projection/journal and optional equity sidecar.
- `src/sniper/nitrogen/book.py`: presentation-only publication hook.
- `src/sniper/nitrogen/view.py`, `src/sniper/console.py`: projection and read-only journal route.
- `tests/test_nitrogen_cockpit.py`, `tests/verify_nitrogen_console.py`: projection/browser acceptance.

Shared Jev files, NITROGEN's configuration/gate/sizing/approval/exit code and all RADON
strategy files are unchanged by the cockpit work.
