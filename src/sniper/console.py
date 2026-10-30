"""Allowlisted, loopback-only observation API over the existing paper accounts.

Every database connection is mode=ro/query_only, with short read transactions.
No account constructors, provider calls, signing, notification sends or writes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .ledger import Ledger, decimal, json_text


@contextmanager
def reader(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


class LedgerView:
    """Reuse the engine's accounting calculation without opening a writable Ledger."""
    _get = Ledger._get
    _positions = Ledger._positions
    snapshot = Ledger._snapshot

    def __init__(self, connection):
        self.conn = connection


def selected(value, keys):
    return {key: value.get(key) for key in keys}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def bounded_json(path, maximum=16_000_000):
    if path.stat().st_size > maximum:
        raise ValueError('Evidence export exceeds console read limit')
    return read_json(path)


def public_wallet(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise ValueError('Invalid public address')
    return value.lower()


def rows(connection, sql, params=()):
    return [dict(row) for row in connection.execute(sql, params)]


ENTRY_STRATEGIES = {'whale_follow': 'A · Whale following', 'two_wallet_v3': 'B · Two-whale V3 confirmation'}


def entry_attribution(metadata):
    if not isinstance(metadata, dict):
        metadata = {}
    initiator = metadata.get('initiating_strategy')
    qualified = metadata.get('qualified_strategies')
    revision = metadata.get('entry_revision')
    return {'initiating_strategy': initiator if isinstance(initiator, str) and initiator in ENTRY_STRATEGIES else None,
            'qualified_strategies': [s for s in qualified if isinstance(s, str) and s in ENTRY_STRATEGIES] if isinstance(qualified, list) else [],
            'entry_revision': revision[:256] if isinstance(revision, str) else None}


def entry_signal_view(connection, trial):
    """Bounded projection of append-only signal states, never admission authority."""
    revision = trial.get('entry_revision')
    if not isinstance(revision, str) or not revision:
        return None
    since = trial.get('entry_revision_started_at', trial['started_at'])
    latest, scanned = {}, 0
    for row in connection.execute("SELECT observed_at,payload FROM events WHERE kind='entry_strategy_signal' AND observed_at>=? ORDER BY rowid DESC LIMIT 1000", (since,)):
        scanned += 1
        try:
            value = json.loads(row['payload'])
        except (ValueError, TypeError):
            continue
        if (not isinstance(value, dict) or value.get('entry_revision') != revision
                or not isinstance(value.get('strategy_id'), str) or value['strategy_id'] not in ENTRY_STRATEGIES):
            continue
        signal_key = value.get('signal_key')
        if not isinstance(signal_key, str) or not signal_key or signal_key in latest:
            continue
        # Provider payloads and raw proofs are intentionally absent from this API.
        projected = {'observed_at': row['observed_at'], 'qualified': value.get('qualified') is True}
        for key in ('signal_key','event_id','asset_id','symbol','strategy_id','entry_revision','qualification_reason',
                    'execution_pool_id','execution_status','execution_reason'):
            item = value.get(key)
            projected[key] = item[:256] if isinstance(item, str) else None
        projected.update(entry_attribution(value))
        projected['source_pools'] = [p for p in value.get('source_pools', [])[:32] if isinstance(p, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', p)] if isinstance(value.get('source_pools'), list) else []
        for key in ('observed_wallets', 'required_wallets'):
            projected[key] = value.get(key) if type(value.get(key)) is int and value[key] >= 0 else None
        latest[signal_key] = projected
    signals = list(latest.values())
    strategies = []
    for strategy_id, label in ENTRY_STRATEGIES.items():
        current = [s for s in signals if s['strategy_id'] == strategy_id]
        rules = trial.get('entry_rules', {}).get(strategy_id, {})
        eligibility = rules.get('eligibility') if isinstance(rules, dict) else None
        if eligibility not in ('reviewed_tracked', 'observed_recipient'):
            eligibility = None
        strategies.append({'strategy_id': strategy_id, 'label': label,
            'eligibility': eligibility,
            'observed_signals': len(current), 'qualified_signals': sum(s['qualified'] for s in current),
            'latest': current[0] if current else None})
    return {'entry_revision': revision, 'since': since, 'strategies': strategies, 'signals': signals[:80],
            'scanned_event_count': scanned, 'projected_signal_count': len(signals),
            'scope': 'Latest state per signal within at most 1000 recent state events; latest 80 signals displayed. These are bounded observations, not lifetime totals or separate strategy returns.'}


def queue(path):
    try:
        with reader(path) as c:
            counts = {r['state']: r['n'] for r in c.execute('SELECT state,COUNT(*) n FROM outbox GROUP BY state')}
            last = rows(c, "SELECT created_at,message_id FROM outbox WHERE state='sent' ORDER BY created_at DESC LIMIT 1")
            return {'available': True, 'counts': counts, 'last_ack': last[0] if last else None}
    except (OSError, sqlite3.Error):
        return {'available': False, 'counts': {}, 'last_ack': None}


class ConsoleData:
    def __init__(self, root):
        self.root = Path(root)
        self._cache = {}
        self._lock = threading.RLock()
        self._load_locks = {}
        self._jobs_refreshing = False
        self._jobs_thread = None

    def cached(self, key, lifetime, load):
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < lifetime:
                return cached[1]
            load_lock = self._load_locks.setdefault(key, threading.Lock())
        # Coalesce the same view, but never hold the global cache lock while
        # reading SQLite or exports. A slow chart must not block another bot.
        with load_lock:
            with self._lock:
                cached = self._cache.get(key)
                if cached and time.monotonic() - cached[0] < lifetime:
                    return cached[1]
            value = load()
            with self._lock:
                self._cache[key] = (time.monotonic(), value)
            return value

    def _inspect_jobs(self):
        if os.name != 'nt':
            return {'available': False, 'items': [], 'checked_at': None, 'error': 'unsupported_platform'}
        command = "$ErrorActionPreference='Stop'; Get-ScheduledTask -TaskName 'SNIPER*','RUG DESK*','NEON*','Pump Jev*','KRYPTON*','HELIUM*' | Where-Object { $_.TaskName -in @('SNIPER Exploratory Paper','SNIPER Paper Research','SNIPER Telegram Monitor','SNIPER Automatic Executor Shadow','RUG DESK Watch','RUG DESK Scan','RUG DESK Report','RUG DESK Console','NEON Paper Signals','NEON Paper Watch','Pump Jev Shadow Review','KRYPTON Paper Scan','KRYPTON Paper Watch','HELIUM Paper Scan','HELIUM Paper Watch') } | ForEach-Object { $i=$_ | Get-ScheduledTaskInfo; [pscustomobject]@{name=$_.TaskName;state=[string]$_.State;last_result=$i.LastTaskResult;last_run=$i.LastRunTime.ToUniversalTime().ToString('o');next_run=$i.NextRunTime.ToUniversalTime().ToString('o');working_directory=($_.Actions | Select-Object -First 1).WorkingDirectory} } | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError('Task inspection unavailable')
        items = json.loads(result.stdout.decode('utf-8-sig'))
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('Task inspection response is malformed')
        for item in items:
            directory=item.pop('working_directory',None)
            item['account_root_bound']=bool(directory and Path(directory).resolve()==self.root.resolve())
        return {'available': True, 'checked_at': int(time.time()), 'items': items, 'error': None}

    def _refresh_jobs(self):
        # PowerShell must never run under the shared account/chart cache lock.
        try:
            value = self._inspect_jobs()
            error = None
        except subprocess.TimeoutExpired:
            value, error = None, 'task_inspection_timeout'
        except Exception:
            value, error = None, 'task_inspection_unavailable'
        with self._lock:
            old = self._cache.get('jobs')
            result = dict(value if value is not None else old[1] if old else
                          {'available': False, 'items': [], 'checked_at': None})
            if error:
                result['error'] = error
            result['last_attempt_at'] = int(time.time())
            self._cache['jobs'] = (time.monotonic(), result)
            self._jobs_refreshing = False

    def jobs(self):
        """Return saved task observations immediately; at most one refresh runs."""
        with self._lock:
            cached = self._cache.get('jobs')
            if not self._jobs_refreshing and (not cached or time.monotonic() - cached[0] >= 30):
                self._jobs_refreshing = True
                self._jobs_thread = threading.Thread(target=self._refresh_jobs, name='console-task-inspection', daemon=True)
                try:
                    self._jobs_thread.start()
                except RuntimeError:
                    self._jobs_refreshing = False
                    value = dict(cached[1] if cached else {'available': False, 'items': [], 'checked_at': None})
                    value.update(error='task_inspection_unavailable', last_attempt_at=int(time.time()))
                    self._cache['jobs'] = (time.monotonic(), value)
            saved = self._cache.get('jobs')
            value = dict(saved[1] if saved else {'available': False, 'items': [], 'checked_at': None, 'error': None})
            value['refreshing'] = self._jobs_refreshing
            return value

    def state(self, bot='robinhood'):
        if bot not in ('robinhood', 'solana', 'neon', 'jev', 'krypton', 'helium'):
            raise ValueError('Unknown bot')
        def load():
            if bot in ('krypton', 'helium'):
                if bot == 'helium':
                    from .helium.view import view
                else:
                    from .krypton.view import view
                data = view(self.root)
                data['cadence_seconds'] = 60
                data['error_count'] = data.get('status', {}).get('error_count', 0)
            elif bot == 'jev':
                from .jev_review import view
                data = view(self.root)
            elif bot == 'neon':
                from .neon_console import view
                data = view(self.root)
            else:
                data = self.robinhood() if bot == 'robinhood' else self.solana()
            data.update({'bot': bot, 'version': __version__, 'generated_at': int(time.time()),
                         'refresh_seconds': 5, 'read_only': True, 'real_money_enabled': False,
                         'execution_target': 'automatic', 'live_execution_ready': False,
                         'jobs': self.jobs()})
            workspace_key = {'neon': 'neon_workspace', 'helium': 'helium_workspace'}.get(bot)
            if workspace_key and data.get(workspace_key):
                worker=data[workspace_key]['worker'];jobs=data['jobs']
                worker['schedule_observed_at']=jobs.get('checked_at')
                task_names = ('NEON Paper Signals', 'NEON Paper Watch') if bot == 'neon' else ('HELIUM Paper Scan', 'HELIUM Paper Watch')
                if not (data[workspace_key].get('replay') or data.get('demo')) and jobs.get('checked_at') and 0<=int(time.time())-jobs['checked_at']<=90:
                    for job in jobs.get('items',[]):
                        if not job.get('account_root_bound'):continue
                        from datetime import datetime
                        try:
                            planned=datetime.fromisoformat(job['next_run'].replace('Z','+00:00'))
                            planned=int(planned.timestamp()) if planned.tzinfo else None
                        except (ValueError,TypeError,KeyError):planned=None
                        if planned is not None and planned<=int(time.time()):planned=None
                        if job.get('name')==task_names[0]:worker['next_scan_at']=planned
                        if job.get('name')==task_names[1]:worker['next_watch_at']=planned
            from .switchover import inventory
            if bot not in ('neon','jev','krypton','helium'):
                data['switchover'] = inventory(self.root, 'evm' if bot=='robinhood' else 'solana')
            if bot=='robinhood':
                try:
                    executor=read_json(self.root/'runtime/executor/status.json')
                    data['executor']=selected(executor,('mode','finished_at','health','observed_candidates','signer_loaded','signing_calls','broadcast_calls','blockers','real_money_enabled'))
                except (OSError,ValueError):data['executor']={'mode':'UNAVAILABLE'}
            if bot in ('solana','neon'):
                from .jev_review import links
                try:
                    linked = links(self.root, data['account_id'])
                except (OSError, ValueError, sqlite3.Error):
                    linked = {}
                    data['jev_link_status'] = 'UNAVAILABLE'
                for decision in data.get('decisions',[]):
                    decision['jev'] = linked.get(decision.get('asset_id'))
            return data
        return self.cached(('state', bot), .15 if bot in ('neon', 'helium') else 3, load)

    def robinhood(self):
        folder = self.root / 'runtime/exploration'
        status = read_json(folder / 'status.json')
        trial = read_json(folder / 'trial.json')
        with reader(folder / 'ledger.sqlite3') as c:
            view = LedgerView(c)
            identity = view._get('account_id')
            if identity != trial['account_id'] or view._get('execution_mode') != 'paper':
                raise ValueError('Account identity or mode mismatch')
            snapshot = view.snapshot()
            snapshot['net_pnl'] = snapshot['equity'] - snapshot['starting_cash']
            snapshot['return_pct'] = snapshot['net_pnl'] / snapshot['starting_cash'] * 100
            snapshot['as_of'] = int(view._get('last_processed_at') or 0)
            policy = json.loads(view._get('policy'))
            from .radeon_exits import binding as exit_binding, KEY as EXIT_KEY
            active_exits = json.loads(view._get(EXIT_KEY) or 'null')
            exit_policy = selected(active_exits, ('revision','policy','activated_at')) if active_exits else None
            for p in snapshot['positions']:
                p['unrealized_pnl'] = p['liquidation_value'] - p['remaining_basis']
                p['unrealized_pct'] = p['unrealized_pnl'] / p['remaining_basis'] * 100 if p['remaining_basis'] else None
                # Exactly the entry_price basis used by PaperEngine.watch.
                p['stop_price'] = p['entry_price'] * (1 - decimal(policy['stop_loss_pct']) / 100)
                p['take_price'] = p['entry_price'] * (1 + decimal(policy['take_profit_pct']) / 100)
                p['trailing_price'] = p['peak_price'] * (1 - decimal(policy['trailing_stop_pct']) / 100) if p['tp_done'] else None
                bound = exit_binding(c, p['position_id'])
                if bound:
                    spec, stage = bound['policy'], bound['tp_stage']
                    p.update(exit_revision=bound['revision'], exit_policy_name=spec['name'],
                             inherited_original_exits=bool(bound['inherited']), tp_stage=stage,
                             profit_protection_armed=bound['armed_at'] is not None and stage == 0,
                             exit_policy=spec)
                    p['stop_price'] = p['entry_price'] * (1 - decimal(spec['stop_loss_pct']) / 100)
                    levels = spec['take_profits']
                    p['take_price'] = p['entry_price'] * (1 + decimal(levels[stage]['gain_pct']) / 100) if stage < len(levels) else None
                    p['trailing_price'] = p['peak_price'] * (1 - decimal(spec['trailing_stop_pct']) / 100) if stage else None
            fills = rows(c, 'SELECT * FROM fills ORDER BY id DESC LIMIT 500')
            closed = rows(c, 'SELECT * FROM closed_positions ORDER BY closed_at DESC LIMIT 200')
            origins = {}
            position_ids = sorted({item['position_id'] for item in snapshot['positions'] + fills + closed})
            origin_rows = c.execute("SELECT f.position_id,d.payload FROM fills f JOIN decisions d ON d.event_id=f.event_id WHERE f.action='OPEN' AND f.position_id IN ("
                + ','.join('?' for _ in position_ids) + ')', position_ids) if position_ids else []
            for row in origin_rows:
                payload = json.loads(row['payload'])
                metadata = payload.get('candidate', {}).get('metadata', {})
                attribution = entry_attribution(metadata)
                if attribution['initiating_strategy']:
                    origins[row['position_id']] = attribution
            for item in snapshot['positions'] + fills + closed:
                item.update(origins.get(item.get('position_id'), {}))
            assets = {}
            for fill in reversed(fills):
                assets[fill['asset_id']] = {'asset_id': fill['asset_id'], 'symbol': fill['symbol'], 'state': 'closed', 'pool_id': None}
            for p in snapshot['positions']:
                assets[p['asset_id']] = selected(p, ('asset_id', 'symbol', 'pool_id')) | {'state': 'open'}
            decisions = []
            # Payloads are untrusted source data. Only explicitly selected fields reach the browser.
            for row in rows(c, 'SELECT * FROM decisions ORDER BY observed_at DESC LIMIT 80'):
                d = json.loads(row.pop('payload'))
                candidate = d.get('candidate', {})
                quote = d.get('quote') or {}
                confirmations = candidate.get('confirmations') or []
                safety = candidate.get('safety') or {}
                metadata = candidate.get('metadata') or {}
                row.update({'symbol': candidate.get('symbol'), 'pool_id': quote.get('pool_id'),
                            'buyers': len({x.get('wallet') for x in confirmations if x.get('wallet')}),
                            'liquidity_usd': quote.get('liquidity_usd'),
                            'safety_checks': safety.get('checks', {}),
                            'policy_version': (d.get('result') or {}).get('policy_version'),
                            **entry_attribution(metadata)})
                decisions.append(row)
            for asset in assets.values():
                if not asset['pool_id']:
                    row = c.execute("SELECT json_extract(d.payload,'$.quote.pool_id') FROM fills f JOIN decisions d ON d.event_id=f.event_id WHERE f.asset_id=? AND f.action='OPEN' ORDER BY f.id DESC LIMIT 1", (asset['asset_id'],)).fetchone()
                    asset['pool_id'] = row[0] if row else None
            reasons = rows(c, 'SELECT verdict,reason,COUNT(*) count FROM decisions GROUP BY verdict,reason ORDER BY count DESC')
            event_counts = {r['kind']: r['n'] for r in c.execute('SELECT kind,COUNT(*) n FROM events GROUP BY kind')}
            equity = rows(c, 'SELECT observed_at,equity,reason FROM equity_history ORDER BY id DESC LIMIT 4000')[::-1]
            revisions = []
            for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='policy_revision' ORDER BY observed_at"):
                p = json.loads(r['payload'])
                revisions.append({'at': r['observed_at'], 'before': p['previous_trial']['policy_version'], 'after': p['new_trial']['policy_version']})
            active_version = view._get('policy_version')
            active_since = max((r['at'] for r in revisions if r['after'] == active_version), default=trial['started_at'])
            # Match both the activation window and recorded decision policy.
            # Historical/missing policy evidence must not become a current rejection.
            entry_revision = trial.get('entry_revision')
            entry_scope = {}
            if isinstance(entry_revision, str) and entry_revision:
                active_since = max(active_since, trial.get('entry_revision_started_at', trial['started_at']))
                entry_scope = {'entry_revision': entry_revision}
            revision_clause = " AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.candidate.metadata.entry_revision') END = ?" if entry_scope else ''
            active_reasons = rows(c, '''SELECT verdict,reason,COUNT(*) count FROM decisions
                WHERE observed_at>=? AND CASE WHEN json_valid(payload)
                THEN json_extract(payload,'$.result.policy_version') END = ?''' + revision_clause + '''
                GROUP BY verdict,reason ORDER BY count DESC,reason,verdict''',
                (active_since, active_version, entry_revision) if entry_scope else (active_since, active_version))
            def rejection_scope(counts, **scope):
                rejected = [r for r in counts if r['verdict'] == 'SKIP']
                return {**scope, 'decision_count': sum(r['count'] for r in counts),
                        'rejection_count': sum(r['count'] for r in rejected), 'reasons': rejected}
            rejection_scopes = {
                'current_policy': rejection_scope(active_reasons, policy_version=active_version, since=active_since, **entry_scope),
                'account': rejection_scope(reasons, since=trial['started_at'])}
            entry_strategies = entry_signal_view(c, trial)
            gaps = [selected(json.loads(r['payload']), ('from','to','seconds')) for r in rows(c, "SELECT payload FROM events WHERE kind='monitoring_gap' ORDER BY observed_at")]
            screens = {}
            for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='rug_screen' ORDER BY observed_at DESC LIMIT 160"):
                p = json.loads(r['payload']); asset = p.get('asset_id')
                if asset and asset not in screens:
                    screens[asset] = {'observed_at': r['observed_at'], 'checks': p.get('checks', {}), 'source': p.get('source', '')}
            # A quiet held coin can fall outside the recent candidate-screen window.
            # Read its latest actual evidence, including the original entry payload.
            missing = set(assets) - set(screens)
            if missing:
                # One shared historical scan, rather than an expensive JSON
                # predicate/full-table scan for every old traded asset.
                # Sort only small row keys; sorting raw provider payloads can
                # spill large temporary files onto a space-constrained drive.
                keys = rows(c, "SELECT rowid AS event_rowid,observed_at FROM events WHERE kind='rug_screen' ORDER BY observed_at DESC")
                for r in keys:
                    payload = c.execute('SELECT payload FROM events WHERE rowid=?',(r['event_rowid'],)).fetchone()[0]
                    p = json.loads(payload); asset = p.get('asset_id')
                    if asset in missing:
                        screens[asset] = {'observed_at':r['observed_at'],'checks':p.get('checks',{}),'source':p.get('source','')}
                        missing.remove(asset)
                        if not missing:break
            for asset in missing:
                row = c.execute('SELECT payload FROM decisions WHERE asset_id=? ORDER BY observed_at DESC LIMIT 1', (asset,)).fetchone()
                p = (json.loads(row[0]).get('candidate', {}).get('safety') or {}) if row else {}
                if p.get('asset_id'):
                    screens[asset] = selected(p, ('observed_at','checks','source'))
            repairs = []
            for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='operational_repair' ORDER BY observed_at"):
                p = json.loads(r['payload'])
                repairs.append({'at': r['observed_at'], 'summary': selected(p, ('action','reason','skipped_blocks','collection_skipped_blocks'))})
            market = []
            for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='market_signal' ORDER BY observed_at DESC LIMIT 15"):
                p = json.loads(r['payload'])
                market.append({'observed_at': r['observed_at'], **selected(p, ('asset_id','symbol','pool_id','liquidity_usd','buyers','price_usd'))})
            daily = rows(c, 'SELECT * FROM daily_equity ORDER BY day DESC LIMIT 1')
        try:
            research = selected(read_json(self.root / 'runtime/research/wallet-research.json'), ('candidate_count','complete_history_count','performance_qualified_count','approved_wallet_count'))
        except (OSError, ValueError):
            research = None
        try:
            collector = selected(read_json(self.root / 'runtime/status.json'), ('finished_at','health','collector_lag_blocks'))
        except (OSError, ValueError):
            collector = None
        return {'account_id': identity, 'name': 'Robinhood Ã‚Â· Wallet follower', 'chain': 'Robinhood Chain Ã‚Â· 4663',
                'status': selected(status, ('started_at','finished_at','trial_state','entry_halt','collection','source_retry_at','quote_retry_at')),
                'error_count': len(status.get('errors', [])), 'trial': selected(trial, ('started_at','ends_at','revision_started_at','policy_version','entry_revision','entry_revision_started_at')),
                'cycle_issues': [selected(i, ('stage','asset_id','pool_id','code','severity','attempts','observed_at')) for i in status.get('cycle_issues', [])],
                'snapshot': snapshot, 'policy': policy, 'exit_policy': exit_policy, 'assets': list(assets.values()), 'fills': fills, 'closed': closed,
                'decisions': decisions, 'entry_strategies': entry_strategies, 'reasons': reasons, 'rejection_scopes': rejection_scopes, 'equity_history': equity, 'screens': screens,
                'market': market, 'event_counts': event_counts, 'gaps': gaps, 'policy_revisions': revisions,
                'daily': daily[0] if daily else None, 'research': research, 'collector': collector, 'repairs': repairs,
                'notifications': {'trades': queue(folder / 'notifications.sqlite3'), 'market': queue(folder / 'market-notifications.sqlite3')},
                'cadence_seconds': 120, 'fill_model': status.get('snapshot', {}).get('fill_model', 'Simulated fills using recorded live quotes'),
                'blocker': None, 'setup_blocked': False}

    def solana(self):
        from .solana_console import view
        return view(self.root, 'solana')

    def wallets(self):
        """Public generated evidence only; unrelated runtime files are never exposed."""
        def load():
            path = self.root / 'runtime/research/wallet-evidence-index.json'
            if not path.exists():
                return {'available': False, 'addresses': [], 'generated_at': None}
            index = bounded_json(path)
            if index.get('schema_version') != 1 or index.get('mode') != 'RESEARCH_ONLY':
                raise ValueError('Unknown evidence export format')
            addresses = []
            for item in index['addresses'][:10000]:
                wallet = public_wallet(item['wallet'])
                row = selected(item, ('research_candidate','evidence_count','current_source_checked_observations',
                    'capture_only_observations','invalidated_observations','evidence_status','observed_recipient',
                    'qualified','history_complete','performance_qualified','first_observed_at','last_observed_at'))
                row['wallet'] = wallet
                row['roles'] = selected(item.get('roles', {}), ('funded_recipient','transaction_sender','base_payer'))
                addresses.append(row)
            return {'available': True, **selected(index, ('generated_at','address_count','candidate_count',
                'observation_count','source_checked_observations','capture_only_observations',
                'invalidated_observations','legacy_reference_only_count','qualified_count')),
                'addresses': addresses, 'row_limit': 10000,
                'displayed_address_count': len(addresses)}
        return self.cached('wallets', 5, load)

    def wallet_evidence(self, wallet):
        wallet = public_wallet(wallet)
        path = self.root / 'runtime/research/addresses' / (wallet + '.json')
        item = bounded_json(path)
        if item.get('schema_version') != 1 or item.get('mode') != 'RESEARCH_ONLY':
            raise ValueError('Unknown address export format')
        if public_wallet(item['wallet']) != wallet:
            raise ValueError('Address evidence identity mismatch')
        history = selected(item.get('history', {}), ('assessment','history_complete','performance_qualified',
            'performance_estimate_available','net_pnl_after_follower_stress_usd','gas_usd','closed_positions',
            'qualification_blockers','controller_status','independence_verified','qualified',
            'cutoff','controller_review_current','qualification_checked_at'))
        fields = ('source_id','chain_id','wallet','role','tx_hash','log_index','block_number','block_hash',
            'observed_at','pool','token','canonical','canonical_scope','canonical_checked_at',
            'proof_scope','proof_digest','raw_evidence_available','qualification','controller_independence',
            'funding_ownership','quote_address','amount_raw','spent_raw','spent_usd',
            'transaction_sender','base_payers','source_proof_scope','source_spend_basis')
        # The exporter orders each address chronologically; keep recent proof visible.
        observations = [selected(row, fields) for row in item.get('observations', [])[-200:][::-1]]
        return {'wallet': wallet, **selected(item, ('generated_at','evidence_status','evidence_count',
            'current_source_checked_observations','capture_only_observations','invalidated_observations',
            'first_observed_at','last_observed_at','research_candidate','qualified')),
            'history': history, 'observations': observations, 'observation_limit': 200,
            'shown_observations': len(observations),
            'scope': 'Public address evidence; qualification is the saved assessment at export time, not a trading authorization'}

    def wallet_csv(self):
        path = self.root / 'runtime/research/wallet-evidence.csv'
        if path.stat().st_size > 64_000_000:
            raise ValueError('CSV exceeds console download limit')
        return path.read_bytes()

    def chart(self, bot, asset, pool):
        if bot in ('solana','neon'):
            from .solana_console import chart
            return self.cached(('chart', bot, asset, pool), 5, lambda: chart(self.root,bot,asset,pool))
        if bot != 'robinhood' or not asset or not pool or len(asset) > 160 or len(pool) > 100:
            raise ValueError('Unknown chart')
        def load():
            folder = self.root / 'runtime/exploration'
            with reader(folder / 'ledger.sqlite3') as c:
                points = []
                for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='market_mark' AND json_extract(payload,'$.asset_id')=? AND json_extract(payload,'$.pool_id')=? ORDER BY observed_at DESC LIMIT 12000", (asset,pool))[::-1]:
                    p = json.loads(r['payload'])
                    price = decimal(p['price_usd'])
                    if price > 0:
                        points.append({'at': p['observed_at'], 'price': str(price), 'liquidity': p.get('liquidity_usd')})
                fills = rows(c, 'SELECT observed_at,action,reference_price,fill_price,quantity,pnl,reason,position_id FROM fills WHERE asset_id=? ORDER BY id', (asset,))
                # Keep markers pinned to the pool recorded for each entry, including closed positions.
                matching = set()
                for fill in fills:
                    if fill['action'] == 'OPEN':
                        row = c.execute('SELECT payload FROM decisions WHERE event_id=(SELECT event_id FROM fills WHERE position_id=? LIMIT 1)', (fill['position_id'],)).fetchone()
                        if row and (json.loads(row[0]).get('quote') or {}).get('pool_id') == pool:
                            matching.add(fill['position_id'])
                fills = [f for f in fills if f['position_id'] in matching]
            return {'asset_id': asset, 'pool_id': pool, 'points': points, 'fills': fills, 'sample_limit': 12000, 'generated_at': int(time.time()), 'source': 'Recorded pinned-pool market marks; no fabricated ticks or exchange candles'}
        return self.cached(('chart', bot, asset, pool), 5, load)


def make_server(root, host='127.0.0.1', port=8765):
    if host != '127.0.0.1':
        raise ValueError('Console binds to loopback only')
    data = ConsoleData(root)
    static = Path(__file__).resolve().parents[2] / 'console'
    files = {'/': ('index.html','text/html; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8'), '/favicon.svg': ('favicon.svg','image/svg+xml'),
             '/neon.js':('neon.js','text/javascript; charset=utf-8'),'/neon.css':('neon.css','text/css; charset=utf-8'),
             '/krypton.js':('krypton.js','text/javascript; charset=utf-8'),'/krypton.css':('krypton.css','text/css; charset=utf-8'),
             '/helium.js':('helium.js','text/javascript; charset=utf-8'),'/helium.css':('helium.css','text/css; charset=utf-8')}
    event_waiters=threading.BoundedSemaphore(8)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log request strings or local account information.

        def send(self, status, body, content_type='application/json; charset=utf-8', filename=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            permitted = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in permitted or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send(403, b'{"error":"Local console only"}'); return
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + x for x in permitted}:
                self.send(403, b'{"error":"Origin rejected"}'); return
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            try:
                if url.path in files:
                    name, mime = files[url.path]
                    self.send(200, (static / name).read_bytes(), mime)
                elif url.path == '/api/state':
                    self.send(200, json_text(data.state(params.get('bot', ['robinhood'])[0])).encode())
                elif url.path in ('/radeon-sizing', '/api/radeon-sizing'):
                    # Fixed derived files only. Reads cannot initialize or run the study.
                    name = 'report.html' if url.path == '/radeon-sizing' else 'report.json'
                    path = data.root / 'runtime/radeon-sizing' / name
                    if not path.is_file():
                        self.send(404, b'{"error":"Sizing study not initialized"}')
                    else:
                        self.send(200, path.read_bytes(), 'text/html; charset=utf-8' if name.endswith('.html') else 'application/json')
                elif url.path == '/api/events':
                    from .solana_console import events
                    bot=params.get('bot',['solana'])[0]
                    after=self.headers.get('Last-Event-ID') or params.get('after',[''])[0]
                    limit=int(params.get('limit',['200'])[0]);wait_ms=int(params.get('wait_ms',['0'])[0])
                    if not 0<=wait_ms<=10000:raise ValueError('Invalid event wait')
                    page=events(data.root,bot,after,limit)
                    # Each inspection closes its read transaction. Waiting neither
                    # locks the book nor drives a worker/provider. Bounded clients.
                    if wait_ms and not page['events'] and page['available']:
                        if not event_waiters.acquire(blocking=False):
                            self.send(429,b'{"error":"Event reader capacity reached; retry shortly."}');return
                        try:
                            deadline=time.monotonic()+wait_ms/1000
                            while not page['events'] and time.monotonic()<deadline:
                                time.sleep(min(.1,max(0,deadline-time.monotonic())))
                                page=events(data.root,bot,after,limit)
                        finally:event_waiters.release()
                    page['server_time_ms']=time.time_ns()//1_000_000
                    if 'text/event-stream' in self.headers.get('Accept','') or params.get('format')==['sse']:
                        # Bounded catch-up batches release SQLite immediately. EventSource
                        # reconnects with Last-Event-ID; no worker/provider calls occur here.
                        chunks=['retry: 250\n\n']
                        for event in page['events']:
                            chunks.append('id: '+event['id']+'\nevent: observation\ndata: '+json_text(event)+'\n\n')
                        chunks.append('event: cursor\ndata: '+json_text({k:v for k,v in page.items() if k!='events'})+'\n\n')
                        self.send(200,''.join(chunks).encode(),'text/event-stream; charset=utf-8')
                    else:
                        self.send(200,json_text(page).encode())
                elif url.path == '/api/neon-journal':
                    from .neon_workspace import journal
                    self.send(200,json_text(journal(data.root,
                        stage_filter=params.get('stage',[''])[0],token=params.get('token',[''])[0],
                        outcome_filter=params.get('outcome',[''])[0],category_filter=params.get('category',[''])[0],
                        reason_filter=params.get('reason',[''])[0],
                        since=int(params.get('since',['0'])[0]),before=int(params['before'][0]) if 'before' in params else None,
                        limit=int(params.get('limit',['100'])[0]))).encode())
                elif url.path == '/api/neon-candidate':
                    from .neon_workspace import candidate
                    self.send(200,json_text(candidate(data.root,params.get('mint',[''])[0])).encode())
                elif url.path == '/api/helium-journal':
                    from .helium.cockpit import journal
                    self.send(200,json_text(journal(data.root,
                        after=params.get('after',[''])[0],limit=int(params.get('limit',['200'])[0]),
                        reason_filter=params.get('reason',[''])[0],
                        stage_filter=params.get('stage',[''])[0],
                        before=int(params['before'][0]) if 'before' in params else None)).encode())
                elif url.path == '/api/helium-candidate':
                    from .helium.cockpit import candidate
                    self.send(200,json_text(candidate(data.root,params.get('mint',[''])[0])).encode())
                elif url.path == '/api/chart':
                    self.send(200, json_text(data.chart(params.get('bot', ['robinhood'])[0], params.get('asset', [''])[0], params.get('pool', [''])[0])).encode())
                elif url.path == '/api/wallets':
                    self.send(200, json_text(data.wallets()).encode())
                elif url.path == '/api/wallet-evidence':
                    evidence = data.wallet_evidence(params.get('wallet', [''])[0])
                    self.send(200, json_text(evidence).encode(), filename=(evidence['wallet']+'.json') if params.get('download') == ['1'] else None)
                elif url.path == '/api/wallet-export.csv':
                    self.send(200, data.wallet_csv(), 'text/csv; charset=utf-8', 'wallet-evidence.csv')
                elif url.path == '/api/health':
                    from .solana_console import discover
                    from .krypton.view import health as krypton_health
                    from .helium.view import health as helium_health
                    self.send(200, json_text({'service':'TENDIES TRACKER Observation Console','version':__version__,'read_only':True,'at':int(time.time()),
                        'paper_views':['robinhood','solana','neon','jev','krypton','helium'],'event_stream_version':1,
                        'event_stream_bots':['solana','neon'],'journal_bots':['helium'],'accounts':dict(discover(data.root),krypton=krypton_health(data.root),helium=helium_health(data.root))}).encode())
                elif url.path == '/api/jev-assessment':
                    from .jev_review import detail
                    self.send(200, json_text(detail(data.root, params.get('id',[''])[0])).encode())
                elif url.path == '/api/jev-readiness':
                    from .jev_readiness import report
                    self.send(200,json_text(report(data.root)).encode(),filename='jev-readiness.json')
                else:
                    self.send(404, b'{"error":"Not found"}')
            except ValueError:
                self.send(409, b'{"error":"Account data unavailable or inconsistent. Retrying automatically; no account was changed."}')
            except (OSError, sqlite3.Error, KeyError, TypeError, ArithmeticError):
                self.send(503, b'{"error":"Cannot read current account data. Retrying automatically; no account was changed."}')

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(default_root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=default_root)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.data_root, port=args.port)
    print(f'TENDIES TRACKER Observation Console: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
