"""Restartable seven-day exploratory PAPER trial on live Robinhood observations."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from supervise import atomic_json, instance_lock
from sniper.adapters.evm import RpcClient, DEFAULT_RPC, CHAIN_ID, RpcError, ReorgError
from sniper.adapters.evm_store import EvmStore
from sniper.adapters.goplus import GoPlusClient
from sniper.adapters.solana import DexScreenerClient, SourceError
from sniper.adapters.v3_route import V3RouteClient
from sniper.exploration import ExploratoryEngine, exploratory_policy, pool_adjusted_screen, observed_pool_screen, observe_creator_balance
from sniper.exploratory_source import ExploratorySource, signal_id
from sniper.exploratory_alerts import notify_fills
from sniper.confirmation_book import ConfirmationBook
from sniper.rug_screen import enhance_rug_screen
from sniper.ledger import Ledger
from sniper.ledger import json_text
from sniper.models import Candidate, SafetyEvidence, WalletConfirmation, json_safe
from sniper.policy import RiskPolicy
from sniper.reporting import write_html

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


def profile_settings(profile):
    if profile == 'legacy':
        return exploratory_policy(RiskPolicy.from_file(ROOT / 'config' / 'paper.toml')), RULES, 'exploration'
    if profile == 'two-wallet-30k':
        return RiskPolicy.from_file(ROOT / 'config' / 'exploration-v2.toml'), RULES_V2, 'exploration'
    raise ValueError('Unknown paper profile')


def active_profile(runtime):
    path = runtime / 'active-exploration.json'
    if not path.exists():
        # A missing selector must not restart the retired strategy.
        if (runtime / 'exploration' / 'retired.json').exists() or (runtime / 'exploration-v2' / 'trial.json').exists():
            raise ValueError('Active profile selector missing; restore it before restarting')
        manifest_path = runtime / 'exploration' / 'trial.json'
        if manifest_path.exists() and json.loads(manifest_path.read_text(encoding='utf-8')).get('rules', {}).get('revision') == 'two-wallet-30k':
            raise ValueError('Active profile selector missing; rerun the authorized activation to recover it')
        return 'legacy'
    value = json.loads(path.read_text(encoding='utf-8'))
    profile = value.get('profile')
    _, _, name = profile_settings(profile)
    manifest = json.loads((runtime / name / 'trial.json').read_text(encoding='utf-8'))
    if value.get('account_id') != manifest.get('account_id'):
        raise ValueError('Active profile identity mismatch')
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


def open_trial(folder, policy, *, start=False, now=None, rules=None):
    rules = RULES if rules is None else rules
    now = int(time.time()) if now is None else now
    manifest = folder / 'trial.json'
    if not manifest.exists() and not start:
        raise ValueError('Trial is not initialized; run explore.py --start once')
    prior = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else None
    if prior and any(not (folder / f).is_file() for f in ('ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('Established trial database missing; restore original state')
    if not prior and any((folder / f).exists() for f in ('ledger.sqlite3', 'events.sqlite3')):
        raise ValueError('Trial manifest missing beside existing databases; restore original manifest')
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=bool(prior))
    store = None
    try:
        engine = ExploratoryEngine(ledger, policy)
        store = EvmStore(folder / 'events.sqlite3')
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


def run_cycle(folder, runtime, *, start=False, profile='legacy'):
    policy, rules, _ = profile_settings(profile)
    ledger, store, engine, trial = open_trial(folder, policy, start=start, rules=rules)
    book = ConfirmationBook(store) if profile == 'two-wallet-30k' else None
    started = int(time.time())
    previous = json.loads((folder / 'status.json').read_text(encoding='utf-8')) if (folder / 'status.json').exists() else {}
    status = {'mode': 'EXPLORATORY_PAPER', 'started_at': started, 'errors': [], 'decisions': [],
              'trial': trial, 'profile': profile, 'entry_rules': {'minimum_liquidity_usd': str(policy.min_liquidity_usd),
              'distinct_buyers_required': policy.min_independent_wallets, 'buyer_window_seconds': policy.max_confirmation_age_seconds},
              'rug_screens': [], 'real_money_enabled': False, 'strict_strategy_result': False}
    endpoint_file = runtime / 'observation-rpc-url.txt'
    endpoint = endpoint_file.read_text(encoding='utf-8-sig').strip() if endpoint_file.exists() else DEFAULT_RPC
    def error(exc):
        return (type(exc).__name__ + ': ' + str(exc).replace(endpoint, '[RPC]'))[:400]
    def route(token, pool, quantity=Decimal(0)):
        client = V3RouteClient(RpcClient(endpoint, timeout=8, max_retries=1), DexScreenerClient(chain_id='robinhood'))
        quote = client.quote(token, pool, max_notional=policy.ticket_usd, sell_quantity=quantity)
        return quote, client.last_evidence
    def watch():
        positions = ledger.snapshot()['positions']
        quotes = {}
        if positions:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [(p, executor.submit(route, p['asset_id'].split(':')[-1], p['pool_id'], p['qty'])) for p in positions]
                for p, future in futures:
                    try:
                        q, evidence = future.result()
                        quotes[p['asset_id']] = q
                        ledger.record_event('route:' + p['position_id'] + ':' + str(q.observed_at), 'exploratory_exit_route', evidence, int(time.time()))
                    except Exception as exc:
                        status['errors'].append(error(exc))
        return engine.watch(quotes, int(time.time()))
    try:
        last = previous.get('finished_at', started)
        gap = max(0, started - last)
        if gap > 180:
            ledger.record_event('gap:' + str(started), 'monitoring_gap', {'seconds': gap, 'from': last, 'to': started}, started)
        status['monitoring_gap_seconds'] = gap
        status['exits'] = watch()
        rpc = RpcClient(endpoint, timeout=8, max_retries=1)
        market = DexScreenerClient(chain_id='robinhood')
        source = ExploratorySource(rpc, store, market)
        # Any reorg halts new entries pending review; existing holdings remain
        # real paper holdings and continue receiving exit quotes.
        if int(time.time()) < trial['ends_at']:
            try:
                with rpc.budget(45):
                    status['collection'] = source.collect(watch_pools=book.watched_pools(int(time.time()), policy.max_confirmation_age_seconds)) if book else source.collect()
                if store.reorgs(CHAIN_ID):
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
            except Exception as exc:
                status['errors'].append(error(exc))
                if isinstance(exc, ReorgError):
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
            for swap in source.pending():
                if time.time() - started > 125 or int(time.time()) >= trial['ends_at'] or ledger._get('entry_halt'):
                    break
                event_id = signal_id(swap)
                existing = ledger.conn.execute('SELECT verdict,reason FROM decisions WHERE event_id=?', (event_id,)).fetchone()
                if existing:
                    source.finish(swap, dict(existing))
                    continue
                try:
                    with rpc.budget(25):
                        proof = source.prove(swap)
                    if proof is None or Decimal(proof['spent_usd']) < policy.min_wallet_spend_usd:
                        source.finish(swap, {'reason': 'not_a_supported_direct_buy_or_below_minimum'})
                        continue
                    token, pool = proof['token_address'], proof['pool']
                    asset = 'eip155:4663/erc20:' + token
                    ledger.record_event(event_id + ':proof', 'exploratory_wallet_buy', proof, int(time.time()))
                    if book:
                        book.record(event_id, proof)
                        cohort = book.recent(token, pool, now=int(time.time()), age=policy.max_confirmation_age_seconds,
                                             minimum_spend=policy.min_wallet_spend_usd)
                        if len(cohort) < policy.min_independent_wallets:
                            waiting = {'verdict': 'WAIT', 'reason': 'waiting_for_distinct_buyers', 'asset_id': asset,
                                       'observed_wallets': len(cohort), 'required_wallets': policy.min_independent_wallets}
                            source.finish(swap, waiting)
                            status['decisions'].append(waiting)
                            continue
                        cohort = cohort[:policy.min_independent_wallets]
                    # Known provider failures persist across an outage. A missing
                    # response must not erase a previously failed safety check.
                    remembered = ledger._get('safety_rejection:' + token)
                    if remembered:
                        safety = SafetyEvidence.from_dict(json.loads(remembered))
                        reconciled = observed_pool_screen(safety, proof)
                        if any(v is False for v in reconciled.checks.values()):
                            source.finish(swap, {'reason': 'previously_failed_safety', 'checks': safety.checks})
                            continue
                    try:
                        safety = pool_adjusted_screen(GoPlusClient().screen(token), pool)
                        safety = observed_pool_screen(safety, proof)
                    except Exception as exc:
                        safety = SafetyEvidence(asset, int(time.time()), {k: None for k in policy.required_safety_checks},
                                                'unavailable:exploratory_only', {'error': error(exc), 'report_pool_id': pool})
                    # A current creator concentration failure is meaningful even
                    # when the provider omits its complete holder list.
                    safety = observe_creator_balance(safety, rpc)
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
                        status['exits'].extend(watch())
                        quote, evidence = route(token, pool)
                        ledger.record_event(event_id + ':route', 'exploratory_entry_route', evidence, int(time.time()))
                    now = int(time.time())
                    if now >= trial['ends_at']:
                        break
                    confirmation = WalletConfirmation(proof['wallet'], 'unreviewed:' + proof['wallet'], proof['tx_hash'],
                        proof['observed_at'], Decimal(proof['spent_usd']), True, False)
                    if book:
                        with rpc.budget(15):
                            confirmations = book.confirm(cohort, rpc)
                    else:
                        confirmations = [confirmation]
                    now = int(time.time())
                    if now >= trial['ends_at']:
                        break
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
                    result = engine.consider(candidate, quote, now)
                    source.finish(swap, json_safe(result))
                    status['decisions'].append(json_safe(result))
                    if result['verdict'] == 'PAPER_BUY':
                        # End entry work promptly so the next watcher remains on cadence.
                        break
                except ReorgError as exc:
                    with ledger.transaction():
                        ledger._set('entry_halt', 'source_reorganization_requires_review')
                    status['errors'].append(error(exc))
                    break
                except SourceError as exc:
                    if str(exc) == 'no valid matching pool; no substitute quote':
                        source.finish(swap, {'reason': 'pool_not_covered_by_market_feed'})
                    else:
                        status['errors'].append(error(exc))
                except RpcError as exc:
                    if 'eth_call returned error 3;' in str(exc):
                        rejected = {'verdict': 'SKIP', 'reason': 'contract_or_route_call_reverted', 'event_id': event_id}
                        source.finish(swap, rejected)
                        status['decisions'].append(rejected)
                    else:
                        status['errors'].append(error(exc))
                except Exception as exc:
                    status['errors'].append(error(exc))
                    # Retry transient provider/route errors until this signal expires.
                    continue
        now = int(time.time())
        # Refresh existing holdings after source work, before publishing equity.
        status['exits'].extend(watch())
        now = int(time.time())
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
        try:
            status['telegram'] = notify_fills(ledger, folder, runtime)
        except Exception as exc:
            status['errors'].append('Telegram notification: ' + type(exc).__name__)
        status['health'] = 'DEGRADED' if status['errors'] else 'OK'
        ledger.export_csv(folder / 'trades.csv')
        atomic_json(folder / 'status.json', json_safe(status))
        write_html(status, folder / 'report.html', data_mode='EXPLORATORY_PAPER / LIVE MARKET DATA')
        return status
    finally:
        ledger.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true', help='Initialize a new seven-day paper trial; never resets an existing trial')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--activate-v2', action='store_true', help='Explicitly activate the user-requested $30k/two-buyer revision; preserves previous records')
    args = parser.parse_args()
    args.runtime.mkdir(parents=True, exist_ok=True)
    folder = args.runtime
    try:
        with instance_lock(args.runtime / 'exploration-controller.lock'):
            if args.activate_v2:
                activate_v2(args.runtime)
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
