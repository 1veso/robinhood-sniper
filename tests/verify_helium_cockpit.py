"""Isolated Helium cockpit replay through its real scan/watch workers.

All market/model dependencies are fictional and network access is forbidden.
The account is explicitly synthetic, separately identified, and never eligible
for strategy-performance claims. Prepare a NEW app root, serve the console from
that root, and advance the stages to inspect committed UI changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE / 'src'))

from rug_desk.book import atomic
from sniper.helium.config import encode, load, digest
from sniper.helium.demo import FictionalProviders, FictionalReviewer
from sniper.helium.paper import Paper
from sniper.helium.service import scan, watch
from sniper.helium.view import snapshot
from sniper.krypton.demo import FictionalGrok, address, offline

MARKER = 'helium-cockpit-acceptance-v1'
STAGES = ('reject', 'entry', 'duplicate', 'mark', 'missing_quote', 'recover', 'close')
STAGE_OFFSETS = dict(zip(STAGES, (1, 2, 3, 4, 70, 71, 72)))


class ReplayProviders(FictionalProviders):
    """Fixture adapters report their source observations just as real adapters do."""

    def __init__(self, book, clock, stage):
        super().__init__(clock)
        self.book, self.stage = book, stage
        self.price = {'mark': '1.10', 'recover': '1.08', 'close': '2'}.get(stage, '1')

    def observed(self, source, state='OK'):
        now = int(self.clock())
        self.book.provider(source, {'attempted_at': now, 'finished_at': now,
                                    'state': state, 'retry_after': now,
                                    'scope': 'SYNTHETIC REPLAY; no remote request'})

    def profiles(self):
        self.observed('profiles')
        return [address(2)] if self.stage == 'reject' else [address(1)]

    def quote(self, mint, pool=None):
        if self.stage == 'missing_quote':
            self.observed('dexscreener', 'ERROR')
            raise ValueError('SYNTHETIC REPLAY: market quote unavailable')
        self.observed('dexscreener')
        return super().quote(mint, pool)

    def holders(self, mint, cfg):
        self.observed('rpc')
        return super().holders(mint, cfg)

    def lp(self, mint, pool):
        self.observed('rugcheck')
        return super().lp(mint, pool)


def _marker(app):
    app = Path(app).resolve()
    marker = json.loads((app / 'REPLAY.json').read_text(encoding='utf-8'))
    if (marker.get('reference') != MARKER or marker.get('synthetic') is not True
            or marker.get('excluded_from_strategy') is not True):
        raise ValueError('Refusing non-replay account')
    path = app / 'helium_bot/runtime/book.sqlite3'
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        meta = {k: json.loads(v) for k, v in db.execute(
            "SELECT key,value FROM meta WHERE key IN ('account_id','bot_id','demo','config_hash')")}
    finally:
        db.close()
    if (meta.get('account_id') != marker.get('account_id')
            or meta.get('bot_id') != 'helium' or meta.get('demo') is not True
            or meta.get('config_hash') != marker.get('config_hash')):
        raise ValueError('Replay account identity changed')
    return marker


def prepare(app, *, now=None):
    app = Path(app).resolve()
    if app.exists():
        raise ValueError('Prepare requires a NEW isolated directory; existing accounts are never reset')
    now = int(time.time() if now is None else now)
    folder = app / 'helium_bot'
    folder.mkdir(parents=True)
    (folder / 'config.json').write_bytes((SOURCE / 'helium_bot/config.json').read_bytes())
    with offline():
        cfg = load(folder)
        book = Paper(folder / 'runtime', cfg, initialize=True, demo=True, clock=lambda: now)
        try:
            marker = {'label': 'SYNTHETIC REPLAY', 'reference': MARKER,
                      'synthetic': True, 'excluded_from_strategy': True,
                      'account_id': book.identity, 'config_hash': digest(cfg),
                      'created_at': now, 'scope': 'Offline cockpit acceptance; no market or paid calls'}
            with book.transaction():
                book.db.execute('CREATE TABLE ui_replay_steps(name TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL)')
                book.set('ui_replay', marker)
            atomic(app / 'REPLAY.json', encode(marker))
            book.publish()
            result = dict(marker, app_root=str(app), stages=list(STAGES),
                          external_calls=0, paid_calls=0, signatures=0, broadcasts=0)
            atomic(app / 'prepared.json', encode(result))
            return result
        finally:
            book.close()


def run_stage(app, name, *, now=None):
    app = Path(app).resolve()
    marker = _marker(app)
    if name not in STAGES:
        raise ValueError('Unknown replay stage')
    now = int(marker['created_at'] + STAGE_OFFSETS[name] if now is None else now)
    if not marker['created_at'] <= now <= marker['created_at'] + 3600:
        raise ValueError('Replay clock outside the fixture interval')
    with offline():
        book = Paper(app / 'helium_bot/runtime', load(app / 'helium_bot'), demo=True, clock=lambda: now)
        try:
            prior = book.db.execute('SELECT payload FROM ui_replay_steps WHERE name=?', (name,)).fetchone()
            if prior:
                return dict(json.loads(prior[0]), already_completed=True)
            completed = {r[0] for r in book.db.execute('SELECT name FROM ui_replay_steps')}
            if any(step not in completed for step in STAGES[:STAGES.index(name)]):
                raise ValueError('Replay stages must run in their declared order')
            before = snapshot(book.db, now)
            providers = ReplayProviders(book, lambda: now, name)
            model = FictionalGrok(book, lambda: now)
            reviewer = FictionalReviewer(book, lambda: now)
            if name in ('reject', 'entry', 'duplicate'):
                errors = scan(app, book, providers, model, reviewer, lambda: now)
            else:
                errors = watch(app, book, providers, lambda: now)
            state = snapshot(book.db, now)
            expected_fills = 0 if name == 'reject' else 2 if name == 'close' else 1
            assert state['summary']['fill_count'] == expected_fills, (name, state['summary'])
            assert len(state['positions']) == (0 if name in ('reject', 'close') else 1)
            if name == 'missing_quote':
                assert errors and state['summary']['unresolved_positions'] == 1
                assert state['positions'][0]['mark_current'] is False
            else:
                assert not errors, errors
            if name in ('reject', 'duplicate'):
                assert model.calls == reviewer.calls == 0
            if name == 'entry':
                assert model.calls == reviewer.calls == 1
            record = {'stage': name, 'at': now, 'account_id': book.identity,
                      'mode': 'SYNTHETIC REPLAY', 'synthetic': True, 'excluded_from_strategy': True,
                      'fills_before': before['summary']['fill_count'], 'fills_after': state['summary']['fill_count'],
                      'positions': len(state['positions']), 'unresolved_positions': state['summary']['unresolved_positions'],
                      'errors': errors, 'mock_grok_calls': model.calls, 'mock_jev_calls': reviewer.calls,
                      'cursor': state.get('helium_workspace', {}).get('cursor')}
            with book.transaction():
                book.db.execute('INSERT INTO ui_replay_steps VALUES(?,?,?)', (name, now, encode(record)))
            atomic(app / ('stage-' + name + '.json'), encode(record))
            atomic(app / ('state-' + name + '.json'), encode(dict(state, initialized=True)))
            return record
        finally:
            book.close()


def verify(app):
    app = Path(app).resolve()
    marker = _marker(app)
    now = marker['created_at'] + STAGE_OFFSETS['close']
    book = Paper(app / 'helium_bot/runtime', load(app / 'helium_bot'), demo=True, clock=lambda: now)
    try:
        steps = {r[0]: json.loads(r[1]) for r in book.db.execute('SELECT name,payload FROM ui_replay_steps')}
        state = snapshot(book.db, now)
        fills = state['fills']
        missing = json.loads((app / 'state-missing_quote.json').read_text()) if 'missing_quote' in steps else {}
        history = state.get('equity_history', [])
        from sniper.helium.cockpit import journal
        event_rows = []
        cursor = ''
        while True:
            page = journal(app, after=cursor, limit=25)
            event_rows.extend(page['events'])
            if not page['has_more']:
                break
            if page['cursor'] == cursor:
                raise AssertionError('Replay journal pagination did not advance')
            cursor = page['cursor']
        fill_events = [event for event in event_rows if event.get('kind') == 'fill']
        checks = {
            'all_stages_completed': set(steps) == set(STAGES),
            'rejection_has_no_fill': steps.get('reject', {}).get('fills_after') == 0,
            'entry_committed_once': steps.get('entry', {}).get('fills_after') == 1,
            'duplicate_does_not_repeat_entry': steps.get('duplicate', {}).get('fills_after') == 1,
            'quote_outage_retains_position': steps.get('missing_quote', {}).get('positions') == 1 and steps.get('missing_quote', {}).get('fills_after') == 1,
            'stale_mark_is_unknown': missing.get('summary', {}).get('unresolved_positions') == 1,
            'full_original_take_profit': len(fills) == 2 and sorted(f['side'] for f in fills) == ['BUY', 'SELL'] and next((f['payload'].get('reason') for f in fills if f['side'] == 'SELL'), None) == 'take_profit' and not state['positions'],
            'modeled_costs_preserved': all('explicit_fee_usd' in fill['payload'] and 'embedded_friction_usd' in fill['payload'] for fill in fills),
            'durable_performance_history': len(history) >= len(STAGES),
            'stale_history_retains_gap': any(row.get('chart_equity') is None and row.get('unresolved_positions') for row in history),
            'committed_fill_notifications': {str(row.get('fill_id')) for row in fill_events} == {str(fill['id']) for fill in fills} and len(fill_events) == len(fills),
            'ordered_unique_event_ids': len({row['id'] for row in event_rows}) == len(event_rows),
            'separate_synthetic_account': state['account_id'] == marker['account_id'] and state['demo'] is True and state['execution_mode'] == 'paper',
            'no_other_bot_account_created': not any((app / name / 'runtime/book.sqlite3').exists() for name in ('krypton_bot', 'neon_bot', 'solana_bot', 'jev_bot')),
        }
        result = {'passed': all(checks.values()), 'checks': checks, 'steps': steps,
                  'account_id': marker['account_id'], 'mode': 'SYNTHETIC REPLAY',
                  'synthetic': True, 'excluded_from_strategy': True,
                  'fills': fills, 'event_count': len(event_rows), 'equity_observations': len(history),
                  'network_calls': 0, 'paid_calls': 0, 'signatures': 0, 'broadcasts': 0,
                  'scope': 'Original paper worker accounting and cockpit projection; not live provider, installed Windows scheduler, strategy returns or live execution validation'}
        atomic(app / 'verification.json', encode(result))
        if not result['passed']:
            raise AssertionError(encode(checks))
        return result
    finally:
        book.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--stage', choices=STAGES)
    action.add_argument('--play', action='store_true')
    action.add_argument('--verify', action='store_true')
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if not 0 <= args.interval <= 20:
        parser.error('Replay interval must be 0..20 seconds')
    app = args.app_root or SOURCE / 'runtime/helium-cockpit-acceptance' / uuid.uuid4().hex
    if args.prepare:
        result = prepare(app)
    elif args.stage:
        result = run_stage(app, args.stage)
    elif args.play:
        for name in STAGES:
            print(encode(run_stage(app, name)), flush=True)
            if args.interval:
                time.sleep(args.interval)
        result = verify(app)
    else:
        result = verify(app)
    print(encode({k: v for k, v in result.items() if k not in ('steps', 'fills')}), flush=True)


if __name__ == '__main__':
    main()
