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


def rows(connection, sql, params=()):
    return [dict(row) for row in connection.execute(sql, params)]


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

    def cached(self, key, lifetime, load):
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < lifetime:
                return cached[1]
            value = load()
            self._cache[key] = (time.monotonic(), value)
            return value

    def jobs(self):
        def load():
            if os.name != 'nt':
                return {'available': False, 'items': []}
            command = "$ErrorActionPreference='Stop'; Get-ScheduledTask | Where-Object { $_.TaskName -in @('SNIPER Exploratory Paper','SNIPER Paper Research','SNIPER Telegram Monitor','RUG DESK Watch','RUG DESK Scan','RUG DESK Report','RUG DESK Console') } | ForEach-Object { $i=$_ | Get-ScheduledTaskInfo; [pscustomobject]@{name=$_.TaskName;state=[string]$_.State;last_result=$i.LastTaskResult;last_run=$i.LastRunTime.ToUniversalTime().ToString('o');next_run=$i.NextRunTime.ToUniversalTime().ToString('o')} } | ConvertTo-Json -Compress"
            try:
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise ValueError('Task inspection unavailable')
                items = json.loads(result.stdout.decode('utf-8-sig'))
                return {'available': True, 'checked_at': int(time.time()), 'items': items if isinstance(items, list) else [items]}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return {'available': False, 'items': []}
        return self.cached('jobs', 30, load)

    def state(self, bot='robinhood'):
        if bot not in ('robinhood', 'solana'):
            raise ValueError('Unknown bot')
        def load():
            data = self.robinhood() if bot == 'robinhood' else self.solana()
            data.update({'bot': bot, 'version': __version__, 'generated_at': int(time.time()),
                         'refresh_seconds': 5, 'read_only': True, 'real_money_enabled': False,
                         'execution_target': 'automatic', 'live_execution_ready': False,
                         'jobs': self.jobs()})
            return data
        return self.cached(('state', bot), 3, load)

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
            for p in snapshot['positions']:
                p['unrealized_pnl'] = p['liquidation_value'] - p['remaining_basis']
                p['unrealized_pct'] = p['unrealized_pnl'] / p['remaining_basis'] * 100 if p['remaining_basis'] else None
                # Exactly the entry_price basis used by PaperEngine.watch.
                p['stop_price'] = p['entry_price'] * (1 - decimal(policy['stop_loss_pct']) / 100)
                p['take_price'] = p['entry_price'] * (1 + decimal(policy['take_profit_pct']) / 100)
                p['trailing_price'] = p['peak_price'] * (1 - decimal(policy['trailing_stop_pct']) / 100) if p['tp_done'] else None
            fills = rows(c, 'SELECT * FROM fills ORDER BY id DESC LIMIT 500')
            closed = rows(c, 'SELECT * FROM closed_positions ORDER BY closed_at DESC LIMIT 200')
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
                row.update({'symbol': candidate.get('symbol'), 'pool_id': quote.get('pool_id'),
                            'buyers': len({x.get('wallet') for x in confirmations if x.get('wallet')}),
                            'liquidity_usd': quote.get('liquidity_usd'),
                            'safety_checks': safety.get('checks', {}),
                            'policy_version': (d.get('result') or {}).get('policy_version')})
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
            gaps = [selected(json.loads(r['payload']), ('from','to','seconds')) for r in rows(c, "SELECT payload FROM events WHERE kind='monitoring_gap' ORDER BY observed_at")]
            screens = {}
            for r in rows(c, "SELECT observed_at,payload FROM events WHERE kind='rug_screen' ORDER BY observed_at DESC LIMIT 160"):
                p = json.loads(r['payload']); asset = p.get('asset_id')
                if asset and asset not in screens:
                    screens[asset] = {'observed_at': r['observed_at'], 'checks': p.get('checks', {}), 'source': p.get('source', '')}
            # A quiet held coin can fall outside the recent candidate-screen window.
            # Read its latest actual evidence, including the original entry payload.
            for asset in assets:
                if asset not in screens:
                    row = c.execute("SELECT payload FROM events WHERE kind='rug_screen' AND json_extract(payload,'$.asset_id')=? ORDER BY observed_at DESC LIMIT 1", (asset,)).fetchone()
                    p = json.loads(row[0]) if row else None
                    if p is None:
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
        return {'account_id': identity, 'name': 'Robinhood · Wallet follower', 'chain': 'Robinhood Chain · 4663',
                'status': selected(status, ('started_at','finished_at','trial_state','entry_halt','collection')),
                'error_count': len(status.get('errors', [])), 'trial': selected(trial, ('started_at','ends_at','revision_started_at','policy_version')),
                'snapshot': snapshot, 'policy': policy, 'assets': list(assets.values()), 'fills': fills, 'closed': closed,
                'decisions': decisions, 'reasons': reasons, 'equity_history': equity, 'screens': screens,
                'market': market, 'event_counts': event_counts, 'gaps': gaps, 'policy_revisions': revisions,
                'daily': daily[0] if daily else None, 'research': research, 'collector': collector, 'repairs': repairs,
                'notifications': {'trades': queue(folder / 'notifications.sqlite3'), 'market': queue(folder / 'market-notifications.sqlite3')},
                'cadence_seconds': 120, 'fill_model': status.get('snapshot', {}).get('fill_model', 'Simulated fills using recorded live quotes'),
                'blocker': None, 'setup_blocked': False}

    def solana(self):
        folder = self.root / 'rug_desk/runtime'
        report = read_json(folder / 'report.json')
        with reader(folder / 'book.sqlite3') as c:
            identity = json.loads(c.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
            if identity.get('mode') != 'paper':
                raise ValueError('Unexpected Solana account mode')
            runs = rows(c, 'SELECT job,started,finished,status FROM runs ORDER BY started DESC LIMIT 24')
            checks = rows(c, 'SELECT mint,at,stage,verdict,reason FROM checks ORDER BY at DESC LIMIT 80')
            counts = rows(c, 'SELECT verdict,reason,COUNT(*) count FROM checks GROUP BY verdict,reason ORDER BY count DESC')
            fill_count = c.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
            open_count = c.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
        # A blocked, empty desk has no invented bankroll/equity curve. A future active desk
        # needs its own ledger adapter before exposing position-level financial analytics.
        return {'account_id': identity['account_id'], 'name': 'Solana · RUG DESK', 'chain': 'Solana Mainnet',
                'status': {'finished_at': max((x['finished'] or 0 for x in runs), default=0), 'trial_state': report.get('trial_status')},
                'snapshot': {'open_positions': open_count, 'positions': [], 'realized_pnl': report.get('realized_pnl_total'), 'as_of': report.get('generated_at')},
                'trial': {}, 'policy': {}, 'assets': [], 'fills': [], 'closed': [], 'equity_history': [], 'screens': {},
                'decisions': [{'asset_id': r['mint'], 'observed_at': r['at'], 'symbol': r['mint'][:8], 'verdict': r['verdict'], 'reason': r['reason'], 'stage': r['stage']} for r in checks],
                'reasons': counts, 'runs': runs, 'event_counts': {}, 'gaps': [], 'policy_revisions': [], 'market': [],
                'notifications': {'trades': queue(folder / 'notifications.sqlite3')}, 'cadence_seconds': 600,
                'fill_count': fill_count, 'setup_blocked': True, 'blocker': 'Grok / X gate is unfunded and disabled. The Solana trial has not started.' if not fill_count and not open_count else 'Solana ledger activity requires a position adapter; this view does not value those holdings.',
                'fill_model': report.get('execution_model'), 'paid_ai_enabled': report.get('paid_ai_enabled', False)}

    def chart(self, bot, asset, pool):
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
    files = {'/': ('index.html','text/html; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8'), '/favicon.svg': ('favicon.svg','image/svg+xml')}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log request strings or local account information.

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
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
                elif url.path == '/api/chart':
                    self.send(200, json_text(data.chart(params.get('bot', ['robinhood'])[0], params.get('asset', [''])[0], params.get('pool', [''])[0])).encode())
                elif url.path == '/api/health':
                    self.send(200, json_text({'service':'RUG DESK Console','version':__version__,'read_only':True,'at':int(time.time())}).encode())
                else:
                    self.send(404, b'{"error":"Not found"}')
            except ValueError:
                self.send(409, b'{"error":"Account data unavailable or inconsistent. Retrying automatically; no account was changed."}')
            except (OSError, sqlite3.Error, KeyError, TypeError):
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
    print(f'RUG DESK observation console: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
