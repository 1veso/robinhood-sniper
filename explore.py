"""Restartable seven-day exploratory PAPER trial on live Robinhood observations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from supervise import atomic_json, instance_lock
from sniper.adapters.evm import RpcClient, DEFAULT_RPC, CHAIN_ID, RpcError, ReorgError, RpcBudgetExceeded
from sniper.adapters.evm_store import EvmStore
from sniper.adapters.goplus import GoPlusClient
from sniper.adapters.crius import supplement_from_runtime
from sniper.adapters.solana import DexScreenerClient, SourceError, RequestBudgetExceeded, request_budget
from sniper.adapters.v3_route import V3RouteClient, RouteUnavailable
from sniper.exploration import ExploratoryEngine, exploratory_policy, pool_adjusted_screen, observed_pool_screen, observe_creator_balance
from sniper.exploratory_source import ExploratorySource, signal_id, COLLECTION_REVISION
from sniper.exploratory_alerts import notify_fills
from sniper.market_alerts import record_market_signal, notify_market
from sniper.confirmation_book import ConfirmationBook
from sniper.rug_screen import enhance_rug_screen
from sniper.ledger import Ledger
from sniper.ledger import json_text
from sniper.models import Candidate, SafetyEvidence, WalletConfirmation, json_safe
from sniper.policy import RiskPolicy
from sniper.reporting import write_html
from sniper.paper_comparison import Comparison
from sniper.entry_strategies import entry_rules, rules_hash, load_wallet_registry, evaluate_signals
from sniper.parallel_entry import (PROFILE as PARALLEL_PROFILE, ParallelEntryEngine,
                                  qualified_signals, signal_proofs, record_signals)

RULES = {'strategy': ExploratoryEngine.strategy_name, 'mode': ExploratoryEngine.engine_mode,
         'universe': 'Dynamic hash sample of funded buys and net recipient acquisitions in registered Robinhood V3 pools, including established pools',
         'minimum_buy_size_basis': 'observed pool input; recipient ownership of funding may be unestablished',
         'wallet_qualification': 'unestablished', 'wallet_independence': 'unestablished',
         'unknown_safety': 'recorded, permitted for this paper experiment', 'failed_safety': 'rejected',
         'source_sample': 32, 'max_signal_age_seconds': 900, 'duration_seconds': 7 * 86400,
         'real_money_enabled': False, 'strict_strategy_result': False}

RULES_V2 = {**RULES, 'revision': 'two-wallet-30k', 'minimum_observed_wallets': 2,
            'minimum_liquidity_usd': '30000', 'rug_screen': 'required provider response; known failures reject',
            'candidate_pool_followup': 'up to eight recently observed pools; up to 16 hash-sampled swap legs per pool per cycle',
            'quorum_scope': 'same token and pool; distinct recipients and transactions in 900 seconds; independence unestablished'}

RULES_V3 = {**RULES_V2, 'revision': 'two-wallet-30k-reentry',
            'position_count_cap': None, 'daily_entry_count_cap': None,
            'reentry': 'Only after full close; two distinct buying recipients in different transactions observed strictly after the close. Fresh current safety, route, cash and risk checks remain required.'}

RULES_PARALLEL = {**RULES_V3, 'revision': PARALLEL_PROFILE,
    'minimum_observed_wallets': None,
    'wallet_qualification': 'Per strategy, explicitly pinned in trial.entry_rules',
    'reentry': 'Strategy A uses its explicitly pinned rule; Strategy B requires two fresh distinct buyers after full close',
    'entry_paths': 'Independent whale_follow OR two_wallet_v3; exact eligibility/re-entry rules are pinned in trial.entry_rules',
    'quorum_scope': 'Strategy B aggregates distinct recipients and transactions by chain and token across eligible V3 pools within 900 seconds',
    'execution_scope': 'The triggering pool is selected and screened separately; its liquidity and size-bounded route never borrow evidence from another pool',
    'attribution': 'One shared token position; record both qualifying signals and the initiating strategy; no independent portfolio-return claim'}


def profile_settings(profile):
    if profile == 'legacy':
        return exploratory_policy(RiskPolicy.from_file(ROOT / 'config' / 'paper.toml')), RULES, 'exploration'
    if profile == 'two-wallet-30k':
        return RiskPolicy.from_file(ROOT / 'config' / 'exploration-v2.toml'), RULES_V2, 'exploration'
    if profile == 'two-wallet-30k-reentry':
        return RiskPolicy.from_file(ROOT / 'config' / 'exploration-v3.toml'), RULES_V3, 'exploration'
    if profile == PARALLEL_PROFILE:
        return RiskPolicy.from_file(ROOT / 'config' / 'exploration-v3.toml'), RULES_PARALLEL, 'exploration'
    raise ValueError('Unknown paper profile')


def active_profile(runtime):
    path = runtime / 'active-exploration.json'
    if not path.exists():
        # A missing selector must not restart the retired strategy.
        if (runtime / 'exploration' / 'retired.json').exists() or (runtime / 'exploration-v2' / 'trial.json').exists():
            raise ValueError('Active profile selector missing; restore it before restarting')
        manifest_path = runtime / 'exploration' / 'trial.json'
        if manifest_path.exists() and json.loads(manifest_path.read_text(encoding='utf-8')).get('rules', {}).get('revision') in ('two-wallet-30k', 'two-wallet-30k-reentry', PARALLEL_PROFILE):
            raise ValueError('Active profile selector missing; rerun the authorized activation to recover it')
        return 'legacy'
    value = json.loads(path.read_text(encoding='utf-8'))
    profile = value.get('profile')
    _, _, name = profile_settings(profile)
    manifest = json.loads((runtime / name / 'trial.json').read_text(encoding='utf-8'))
    if value.get('account_id') != manifest.get('account_id'):
        raise ValueError('Active profile identity mismatch')
    if profile == PARALLEL_PROFILE and (value.get('entry_revision') != manifest.get('entry_revision')
            or manifest.get('entry_revision') != rules_hash(manifest.get('entry_rules'))):
        raise ValueError('Active entry revision mismatch')
    return profile


def activate_v2(runtime):
    """Journal an explicit ENTRY-only revision without resetting cash or exits.

    SQLite commits the policy and manifest together. A crash before the atomic
    JSON/selector writes is recoverable by repeating this explicit activation.
    """
    policy, rules, name = profile_settings('two-wallet-30k')
    folder = runtime / name
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / 'trial.json').exists():
        ledger, store, _, manifest = open_trial(folder, policy, start=True, rules=rules)
        ledger.close(); store.close()
    if not (folder / 'events.sqlite3').is_file():
        raise ValueError('Established source journal missing')
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=True)
    store = EvmStore(folder / 'events.sqlite3')
    try:
        manifest = json.loads(ledger._get('trial_manifest'))
        disk_manifest = json.loads((folder / 'trial.json').read_text(encoding='utf-8'))
        revision_id = 'policy-revision:two-wallet-30k'
        previous = ledger.conn.execute('SELECT payload FROM events WHERE event_id=?', (revision_id,)).fetchone()
        previous_manifest = json.loads(previous['payload'])['previous_trial'] if previous else None
        if disk_manifest != manifest and disk_manifest != previous_manifest:
            raise ValueError('Trial manifest changed outside the recorded activation')
        if manifest['account_id'] != ledger._get('account_id') or manifest['source_id'] != store.identity:
            raise ValueError('Account/source identity changed')
        if manifest['rules'] != rules:
            if manifest['rules'] != RULES:
                raise ValueError('Unrecognized prior rules; revision requires review')
            before = json.loads(ledger._get('policy'))
            after = policy.as_dict()
            differences = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            if differences - {'min_liquidity_usd', 'min_independent_wallets', 'required_safety_checks'}:
                raise ValueError('This revision cannot change capital, risk limits, costs or existing exits')
            if after['required_safety_checks'] != before['required_safety_checks'] + ['rug_screen_available']:
                raise ValueError('This revision must retain existing safety checks')
            now = int(time.time())
            updated = {**manifest, 'rules': rules, 'policy_version': policy.version, 'revision_started_at': now}
            atomic_json(folder / 'trial-before-v2.json', manifest)
            with ledger.transaction():
                ledger.record_event(revision_id, 'policy_revision', {'previous_trial': manifest, 'new_trial': updated,
                    'previous_policy': before, 'new_policy': after,
                    'reason': 'User requested $30000 liquidity, two buying wallets and rug screening; cash and open holdings preserved'}, now)
                ledger._set('policy_version', policy.version)
                ledger._set('policy', json_text(after))
                ledger._set('trial_manifest', json.dumps(updated, sort_keys=True))
            manifest = updated
        ExploratoryEngine(ledger, policy)
        atomic_json(folder / 'trial.json', manifest)
        selector = {'profile': 'two-wallet-30k', 'account_id': manifest['account_id'],
                    'activated_at': manifest.get('revision_started_at', manifest['started_at']), 'previous_profile': 'legacy'}
        atomic_json(runtime / 'active-exploration.json', selector)
    finally:
        ledger.close()
        store.close()
    return selector


def activate_reentry(runtime):
    """Explicit entry-only revision of the existing v2 account, crash-recoverable.

    Caller holds exploration-controller.lock. Never creates an account or changes
    cash, source identity, old fills, risk amounts, costs, exits or the deadline.
    """
    policy, rules, name = profile_settings('two-wallet-30k-reentry')
    folder = runtime / name
    if any(not (folder / f).is_file() for f in ('trial.json', 'ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('This revision requires the established paper account')
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=True)
    store = EvmStore(folder / 'events.sqlite3')
    try:
        manifest = json.loads(ledger._get('trial_manifest'))
        disk = json.loads((folder / 'trial.json').read_text(encoding='utf-8'))
        revision_id = 'policy-revision:two-wallet-30k-reentry'
        previous = ledger.conn.execute('SELECT payload FROM events WHERE event_id=?', (revision_id,)).fetchone()
        old_manifest = json.loads(previous['payload'])['previous_trial'] if previous else None
        if disk != manifest and disk != old_manifest:
            raise ValueError('Unrecorded trial manifest change')
        if manifest['account_id'] != ledger._get('account_id') or manifest['source_id'] != store.identity:
            raise ValueError('Account/source identity changed')
        if manifest['rules'] != rules:
            if manifest['rules'] != RULES_V2 or previous:
                raise ValueError('Re-entry revision requires the existing two-wallet policy')
            before, after = json.loads(ledger._get('policy')), policy.as_dict()
            changes = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            allowed = {'max_open_positions', 'max_entries_per_day', 'cooldown_seconds'}
            if changes != allowed or any(after[k] != 0 for k in allowed):
                raise ValueError('Revision cannot change monetary risk, costs, safety or existing exits')
            now = int(time.time())
            updated = {**manifest, 'rules': rules, 'policy_version': policy.version, 'revision_started_at': now}
            atomic_json(folder / 'trial-before-reentry.json', manifest)
            with ledger.transaction():
                ledger.record_event(revision_id, 'policy_revision', {'previous_trial': manifest, 'new_trial': updated,
                    'previous_policy': before, 'new_policy': after,
                    'reason': 'User authorized removing position/daily-entry count caps and fresh-condition re-entry; monetary risk, exits, account and deadline preserved'}, now)
                ledger._set('policy_version', policy.version)
                ledger._set('policy', json_text(after))
                ledger._set('trial_manifest', json.dumps(updated, sort_keys=True))
            manifest = updated
        ExploratoryEngine(ledger, policy)
        atomic_json(folder / 'trial.json', manifest)
        selector = {'profile': 'two-wallet-30k-reentry', 'account_id': manifest['account_id'],
                    'activated_at': manifest['revision_started_at'], 'previous_profile': 'two-wallet-30k'}
        atomic_json(runtime / 'active-exploration.json', selector)
        return selector
    finally:
        ledger.close(); store.close()


def activate_parallel(runtime, *, whale_eligibility, whale_reentry, now=None):
    """Explicit entry revision, preserving the original account and monetary policy.

    Both strategy-A choices are required at activation. Repeating the same
    activation recovers JSON publication after a committed journal transaction.
    Caller holds the controller/worker locks; no account is created here.
    """
    policy, rules, name = profile_settings(PARALLEL_PROFILE)
    entries = entry_rules(whale_eligibility=whale_eligibility, whale_reentry=whale_reentry)
    if now is not None and (type(now) is not int or now < 0):
        raise ValueError('Activation time must be a nonnegative integer')
    revision = rules_hash(entries)
    folder = runtime / name
    if any(not (folder / f).is_file() for f in ('trial.json', 'ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('Independent entry activation requires the established paper account')
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=True)
    store = EvmStore(folder / 'events.sqlite3')
    try:
        manifest = json.loads(ledger._get('trial_manifest'))
        disk = json.loads((folder / 'trial.json').read_text(encoding='utf-8'))
        revision_id = 'policy-revision:' + PARALLEL_PROFILE + ':' + revision
        prior = ledger.conn.execute('SELECT payload FROM events WHERE event_id=?', (revision_id,)).fetchone()
        previous_trial = json.loads(prior['payload'])['previous_trial'] if prior else None
        if disk != manifest and disk != previous_trial:
            raise ValueError('Unrecorded trial manifest change')
        if manifest['account_id'] != ledger._get('account_id') or manifest['source_id'] != store.identity:
            raise ValueError('Account/source identity changed')
        if manifest.get('policy_version') != policy.version:
            raise ValueError('Trial monetary policy differs from the recorded account')
        selector_path = runtime / 'active-exploration.json'
        if selector_path.exists():
            selector = json.loads(selector_path.read_text(encoding='utf-8'))
            if (selector.get('account_id') != manifest['account_id']
                    or selector.get('profile') not in ('two-wallet-30k-reentry', PARALLEL_PROFILE)
                    or (selector.get('profile') == PARALLEL_PROFILE and selector.get('entry_revision') != revision)):
                raise ValueError('Active selector does not match the reviewed account/revision')
        if ledger._get('policy_version') != policy.version or ledger._get('policy') != json_text(policy.as_dict()):
            raise ValueError('Independent entry revision cannot alter the monetary risk, costs or exits')
        if manifest.get('entry_revision') != revision:
            if manifest['rules'] != RULES_V3 or prior or ledger._get('entry_revision'):
                raise ValueError('Independent entry activation requires the existing v3 account; later revisions need review')
            at = int(time.time()) if now is None else now
            updated = {**manifest, 'rules': rules, 'revision_started_at': at,
                       'entry_rules': entries, 'entry_revision': revision, 'entry_revision_started_at': at}
            atomic_json(folder / 'trial-before-parallel.json', manifest)
            with ledger.transaction():
                ledger.record_event(revision_id, 'policy_revision', {
                    'previous_trial': manifest, 'new_trial': updated,
                    'previous_policy': policy.as_dict(), 'new_policy': policy.as_dict(),
                    'entry_revision': revision, 'entry_rules': entries,
                    'reason': 'User authorized independent whale-follow and cross-pool token V3 entry paths; same cash, holdings, monetary risk, exits, source and deadline'}, at)
                ledger._set('entry_revision', revision)
                ledger._set('entry_rules', json_text(entries))
                ledger._set('trial_manifest', json.dumps(updated, sort_keys=True))
            manifest = updated
        if manifest.get('entry_rules') != entries or manifest['rules'] != rules:
            raise ValueError('Independent entry activation binding mismatch')
        ParallelEntryEngine(ledger, policy, entry_rules=entries, runtime=runtime, store=store)
        atomic_json(folder / 'trial.json', manifest)
        selector = {'profile': PARALLEL_PROFILE, 'account_id': manifest['account_id'],
                    'activated_at': manifest['entry_revision_started_at'], 'entry_revision': revision,
                    'previous_profile': 'two-wallet-30k-reentry'}
        atomic_json(runtime / 'active-exploration.json', selector)
        return selector
    finally:
        ledger.close(); store.close()


def open_trial(folder, policy, *, start=False, now=None, rules=None):
    rules = RULES if rules is None else rules
    now = int(time.time()) if now is None else now
    manifest = folder / 'trial.json'
    if not manifest.exists() and not start:
        raise ValueError('Trial is not initialized; run explore.py --start once')
    prior = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else None
    if rules.get('revision') == PARALLEL_PROFILE and prior is None:
        raise ValueError('Independent entry rules require explicit activation of the existing account')
    if prior and any(not (folder / f).is_file() for f in ('ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('Established trial database missing; restore original state')
    if not prior and any((folder / f).exists() for f in ('ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('Trial manifest missing beside existing databases; restore original manifest')
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=bool(prior))
    store = None
    try:
        store = EvmStore(folder / 'events.sqlite3')
        engine = (ParallelEntryEngine(ledger, policy, entry_rules=prior['entry_rules'], runtime=folder.parent, store=store)
                  if rules.get('revision') == PARALLEL_PROFILE else ExploratoryEngine(ledger, policy))
        if prior:
            if (prior.get('account_id') != ledger._get('account_id') or prior.get('source_id') != store.identity or
                    prior.get('rules') != rules or prior.get('policy_version') != policy.version or
                    ledger._get('trial_manifest') != json.dumps(prior, sort_keys=True)):
                raise ValueError('Trial identity, duration, rules or policy changed; restore original state')
        else:
            prior = {'started_at': now, 'ends_at': now + rules['duration_seconds'],
                     'account_id': ledger._get('account_id'), 'source_id': store.identity,
                     'rules': rules, 'policy_version': policy.version}
            with ledger.transaction():
                ledger._set('trial_manifest', json.dumps(prior, sort_keys=True))
            atomic_json(manifest, prior)
        return ledger, store, engine, prior
    except BaseException:
        ledger.close()
        if store:
            store.close()
        raise


class EntrySizingUnavailable(RpcError):
    """A bounded sizing pass could not produce a current entry-sized route."""
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.reason, self.details = reason, evidence


class EntryWorkDeferred(RpcError):
    """Entry allowance is spent; retain the original source event for retry."""


ENTRY_WORK_SECONDS = 80
EXIT_QUOTE_SECONDS = 25
PUBLICATION_RESERVE_SECONDS = 15


def affordable_input_ceiling(policy, state):
    """The existing full-loss ceiling, before final quote-inclusive sizing."""
    reference = max(Decimal(0), min(state['starting_cash'], state['equity']))
    return max(Decimal(0), min(policy.ticket_usd, state['cash'],
        reference * policy.per_position_risk_pct / 100,
        reference * policy.max_account_risk_pct / 100 - state['stress_exposure']))


def entry_account_blocker(engine, asset, now):
    """Only definite account blockers; no replacement for final admission."""
    ledger, policy = engine.ledger, engine.policy
    with ledger.transaction():
        engine._expire_marks(now)
        if ledger._get('entry_halt'):
            return 'manual_entry_halt'
        if engine._daily_halt(now):
            return 'daily_equity_loss'
        state = ledger._snapshot()
        if any(p['asset_id'] == asset for p in state['positions']):
            return 'already_open'
        if policy.max_open_positions and state['open_positions'] >= policy.max_open_positions:
            return 'max_open_positions'
        day_start = now - now % 86400
        count = ledger.conn.execute("SELECT COUNT(*) FROM fills WHERE action='OPEN' AND observed_at BETWEEN ? AND ?", (day_start, now)).fetchone()[0]
        if policy.max_entries_per_day and count >= policy.max_entries_per_day:
            return 'max_entries_per_day'
        closed = ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?', (asset,)).fetchone()[0]
        if closed is not None and now - closed < policy.cooldown_seconds:
            return 'cooldown'
        # Missing/stale marks reduce equity, while stress exposure retains the
        # holding's cost basis and reserve. It cannot invent free capacity.
        if affordable_input_ceiling(policy, state) < Decimal('0.01'):
            return 'risk_or_cash_budget_exhausted'
        return None


def sized_entry_quote(engine, request_quote, *, clock=None, deadline=None):
    """Quote the affordable base input, then refine using the unchanged risk model.

    ``request_quote(input_usd, deadline=...)`` returns a V3 quote and evidence. The V3
    input is settlement-asset spend; the engine's notional is output token spot
    value. Their conversion uses recorded buy input and quoted output coverage,
    never adds modeled stress or gas to the quoter's swap amount. Final entry
    admission still rechecks the original quote, costs and ledger transaction.
    """
    clock = time.time if clock is None else clock
    started = clock()
    end = min(started + 25, deadline) if deadline is not None else started + 25
    policy = engine.policy
    def snapshot():
        with engine.ledger.transaction():
            engine._expire_marks(int(clock()))
            return engine.ledger._snapshot()
    state = snapshot()
    ceiling = affordable_input_ceiling(policy, state)
    requested = ceiling.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
    audit = {'algorithm': 'affordable-entry-v1', 'started_at': int(started),
             'policy_version': policy.version, 'initial_input_ceiling_usd': str(requested),
             'maximum_quotes': 3, 'maximum_seconds': 25, 'maximum_input_excess_fraction': '0.001',
             'attempts': []}
    if requested < Decimal('0.01'):
        raise EntrySizingUnavailable('risk_or_cash_budget_exhausted', audit)
    for _ in range(3):
        if clock() >= end:
            raise EntrySizingUnavailable('entry_quote_sizing_deadline', audit)
        quote, evidence = request_quote(requested, deadline=end)
        problem = engine._quote_problem(quote, quote.asset_id, int(clock()))
        if problem or quote.route_side != 'both' or quote.route_max_notional_usd is None:
            raise EntrySizingUnavailable(problem or 'entry_quote_missing_buy_coverage', audit)
        if clock() >= end:
            raise EntrySizingUnavailable('entry_quote_sizing_deadline', audit)
        try:
            actual_input = Decimal(str(evidence.get('buy_input_usd')))
        except (AttributeError, InvalidOperation, ValueError):
            raise EntrySizingUnavailable('entry_quote_invalid_input_evidence', audit) from None
        if not actual_input.is_finite() or not 0 < actual_input <= requested:
            raise EntrySizingUnavailable('entry_quote_invalid_input_evidence', audit)
        # An oversized first probe may fail impact. It may inform a smaller
        # probe, but its impact is never removed from the quote used to enter.
        sizing_quote = replace(quote, route_impact_bps=Decimal(0))
        amounts = engine._size(sizing_quote, snapshot())
        planned_input = (amounts['notional'] * actual_input / quote.route_max_notional_usd
                         if amounts else None)
        audit['attempts'].append({'requested_input_usd': str(requested),
            'quoted_input_usd': str(actual_input), 'quote': quote.as_dict(), 'route': evidence,
            'planned_notional_usd': str(amounts['notional']) if amounts else None,
            'planned_swap_input_usd': str(planned_input) if planned_input is not None else None})
        # This tolerance is quote precision (0.1%), not an admission/risk limit.
        # The unmodified engine remains authoritative, including impact limits.
        tolerance = max(Decimal('0.00000001'), actual_input * Decimal('0.001'))
        if amounts is None or actual_input - planned_input <= tolerance:
            audit.update(finished_at=int(clock()), converged=amounts is not None,
                         status='SIZED' if amounts else 'NO_AFFORDABLE_SIZE')
            return quote, {**evidence, 'entry_sizing': audit}
        requested = planned_input.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        if requested < Decimal('0.01'):
            raise EntrySizingUnavailable('risk_or_cash_budget_exhausted', audit)
    raise EntrySizingUnavailable('entry_quote_sizing_not_converged', audit)


def run_cycle(folder, runtime, *, start=False, profile='legacy'):
    policy, rules, _ = profile_settings(profile)
    ledger, store, engine, trial = open_trial(folder, policy, start=start, rules=rules)
    parallel = profile == PARALLEL_PROFILE
    book = ConfirmationBook(store) if profile in ('two-wallet-30k', 'two-wallet-30k-reentry', PARALLEL_PROFILE) else None
    started = int(time.time())
    entry_deadline = min(started + ENTRY_WORK_SECONDS, trial['ends_at'])
    previous = json.loads((folder / 'status.json').read_text(encoding='utf-8')) if (folder / 'status.json').exists() else {}
    status = {'mode': 'EXPLORATORY_PAPER', 'started_at': started, 'errors': [], 'cycle_issues': [], 'decisions': [],
              'trial': trial, 'profile': profile, 'entry_rules': {'minimum_liquidity_usd': str(policy.min_liquidity_usd),
              'distinct_buyers_required': None if parallel else policy.min_independent_wallets, 'buyer_window_seconds': policy.max_confirmation_age_seconds},
              'rug_screens': [], 'real_money_enabled': False, 'strict_strategy_result': False}
    if parallel:
        status['entry_rules'].update(entry_revision=trial['entry_revision'], strategies=trial['entry_rules'])
    status['entry_work'] = {'deadline': entry_deadline, 'maximum_seconds_from_cycle_start': ENTRY_WORK_SECONDS,
        'final_exit_quote_allowance_seconds': EXIT_QUOTE_SECONDS, 'publication_reserve_seconds': PUBLICATION_RESERVE_SECONDS,
        'deferred': 0, 'cheap_rejections': {}, 'selected_attempts': 0, 'deadline_reached': False,
        'scope': 'Cooperative absolute entry deadline; socket deadlines and final checks bound providers, not OS scheduling or DNS.'}
    status['stage_seconds'] = {}
    for key in ('source_retry_at', 'quote_retry_at'):
        value = previous.get(key, 0)
        status[key] = value if type(value) is int and value > started else 0
    endpoint_file = runtime / 'observation-rpc-url.txt'
    endpoint = endpoint_file.read_text(encoding='utf-8-sig').strip() if endpoint_file.exists() else DEFAULT_RPC
    quote_endpoint_file = runtime / 'quote-rpc-url.txt'
    quote_endpoint = quote_endpoint_file.read_text(encoding='utf-8-sig').strip() if quote_endpoint_file.exists() else endpoint
    def error(exc):
        return (type(exc).__name__ + ': ' + str(exc).replace(quote_endpoint, '[QUOTE RPC]').replace(endpoint, '[RPC]'))[:400]
    def rate_limited(exc):
        return isinstance(exc, RpcError) and 'HTTP failure (429)' in str(exc)
    def source_error(exc):
        if rate_limited(exc):
            status['source_retry_at'] = int(time.time()) + 60
            status['errors'].append('Collection RPC HTTP 429; pending observations retained; retry on a scheduled cycle after backoff')
        else:
            status['errors'].append(error(exc))
    def check_entry_time():
        if time.time() >= entry_deadline:
            status['entry_work']['deadline_reached'] = True
            raise EntryWorkDeferred('Entry work deadline reached; original fresh events remain pending')
    @contextmanager
    def entry_phase(name, rpc=None, cap=None):
        check_entry_time()
        phase_started = time.time()
        left = entry_deadline - phase_started
        if cap is not None:
            left = min(left, cap)
        phase_deadline = phase_started + left
        try:
            with request_budget(phase_deadline, clock=time.time):
                if rpc is None:
                    yield phase_deadline
                else:
                    with rpc.budget(left):
                        yield phase_deadline
            check_entry_time()
            if time.time() >= phase_deadline:
                raise EntryWorkDeferred(name + ' allowance reached; unfinished observations remain pending')
        finally:
            status['stage_seconds'][name] = round(status['stage_seconds'].get(name, 0) + max(0, time.time() - phase_started), 6)
    def defer(exc, event_id=None):
        status['entry_work']['deferred'] += 1
        status['entry_work']['deadline_reached'] = time.time() >= entry_deadline
        ledger.record_event(f'entry-deferred:{started}:{event_id or "collection"}', 'entry_work_deferred',
            {'event_id': event_id, 'error_type': type(exc).__name__, 'reason': 'entry_budget' if isinstance(exc, (EntryWorkDeferred, RpcBudgetExceeded, RequestBudgetExceeded)) else 'provider_unavailable',
             'original_timestamp_preserved': True}, int(time.time()))
    def route(token, pool, quantity=Decimal(0), *, max_notional=None, deadline=None):
        route_rpc = RpcClient(quote_endpoint, timeout=8, max_retries=1)
        route_market = DexScreenerClient(chain_id='robinhood')
        if deadline is not None:
            original_transport = route_market.transport
            def bounded_transport(method, url, payload, headers, timeout):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise EntrySizingUnavailable('entry_quote_sizing_deadline', {})
                return original_transport(method, url, payload, headers, min(timeout, remaining))
            route_market.transport = bounded_transport
        client = V3RouteClient(route_rpc, route_market)
        kwargs = {'exit_only': True} if quantity > 0 else {}
        remaining = None if deadline is None else deadline - time.time()
        if remaining is not None and remaining <= 0:
            raise EntrySizingUnavailable('entry_quote_sizing_deadline', {})
        with route_rpc.budget(remaining):
            if deadline is None:
                quote = client.quote(token, pool, max_notional=policy.ticket_usd if max_notional is None else max_notional,
                                     sell_quantity=quantity, **kwargs)
            else:
                with request_budget(deadline, clock=time.time):
                    quote = client.quote(token, pool, max_notional=policy.ticket_usd if max_notional is None else max_notional,
                                         sell_quantity=quantity, **kwargs)
        return quote, client.last_evidence
    quote_cache, failed_until, issues = {}, {}, {}
    comparison, comparison_failed, watch_sequence = None, False, 0
    comparison_folder = runtime / 'paper-comparison'
    comparison_gap_path = comparison_folder / 'pending-gap.json'
    if (comparison_folder / 'manifest.json').is_file():
        try:
            comparison = Comparison(comparison_folder, source_account_id=trial['account_id'],
                                    source_id=store.identity, policy_version=policy.version)
            if comparison_gap_path.is_file():
                pending_gap = json.loads(comparison_gap_path.read_text(encoding='utf-8'))
                comparison.capture(f'capture-gap-recovery:{started}', 'gap', started,
                                   {**pending_gap, 'recovered_at': started})
                comparison_gap_path.unlink()
        except Exception as exc:
            comparison_failed = True
            status['errors'].append('Paper comparison unavailable: ' + type(exc).__name__)
    def comparison_capture(key, kind, now, payload):
        nonlocal comparison_failed
        if comparison is not None and not comparison_failed:
            try:
                comparison.capture(key, kind, now, payload)
            except Exception as exc:
                comparison_failed = True
                status['errors'].append('Paper comparison observation gap: ' + type(exc).__name__)
                try:
                    atomic_json(comparison_gap_path, {'reason': 'comparison_capture_failure',
                        'from': now, 'cycle': started, 'first_omitted_kind': kind,
                        'error_type': type(exc).__name__, 'scope': 'Some source inputs in this cycle were not captured; no reconstruction'})
                except Exception:
                    status['errors'].append('Paper comparison gap marker could not be persisted')
    def route_issue(exc, stage, asset, pool):
        unavailable = isinstance(exc, RouteUnavailable)
        if rate_limited(exc):
            status['quote_retry_at'] = int(time.time()) + 60
        code = 'route_unavailable' if unavailable else 'provider_or_route_error'
        key = (stage, asset, pool, code)
        if key not in issues:
            issues[key] = {'stage': stage, 'asset_id': asset, 'pool_id': pool, 'code': code,
                'severity': 'warning' if unavailable else 'error', 'message': error(exc), 'attempts': 0}
            status['cycle_issues'].append(issues[key])
            if not unavailable:
                status['errors'].append(f'{stage} {asset} {pool}: {error(exc)}')
        issues[key].update(attempts=issues[key]['attempts'] + 1, observed_at=int(time.time()),
                           evidence=getattr(exc, 'details', None))
        ledger.record_event(f'cycle-route:{started}:{stage}:{asset}:{pool}:{issues[key]["attempts"]}',
                            'route_observation_failure', issues[key], int(time.time()))
    def watch(deadline=None):
        nonlocal watch_sequence
        watch_started = time.time()
        watch_deadline = min(watch_started + EXIT_QUOTE_SECONDS, deadline) if deadline is not None else watch_started + EXIT_QUOTE_SECONDS
        positions = ledger.snapshot()['positions']
        quotes = {}
        if positions:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                for p in positions:
                    key = (p['asset_id'], p['pool_id'], str(p['qty']))
                    cached = quote_cache.get(key)
                    if cached is not None and 0 <= time.time() - cached.observed_at <= min(25, policy.max_quote_age_seconds):
                        quotes[p['asset_id']] = cached
                    elif time.time() < watch_deadline and time.time() >= max(failed_until.get(key, 0), status['quote_retry_at']):
                        futures.append((p, key, executor.submit(route, p['asset_id'].split(':')[-1], p['pool_id'], p['qty'], deadline=watch_deadline)))
                for p, key, future in futures:
                    try:
                        q, evidence = future.result()
                        quotes[p['asset_id']] = q
                        quote_cache[key] = q
                        ledger.record_event('route:' + p['position_id'] + ':' + str(q.observed_at), 'exploratory_exit_route', evidence, int(time.time()))
                    except Exception as exc:
                        failed_until[key] = time.time() + 30
                        route_issue(exc, 'position_exit', p['asset_id'], p['pool_id'])
        now = int(time.time())
        watch_sequence += 1
        comparison_capture(f'watch:{started}:{watch_sequence}', 'watch', now,
                           {'quotes': [q.as_dict() for q in quotes.values()], 'cycle': started})
        result = engine.watch(quotes, now)
        status['stage_seconds']['position_watch'] = round(status['stage_seconds'].get('position_watch', 0) + max(0, time.time() - watch_started), 6)
        return result
    try:
        ledger.record_event('collection-behavior:' + COLLECTION_REVISION, 'collection_behavior_revision',
            {'revision': COLLECTION_REVISION, 'source_id': store.identity,
             'global_transactions': 32, 'followup_pools': 8, 'followup_legs_per_pool': 16,
             'collection_window_blocks': 3000, 'signal_freshness_seconds': 900,
             'collection_schedule': 'once per eligible cycle before pending entry evaluation',
             'same_interval_followup': True, 'historical_intervals_reconstructed': False,
             'policy_version': policy.version, 'trading_rules_changed': False}, started)
        ledger.record_event('execution-model:affordable-entry-v1', 'execution_model_revision',
            {'component': 'entry_route_sizing', 'algorithm': 'affordable-entry-v1',
             'previous': 'route requested at maximum ticket before monetary risk sizing',
             'current': 'affordable input then bounded cost-aware refinement',
             'policy_version': policy.version, 'risk_limits_changed': False,
             'active_exits_changed': False}, started)
        last = previous.get('finished_at', started)
        gap = max(0, started - last)
        if gap > 180:
            ledger.record_event('gap:' + str(started), 'monitoring_gap', {'seconds': gap, 'from': last, 'to': started}, started)
            comparison_capture(f'gap:{started}', 'gap', started, {'reason': 'source_monitoring_gap', 'seconds': gap, 'from': last, 'to': started})
        comparison_capture(f'control:{started}', 'control', started, {'entry_halt': ledger._get('entry_halt')})
        status['monitoring_gap_seconds'] = gap
        status['exits'] = watch()
        source_setup_started = time.time()
        rpc = RpcClient(endpoint, timeout=8, max_retries=1)
        proof_rpc = RpcClient(quote_endpoint, timeout=8, max_retries=1)
        market = DexScreenerClient(chain_id='robinhood')
        source = ExploratorySource(rpc, store, market)
        status['stage_seconds']['source_setup'] = round(max(0, time.time() - source_setup_started), 6)
        def pending_stats():
            if callable(getattr(type(source), 'pending_summary', None)):
                return source.pending_summary(budget_seconds=.25, include_history=False)
            return {'fresh': None}
        def recorded_outcome(event_id):
            row = ledger.conn.execute('SELECT verdict,reason,observed_at FROM decisions WHERE event_id=?', (event_id,)).fetchone()
            return None if row is None else {'verdict': row['verdict'], 'reason': row['reason'],
                                            'decision_observed_at': row['observed_at']}
        maintenance_started = time.time()
        if callable(getattr(type(source), 'expire_pending', None)):
            status['expired_pending_recorded'] = source.expire_pending(resolve_outcome=recorded_outcome)
            status['expiry_progress'] = source.expiry_progress
        status['pending_before'] = pending_stats()
        status['stage_seconds']['pending_maintenance'] = round(max(0, time.time() - maintenance_started), 6)
        status['collection'] = {'status': 'NOT_RUN_TRIAL_EXPIRED', 'sampled': 0}
        def selected_work():
            """Collect once on cadence, then evaluate all fresh work oldest-first.

            A pending queue must not starve the collector until its 3,000-block
            window drops still-relevant intervals. Collection remains bounded by
            the same sample, providers and shared entry deadline.
            """
            attempted, resumed = set(), False
            status['collection'] = {'status': 'DEFERRED_ENTRY_BUDGET', 'sampled': 0}
            if ledger._get('entry_halt') or time.time() >= entry_deadline:
                return
            if time.time() < max(status['source_retry_at'], status['quote_retry_at']):
                status['collection'] = {'status': 'RATE_LIMIT_BACKOFF', 'sampled': 0,
                    'retry_at': max(status['source_retry_at'], status['quote_retry_at'])}
                return
            try:
                with entry_phase('collection', rpc, 45):
                    status['collection'] = source.collect(watch_pools=book.watched_pools(int(time.time()), policy.max_confirmation_age_seconds)) if book else source.collect()
                if store.reorgs(CHAIN_ID):
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
                    return
            except (EntryWorkDeferred, RpcBudgetExceeded, RequestBudgetExceeded) as exc:
                defer(exc)
            except Exception as exc:
                source_error(exc); defer(exc)
                if isinstance(exc, ReorgError):
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
                    return
                status['collection'] = {'status': 'PROVIDER_DEFERRED', 'sampled': 0}
            while time.time() < entry_deadline and not ledger._get('entry_halt'):
                if time.time() < max(status['source_retry_at'], status['quote_retry_at']):
                    status['collection'] = {'status': 'RATE_LIMIT_BACKOFF', 'sampled': 0,
                        'retry_at': max(status['source_retry_at'], status['quote_retry_at'])}
                    return
                fresh = [swap for swap in source.pending() if signal_id(swap) not in attempted]
                if fresh:
                    swap = fresh[0]
                    attempted.add(signal_id(swap))
                    yield swap
                    continue
                try:
                    if not resumed and book and callable(getattr(type(source), 'resume_followups', None)):
                        resumed = True
                        with entry_phase('followup_resume', rpc, 15):
                            status['followup_resume'] = source.resume_followups()
                        continue
                    return
                except (EntryWorkDeferred, RpcBudgetExceeded, RequestBudgetExceeded) as exc:
                    defer(exc)
                    return
                except Exception as exc:
                    source_error(exc); defer(exc)
                    if isinstance(exc, ReorgError):
                        with ledger.transaction():
                            ledger._set('entry_halt', 'source_reorganization_requires_review')
                    return
        # Any reorg halts new entries pending review; existing holdings remain
        # real paper holdings and continue receiving exit quotes.
        entry_processing_started = time.time()
        stages_before_entry = sum(status['stage_seconds'].values())
        if int(time.time()) < trial['ends_at']:
            if store.reorgs(CHAIN_ID):
                with ledger.transaction():
                    ledger._set('entry_halt', 'source_reorganization_requires_review')
            for swap in selected_work():
                if (time.time() >= entry_deadline or ledger._get('entry_halt')
                        or time.time() < status['quote_retry_at']):
                    break
                event_id = signal_id(swap)
                existing = ledger.conn.execute('SELECT verdict,reason FROM decisions WHERE event_id=?', (event_id,)).fetchone()
                if existing:
                    source.finish(swap, dict(existing))
                    continue
                signals, signal_checked_at = None, None
                def signal_state(reason=None, execution_status='blocked'):
                    if parallel and signals is not None:
                        record_signals(ledger, event_id, asset, trial['entry_revision'], signals, int(time.time()),
                            execution_status=execution_status, execution_reason=reason, execution_pool_id=pool)
                try:
                    status['entry_work']['selected_attempts'] += 1
                    with entry_phase('funded_buy_proof', rpc, 25) as proof_deadline:
                        proof_remaining = proof_deadline - time.time()
                        if proof_remaining <= 0:
                            raise EntryWorkDeferred('Funded-buy proof allowance reached')
                        with proof_rpc.budget(proof_remaining):
                            proof = source.prove(swap, fallback_rpc=proof_rpc if quote_endpoint != endpoint else None)
                    if proof is None or Decimal(proof['spent_usd']) < policy.min_wallet_spend_usd:
                        source.finish(swap, {'reason': 'not_a_supported_direct_buy_or_below_minimum'})
                        continue
                    token, pool = proof['token_address'], proof['pool']
                    asset = 'eip155:4663/erc20:' + token
                    ledger.record_event(event_id + ':proof', 'exploratory_wallet_buy', proof, int(time.time()))
                    if book:
                        book.record(event_id, proof)
                    if int(time.time()) - proof['observed_at'] > policy.max_confirmation_age_seconds:
                        expired = {'verdict': 'UNEVALUATED', 'reason': 'signal_expired',
                                   'observed_at': proof['observed_at'], 'expired_at': int(time.time())}
                        source.finish(swap, expired); status['decisions'].append(expired)
                        continue
                    # Independent paths must become visible even if the token
                    # is already held. Legacy policies retain their cheap check.
                    blocker = None if parallel else entry_account_blocker(engine, asset, int(time.time()))
                    if blocker:
                        rejected = {'verdict': 'SKIP', 'reason': blocker, 'event_id': event_id, 'asset_id': asset,
                                    'screening': 'not_run_account_blocker', 'route': 'not_run_account_blocker'}
                        source.finish(swap, rejected); status['decisions'].append(rejected)
                        counters = status['entry_work']['cheap_rejections']
                        counters[blocker] = counters.get(blocker, 0) + 1
                        ledger.record_event(event_id + ':account-precheck', 'entry_account_precheck', rejected, int(time.time()))
                        continue
                    if book:
                        if callable(getattr(type(source), 'followup', None)):
                            with entry_phase('same_interval_followup', rpc, 15):
                                focused = source.followup(pool, sampled=proof)
                            status.setdefault('same_interval_followup', []).append(focused)
                        closed = ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?', (asset,)).fetchone()[0]
                        if parallel:
                            all_proofs = book.recent_token_proofs(token, now=int(time.time()),
                                age=policy.max_confirmation_age_seconds, minimum_spend=policy.min_wallet_spend_usd)
                            signal_now = int(time.time())
                            evaluated_signals = evaluate_signals(all_proofs, token=token, now=signal_now,
                                last_closed_at=closed, rules=trial['entry_rules'], registry=load_wallet_registry(runtime, signal_now))
                            # Recorded journal membership is not a fresh chain
                            # check. Verify every displayed qualifying purchase.
                            cohort = signal_proofs(evaluated_signals)
                            if cohort:
                                with entry_phase('canonical_signal', rpc, 15):
                                    signal_checked_at = engine.confirm_proofs(cohort, rpc)
                            signals = evaluated_signals
                            if not qualified_signals(signals):
                                waiting = {'verdict': 'WAIT', 'reason': 'waiting_for_entry_strategy', 'asset_id': asset,
                                           'strategy_signals': signals, 'entry_revision': trial['entry_revision']}
                                signal_state('waiting_for_entry_strategy', 'not_evaluated')
                                source.finish(swap, waiting); status['decisions'].append(waiting)
                                continue
                            signal_state(None, 'not_evaluated')
                            blocker = entry_account_blocker(engine, asset, int(time.time()))
                            if blocker:
                                rejected = {'verdict': 'SKIP', 'reason': blocker, 'event_id': event_id, 'asset_id': asset,
                                            'entry_revision': trial['entry_revision'], 'qualified_strategies':
                                            [s['strategy_id'] for s in qualified_signals(signals)],
                                            'screening': 'not_run_account_blocker', 'route': 'not_run_account_blocker'}
                                signal_state(blocker)
                                source.finish(swap, rejected); status['decisions'].append(rejected)
                                counters = status['entry_work']['cheap_rejections']
                                counters[blocker] = counters.get(blocker, 0) + 1
                                ledger.record_event(event_id + ':account-precheck', 'entry_account_precheck', rejected, int(time.time()))
                                continue
                        else:
                            cohort = book.recent(token, pool, now=int(time.time()), age=policy.max_confirmation_age_seconds,
                                             minimum_spend=policy.min_wallet_spend_usd,
                                             observed_after=closed if policy.cooldown_seconds == 0 else None)
                        if not parallel and len(cohort) < policy.min_independent_wallets:
                            waiting = {'verdict': 'WAIT', 'reason': 'waiting_for_distinct_buyers', 'asset_id': asset,
                                       'observed_wallets': len(cohort), 'required_wallets': policy.min_independent_wallets}
                            source.finish(swap, waiting)
                            status['decisions'].append(waiting)
                            continue
                        if not parallel:
                            cohort = cohort[:policy.min_independent_wallets]
                    # Known provider failures persist across an outage. A missing
                    # response must not erase a previously failed safety check.
                    remembered = ledger._get('safety_rejection:' + token)
                    if remembered:
                        safety = SafetyEvidence.from_dict(json.loads(remembered))
                        reconciled = observed_pool_screen(safety, proof)
                        if book:
                            reconciled = enhance_rug_screen(reconciled)
                        if any(v is False for v in reconciled.checks.values()):
                            signal_state('previously_failed_safety')
                            source.finish(swap, {'reason': 'previously_failed_safety', 'checks': safety.checks})
                            continue
                    with entry_phase('goplus_screen', rpc):
                        safety = pool_adjusted_screen(GoPlusClient().screen(token), pool)
                        safety = observed_pool_screen(safety, proof)
                    # A current creator concentration failure is meaningful even
                    # when the provider omits its complete holder list.
                    with entry_phase('creator_and_supplemental', rpc):
                        safety = observe_creator_balance(safety, rpc)
                        safety = supplement_from_runtime(safety, pool, rpc, runtime)
                    if book:
                        safety = enhance_rug_screen(safety)
                        status['rug_screens'].append({'asset_id': asset, **safety.details['rug_screen']})
                        ledger.record_event(event_id + ':rug-screen', 'rug_screen', safety.as_dict(), int(time.time()))
                    if any(v is False for v in safety.checks.values()):
                        if any(v is False and k != 'rug_screen_available' for k,v in safety.checks.items()):
                            with ledger.transaction():
                                ledger._set('safety_rejection:' + token, json.dumps(safety.as_dict()))
                        quote = None
                    else:
                        # Keep marks fresh before another entry risk decision.
                        status['exits'].extend(watch(deadline=entry_deadline))
                        check_entry_time()
                        if time.time() < status['quote_retry_at']:
                            break  # Pending source event retained; exits keep retrying independently.
                        try:
                            with entry_phase('entry_quote', rpc):
                                quote, evidence = sized_entry_quote(engine,
                                    lambda amount, deadline: route(token, pool, max_notional=amount, deadline=deadline),
                                    deadline=entry_deadline)
                        except RpcError as exc:
                            if rate_limited(exc):
                                route_issue(exc, 'candidate_entry', asset, pool)
                                defer(exc, event_id)
                                break  # Quote provider backoff; retain the pending candidate.
                            raise
                        ledger.record_event(event_id + ':route', 'exploratory_entry_route', evidence, int(time.time()))
                    now = int(time.time())
                    if now >= trial['ends_at']:
                        break
                    confirmation = WalletConfirmation(proof['wallet'], 'unreviewed:' + proof['wallet'], proof['tx_hash'],
                        proof['observed_at'], Decimal(proof['spent_usd']), True, False)
                    if book:
                        if parallel:
                            # Quoting can consume the last seconds of B's older
                            # witness. Drop expired optional-path proofs before
                            # RPC validation so a still-valid A can qualify.
                            closed = ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?', (asset,)).fetchone()[0]
                            signals = evaluate_signals(all_proofs, token=token, now=now, last_closed_at=closed,
                                rules=trial['entry_rules'], registry=load_wallet_registry(runtime, now))
                            cohort = signal_proofs(signals)
                            if not qualified_signals(signals):
                                signal_state('entry_strategy_no_longer_qualified')
                                source.finish(swap, {'verdict': 'SKIP', 'reason': 'entry_strategy_no_longer_qualified'})
                                continue
                        with entry_phase('canonical_confirmation', rpc, 15):
                            if parallel:
                                signal_checked_at = engine.confirm_proofs(cohort, rpc)
                            else:
                                confirmations = book.confirm(cohort, rpc)
                    else:
                        confirmations = [confirmation]
                    now = int(time.time())
                    check_entry_time()
                    if now >= trial['ends_at']:
                        break
                    if now - proof['observed_at'] > policy.max_confirmation_age_seconds:
                        expired = {'verdict': 'UNEVALUATED', 'reason': 'signal_expired',
                                   'observed_at': proof['observed_at'], 'expired_at': now}
                        source.finish(swap, expired); status['decisions'].append(expired)
                        continue
                    if parallel:
                        closed = ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?', (asset,)).fetchone()[0]
                        signals = evaluate_signals(cohort, token=token, now=now, last_closed_at=closed,
                            rules=trial['entry_rules'], registry=load_wallet_registry(runtime, now))
                        qualified = qualified_signals(signals)
                        if not qualified:
                            signal_state('entry_strategy_no_longer_qualified')
                            source.finish(swap, {'verdict': 'SKIP', 'reason': 'entry_strategy_no_longer_qualified'})
                            continue
                        selected_signal = qualified[0]  # Stable A-first attribution, one shared position.
                        confirmations = [WalletConfirmation.from_dict(c) for c in selected_signal['confirmations']]
                    # The decision instant belongs to the route observation;
                    # canonical rechecks must not make a fresh quote appear to
                    # precede an artificially later strategy decision.
                    decision_at = quote.observed_at if quote is not None else now
                    symbol = safety.details.get('raw_report', {}).get('token_symbol') or token[:10]
                    symbol = ''.join(c for c in str(symbol) if ord(c) >= 32)[:80] or token[:10]
                    candidate = Candidate(event_id, asset, symbol, proof['observed_at'], confirmations, safety,
                        ExploratoryEngine.strategy_name, {'decision_at': decision_at, 'source_id': store.identity,
                        'proof_event': event_id + ':proof', 'coverage': 'sampled funded acquisitions',
                        'spend_basis': proof.get('spend_basis'), 'proof_scope': proof.get('proof_scope'),
                        'profile': profile, 'quorum_transactions': [c.tx_hash for c in confirmations], 'strict_strategy_result': False})
                    if parallel:
                        candidate.metadata.update(entry_revision=trial['entry_revision'],
                            strategy_signals=signals, qualified_strategies=[s['strategy_id'] for s in qualified],
                            initiating_strategy=selected_signal['strategy_id'], canonical_checked_at=signal_checked_at)
                    comparison_capture(f'candidate:{event_id}:{started}', 'candidate', now,
                        {'candidate': candidate.as_dict(), 'quote': quote.as_dict() if quote is not None else None, 'cycle': started})
                    check_entry_time()
                    now = int(time.time())
                    result = engine.consider(candidate, quote, now)
                    try:
                        record_market_signal(ledger, candidate, quote, result, now)
                    except Exception as exc:
                        status['errors'].append('Market observation notification: ' + type(exc).__name__)
                    source.finish(swap, json_safe(result))
                    status['decisions'].append(json_safe(result))
                    if result['verdict'] == 'PAPER_BUY':
                        # End entry work promptly so the next watcher remains on cadence.
                        break
                except ReorgError as exc:
                    if signals is not None:
                        signals = [{**s, 'qualified': False, 'qualification_reason': 'canonical_recheck_failed'} for s in signals]
                        signal_state('canonical_recheck_failed')
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
                    status['errors'].append(error(exc))
                    break
                except (EntryWorkDeferred, RpcBudgetExceeded, RequestBudgetExceeded) as exc:
                    signal_state('entry_work_deferred', 'deferred')
                    defer(exc, event_id)
                    break
                except SourceError as exc:
                    signal_state('market_evidence_unavailable', 'deferred')
                    if str(exc) == 'no valid matching pool; no substitute quote':
                        source.finish(swap, {'reason': 'pool_not_covered_by_market_feed'})
                    else:
                        status['errors'].append(error(exc))
                        defer(exc, event_id)
                except EntrySizingUnavailable as exc:
                    signal_state(exc.reason, 'deferred' if exc.reason != 'risk_or_cash_budget_exhausted' else 'blocked')
                    ledger.record_event(event_id + ':entry-sizing', 'entry_quote_sizing_unavailable', exc.details, int(time.time()))
                    if exc.reason != 'risk_or_cash_budget_exhausted':
                        defer(exc, event_id)
                        if time.time() >= entry_deadline:
                            break
                        continue
                    rejected = {'verdict': 'SKIP', 'reason': exc.reason, 'event_id': event_id,
                                'asset_id': asset, 'pool_id': pool}
                    source.finish(swap, rejected)
                    status['decisions'].append(rejected)
                except RouteUnavailable as exc:
                    signal_state('route_unavailable')
                    route_issue(exc, 'candidate_entry', asset, pool)
                    rejected = {'verdict': 'SKIP', 'reason': 'route_unavailable', 'event_id': event_id,
                                'asset_id': asset, 'pool_id': pool, 'side': exc.details['side']}
                    source.finish(swap, rejected)
                    status['decisions'].append(rejected)
                except RpcError as exc:
                    if rate_limited(exc):
                        source_error(exc)
                        defer(exc, event_id)
                        break  # Do not classify or consume unverified pending observations.
                    if 'eth_call returned error 3;' in str(exc):
                        rejected = {'verdict': 'SKIP', 'reason': 'contract_or_route_call_reverted', 'event_id': event_id}
                        source.finish(swap, rejected)
                        status['decisions'].append(rejected)
                    else:
                        status['errors'].append(error(exc))
                        defer(exc, event_id)
                except Exception as exc:
                    status['errors'].append(error(exc))
                    defer(exc, event_id)
                    # Retry transient provider/route errors until this signal expires.
                    continue
        now = int(time.time())
        status['entry_work']['finished_at'] = now
        status['entry_work']['deadline_reached'] = time.time() >= entry_deadline and now < trial['ends_at']
        measured_entry_work = sum(status['stage_seconds'].values()) - stages_before_entry
        status['stage_seconds']['entry_local_overhead'] = round(max(0, time.time() - entry_processing_started - measured_entry_work), 6)
        maintenance_started = time.time()
        status['pending_after'] = pending_stats()
        status['stage_seconds']['pending_maintenance'] = round(status['stage_seconds']['pending_maintenance'] + max(0, time.time() - maintenance_started), 6)
        # Refresh existing holdings after source work, before publishing equity.
        status['exits'].extend(watch())
        now = int(time.time())
        reporting_started = time.time()
        snapshot = engine.report(now)
        snapshot['fill_model'] = 'Pinned live V3 buy/sell quotes plus 100bps adverse stress, modeled gas and additional depth stress; simulated fills only'
        status['snapshot'] = json_safe(snapshot)
        status['trial_state'] = 'ACTIVE' if now < trial['ends_at'] else 'CLOSING' if snapshot['open_positions'] else 'COMPLETE'
        status['entry_halt'] = ledger._get('entry_halt')
        status['finished_at'] = now
        status['fills'] = [dict(row) for row in ledger.conn.execute('SELECT * FROM fills ORDER BY observed_at,id')]
        status['first_fill_at'] = min((f['observed_at'] for f in status['fills']), default=None)
        status['policy_revisions'] = [json.loads(r['payload']) for r in ledger.conn.execute("SELECT payload FROM events WHERE kind='policy_revision' ORDER BY observed_at")]
        status['total_monitoring_gaps'] = ledger.conn.execute("SELECT COUNT(*) FROM events WHERE kind='monitoring_gap'").fetchone()[0]
        status['stage_seconds']['financial_reporting'] = round(max(0, time.time() - reporting_started), 6)
        publication_started = time.time()
        try:
            status['telegram'] = notify_fills(ledger, folder, runtime)
        except Exception as exc:
            status['errors'].append('Telegram notification: ' + type(exc).__name__)
        try:
            status['market_telegram'] = notify_market(ledger, folder, runtime)
        except Exception as exc:
            status['errors'].append('Market Telegram notification: ' + type(exc).__name__)
        status['health'] = 'DEGRADED' if (status['errors'] or snapshot['unresolved_positions']
            or max(status['source_retry_at'], status['quote_retry_at']) > now) else 'OK'
        ledger.export_csv(folder / 'trades.csv')
        atomic_json(folder / 'status.json', json_safe(status))
        write_html(status, folder / 'report.html', data_mode='EXPLORATORY_PAPER / LIVE MARKET DATA')
        primary_completed_at = int(time.time())
        status['primary_completed_at'] = primary_completed_at
        status['primary_duration_seconds'] = max(0, primary_completed_at - started)
        status['stage_seconds']['primary_publication'] = round(max(0, time.time() - publication_started), 6)
        ledger.record_event('cycle-completed:' + str(started), 'exploratory_cycle_completed',
            {'started_at': started, 'primary_completed_at': primary_completed_at,
             'primary_duration_seconds': status['primary_duration_seconds'], 'profile': profile,
             'entry_work': status['entry_work'], 'stage_seconds': status['stage_seconds'],
             'pending_before': status['pending_before'], 'pending_after': status['pending_after'],
             'expired_pending_recorded': status.get('expired_pending_recorded', 0),
             'expiry_progress': status.get('expiry_progress'),
             'collection': status['collection'], 'errors': len(status['errors'])}, primary_completed_at)
        atomic_json(folder / 'status.json', json_safe(status))
        # All primary trades, notifications and publication complete first.
        # Comparisons are separate portfolios; failures cannot replay a source fill.
        if comparison is not None:
            try:
                now = int(time.time())
                comparison_capture(f'final-control:{started}', 'control', now, {'entry_halt': ledger._get('entry_halt')})
                if comparison_failed:
                    raise ValueError('Observation capture incomplete; comparison requires review')
                comparison.process()
                extra_quotes = []
                for asset, pool, quantity in comparison.needed_quotes(now, limit=2):
                    key = (asset, pool, str(quantity))
                    if time.time() >= started + 110 or time.time() < max(status['quote_retry_at'], failed_until.get(key, 0)):
                        comparison.capture(f'quote-deferred:{started}:{asset}:{pool}', 'gap', int(time.time()),
                            {'reason': 'comparison_quote_deferred', 'asset_id': asset, 'pool_id': pool})
                        continue
                    try:
                        q, _ = route(asset.split(':')[-1], pool, quantity, deadline=min(started + 110, time.time() + 15))
                        extra_quotes.append(q)
                    except Exception as exc:
                        comparison.capture(f'quote-gap:{started}:{asset}:{pool}', 'gap', int(time.time()),
                            {'reason': 'comparison_route_unavailable' if isinstance(exc, RouteUnavailable) else 'comparison_provider_error',
                             'asset_id': asset, 'pool_id': pool, 'error_type': type(exc).__name__})
                        if rate_limited(exc):
                            status['quote_retry_at'] = int(time.time()) + 60
                            break
                now = int(time.time())
                if extra_quotes:
                    comparison.capture(f'extra-watch:{started}', 'watch', now,
                                       {'quotes': [q.as_dict() for q in extra_quotes], 'supplemental': True, 'cycle': started})
                comparison.process()
                comparison.publish(now)
                status['comparison'] = {'state': 'OBSERVING', 'completed_at': now,
                    'comparison_id': comparison.manifest['comparison_id'], 'excluded_from_strategy': True}
            except Exception as exc:
                status['comparison'] = {'state': 'ERROR', 'at': int(time.time()), 'error_type': type(exc).__name__}
                status['errors'].append('Isolated paper comparison: ' + type(exc).__name__)
                status['health'] = 'DEGRADED'
            atomic_json(folder / 'status.json', json_safe(status))
        status['cycle_completed_at'] = int(time.time())
        status['cycle_duration_seconds'] = max(0, status['cycle_completed_at'] - started)
        atomic_json(folder / 'status.json', json_safe(status))
        return status
    finally:
        if comparison is not None:
            comparison.close()
        ledger.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true', help='Initialize a new seven-day paper trial; never resets an existing trial')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--activate-v2', action='store_true', help='Explicitly activate the user-requested $30k/two-buyer revision; preserves previous records')
    parser.add_argument('--activate-reentry', action='store_true', help='Authorized entry-only revision: no count caps; fresh post-close quorum re-entry')
    parser.add_argument('--activate-parallel', action='store_true', help='Explicitly activate independently reviewed A/B entry rules on the existing account')
    parser.add_argument('--whale-eligibility', choices=('reviewed_tracked', 'observed_recipient'))
    parser.add_argument('--whale-reentry', choices=('fresh_post_close', 'cooldown_24h'))
    args = parser.parse_args()
    if args.activate_parallel and (not args.whale_eligibility or not args.whale_reentry):
        parser.error('--activate-parallel requires explicit --whale-eligibility and --whale-reentry')
    if not args.activate_parallel and (args.whale_eligibility or args.whale_reentry):
        parser.error('Whale rule choices are accepted only with --activate-parallel')
    if args.activate_parallel and (args.start or args.activate_v2 or args.activate_reentry):
        parser.error('Independent entry activation cannot be combined with initialization or older revisions')
    args.runtime.mkdir(parents=True, exist_ok=True)
    folder = args.runtime
    try:
        with instance_lock(args.runtime / 'exploration-controller.lock'):
            if args.activate_v2:
                activate_v2(args.runtime)
            if args.activate_reentry:
                activate_reentry(args.runtime)
            if args.activate_parallel:
                with instance_lock(args.runtime / 'exploration' / 'worker.lock'):
                    activate_parallel(args.runtime, whale_eligibility=args.whale_eligibility,
                                      whale_reentry=args.whale_reentry)
            profile = active_profile(args.runtime)
            _, _, name = profile_settings(profile)
            folder = args.runtime / name
            folder.mkdir(parents=True, exist_ok=True)
            with instance_lock(folder / 'worker.lock'):
                status = run_cycle(folder, args.runtime, start=args.start, profile=profile)
        print(json.dumps({k: status[k] for k in ('health', 'trial_state', 'first_fill_at', 'decisions', 'errors')}, indent=2))
        return 0
    except Exception as exc:
        # Never print private endpoint URLs or a traceback from provider libraries.
        atomic_json(folder / 'worker-error.json', {'at': int(time.time()), 'type': type(exc).__name__})
        print(json.dumps({'health': 'ERROR', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    raise SystemExit(main())
