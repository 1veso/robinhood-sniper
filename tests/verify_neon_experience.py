"""Isolated NEON UI replay through the existing engine; never a production runner.

Prepare a NEW directory, point a separate read-only console at that app root,
then play its stages. All providers/judgments are fixtures and all records are
prominently marked REPLAY. There are no paid calls, alerts, signers or broadcasts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE / 'src'))

from rug_desk.book import atomic, encode
from rug_desk.leader_signals import process_event
from rug_desk.trader_acceptance import ReplayJudge, ReplayMarket, ReplayRugcheck, event, key, trader
from rug_desk.traders import Selections
from rug_desk.watch import watch
from sniper.neon import NeonBook, POLICY, initialize, publish
from sniper.paper_events import cursor, stage

MARKER = 'neon-experience-acceptance-v1'
STAGES = ('reject', 'failed_refresh', 'entry', 'duplicate', 'missing_quote', 'recover', 'partial', 'close')
MINTS = {'reject': key(31), 'failed_refresh': key(32), 'entry': key(33)}


@contextmanager
def offline():
    """Every provider dependency is explicit; fail immediately on accidental IO."""
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('REPLAY forbids network access')), \
         patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('REPLAY forbids network access')):
        yield


def _load_marker(app):
    app = Path(app).resolve()
    marker = json.loads((app / 'REPLAY.json').read_text(encoding='utf-8'))
    if marker.get('reference') != MARKER or marker.get('synthetic') is not True or marker.get('excluded_from_strategy') is not True:
        raise ValueError('Refusing non-replay account')
    account = json.loads((app / 'neon_bot/runtime/account.json').read_text(encoding='utf-8-sig'))
    if account.get('account_id') != marker.get('account_id'):
        raise ValueError('Replay account identity changed')
    return marker


def prepare(app, *, now=None):
    app = Path(app).resolve()
    if app.exists():
        raise ValueError('Prepare requires a NEW isolated directory; existing accounts are never reset')
    now = int(time.time() if now is None else now)
    app.mkdir(parents=True)
    with offline():
        identity = initialize(app)
        marker = {'label': 'REPLAY', 'synthetic': True, 'excluded_from_strategy': True,
                  'reference': MARKER, 'account_id': identity['account_id'], 'created_at': now,
                  'scope': 'Offline UI acceptance. Mocked providers and judgments; not trading performance.'}
        atomic(app / 'REPLAY.json', encode(marker))
        book = NeonBook(app / 'neon_bot')
        try:
            with book.transaction():
                book.db.execute("INSERT INTO metadata VALUES('ui_replay',?)", (encode(marker),))
                book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
                book.db.execute("INSERT INTO metadata VALUES('trial',?)", (encode({
                    'account_id': identity['account_id'], 'started_at': now, 'ends_at': now + POLICY['trial_seconds'],
                    'mode': 'paper', 'purpose': 'SYNTHETIC REPLAY ONLY; not a prospective trial',
                    'synthetic': True, 'excluded_from_strategy': True}),))
                book.db.execute('CREATE TABLE ui_replay_steps(name TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL)')
            selection = Selections(book).select([trader(now)], judge=ReplayJudge(), clock=lambda: now)
            assert selection['entry_enabled']
            book.export()
            publish(book, app, now)  # Actual disabled paid-gate state stays disabled.
            result = {'app_root': str(app), 'account_id': identity['account_id'], 'mode': 'REPLAY',
                      'cursor': cursor(book.db), 'stages': list(STAGES), 'synthetic': True,
                      'excluded_from_strategy': True, 'external_calls': 0, 'paid_calls': 0,
                      'telegram_sends': 0, 'signatures': 0, 'broadcasts': 0}
            atomic(app / 'prepared.json', encode(result))
            return result
        finally:
            book.close()


class DelayedJudge(ReplayJudge):
    def __init__(self, delay=0):
        self.delay = delay

    def ask(self, *args, **kwargs):
        if self.delay:
            time.sleep(self.delay)  # Deliberate offline provider wait, not scanner acceleration.
        return super().ask(*args, **kwargs)


class FailedRefresh(ReplayMarket):
    def __init__(self, clock):
        super().__init__(clock)
        self.calls = 0

    def quote(self, mint, pool):
        self.calls += 1
        if self.calls == 2:
            raise ValueError('REPLAY: refreshed quote unavailable')
        return super().quote(mint, pool)


class MissingQuote:
    def quote(self, *_):
        raise ValueError('REPLAY: quote unavailable; holding must remain')


def _measure_commits(book, app, stage_name):
    """Test-only sidecar: timestamp just AFTER the real SQLite commit returns.

    This neither changes timestamps in the ledger nor adds production hooks.
    Every intermediate stage commit, including entry into a slow mock provider,
    gets a stable event ID that can be joined to browser receipt/paint telemetry.
    """
    transaction = book.transaction
    stream, sequence = cursor(book.db).rsplit(':', 1)
    last = [int(sequence)]

    @contextmanager
    def measured():
        with transaction():
            yield
        committed_ms = time.time_ns() // 1_000_000
        records = book.db.execute('SELECT seq,kind FROM observation_events WHERE seq>? ORDER BY seq', (last[0],)).fetchall()
        if records:
            with (app / 'replay-progress.jsonl').open('a', encoding='utf-8') as output:
                for row in records:
                    output.write(encode({'id': stream + ':' + str(row[0]), 'type': row[1],
                                         'stage': stage_name, 'commit_returned_at_ms': committed_ms,
                                         'account_id': book.identity['account_id'], 'mode': 'REPLAY'}) + '\n')
                output.flush()
            last[0] = records[-1][0]

    book.transaction = measured


def run_stage(app, name, *, clock=time.time, delay=0):
    app = Path(app).resolve()
    marker = _load_marker(app)
    if name not in STAGES:
        raise ValueError('Unknown replay stage')
    if not 0 <= delay <= 20:
        raise ValueError('Replay judgment delay must be 0..20 seconds')
    with offline():
        book = NeonBook(app / 'neon_bot')
        try:
            _measure_commits(book, app, name)
            row = book.db.execute('SELECT payload FROM ui_replay_steps WHERE name=?', (name,)).fetchone()
            if row:
                return {**json.loads(row[0]), 'already_completed': True}
            completed = {r[0] for r in book.db.execute('SELECT name FROM ui_replay_steps')}
            if any(previous not in completed for previous in STAGES[:STAGES.index(name)]):
                raise ValueError('Replay stages must run in their declared order')
            now = int(clock())
            if not marker['created_at'] <= now < marker['created_at'] + POLICY['trial_seconds']:
                raise ValueError('Replay clock outside its fixture interval; prepare a new isolated root')
            start = cursor(book.db)
            before_fills = len(book.fills())
            market = ReplayMarket(clock)
            judge = DelayedJudge(delay)
            rug = ReplayRugcheck(clock)
            if name in ('reject', 'failed_refresh', 'entry'):
                mint = MINTS[name]
                signal = event(now, mint=mint)
                # One synthetic signal identity per mint. Its original observation
                # is retained so replay proves durable event deduplication.
                with book.transaction():
                    stage(book, mint, now, 'intake', 'STARTED', reason='REPLAY synthetic leader buy received')
                    stage(book, mint, now, 'intake', 'PASSED', reason='REPLAY input admitted to the existing token pipeline')
                if name == 'reject':
                    rug.missing = True
                if name == 'failed_refresh':
                    market = FailedRefresh(clock)
                with book.run('signals', now=now) as run:
                    result = process_event(book, signal, clock=clock, market=market, rugcheck=rug, judge=judge)
                    run['result'] = result
                if name == 'entry':
                    atomic(app / 'entry-signal.json', encode(signal))
                    assert result['entry'] and len(book.fills()) == before_fills + 1
                else:
                    assert not result['entry'] and len(book.fills()) == before_fills
                    if name == 'failed_refresh':
                        assert result['verdict'] == 'BUY' and result['stage'] == 'refresh'
                    else:
                        assert result['verdict'] == 'SKIP' and result['stage'] == 'facts'
            elif name == 'duplicate':
                signal = json.loads((app / 'entry-signal.json').read_text(encoding='utf-8'))
                with book.run('signals', now=now) as run:
                    result = process_event(book, signal, clock=clock, market=market, rugcheck=rug, judge=judge)
                    run['result'] = result
                assert not result['entry'] and len(book.fills()) == before_fills
            else:
                if name == 'missing_quote':
                    market = MissingQuote()
                elif name == 'recover':
                    market.price = '1.05'
                elif name == 'partial':
                    market.price = '2.2'
                elif name == 'close':
                    market.price = '1.4'
                with book.run('watch', now=now) as run:
                    result = watch(book, market=market, clock=clock)
                    run['result'] = result
                if name == 'missing_quote':
                    assert result[0]['result'].startswith('feed_unavailable:')
                    assert len(book.fills()) == before_fills and len(book.positions()) == 1
                    assert book.positions()[0]['feed_error']
                elif name == 'recover':
                    assert result[0]['result'] == 'hold' and len(book.fills()) == before_fills
                elif name == 'partial':
                    assert result[0]['result'] == 'take_profit_1' and len(book.fills()) == before_fills + 1
                    assert len(book.positions()) == 1
                else:
                    assert result[0]['result'] == 'trailing_exit' and len(book.fills()) == before_fills + 1
                    assert not book.positions()
            book.export()
            publish(book, app, int(clock()))
            record = {'stage': name, 'at': int(clock()), 'result': result,
                      'account_id': marker['account_id'], 'mode': 'REPLAY', 'synthetic': True,
                      'excluded_from_strategy': True, 'before_cursor': start, 'after_cursor': cursor(book.db),
                      'fills_before': before_fills, 'fills_after': len(book.fills()),
                      'open_positions': len(book.positions()), 'completed_wall_time_ms': time.time_ns() // 1_000_000}
            with book.transaction():
                book.db.execute('INSERT INTO ui_replay_steps VALUES(?,?,?)', (name, record['at'], encode(record)))
            atomic(app / ('stage-' + name + '.json'), encode(record))
            return record
        finally:
            book.close()


def verify(app):
    app = Path(app).resolve()
    marker = _load_marker(app)
    book = NeonBook(app / 'neon_bot')
    try:
        steps = {r[0]: json.loads(r[1]) for r in book.db.execute('SELECT name,payload FROM ui_replay_steps')}
        fills = book.fills()
        events = [dict(r) for r in book.db.execute('SELECT seq,event_key,kind,observed_at,recorded_at FROM observation_events ORDER BY seq')]
        fill_events = [r for r in events if r['kind'] == 'paper_fill']
        checks = {
            'all_stages_completed': set(steps) == set(STAGES),
            'early_rejection_no_fill': steps.get('reject', {}).get('fills_after') == 0,
            'buy_then_refresh_failure_no_fill': steps.get('failed_refresh', {}).get('fills_after') == 0,
            'entry_committed_once': steps.get('entry', {}).get('fills_after') == 1,
            'duplicate_no_second_entry': steps.get('duplicate', {}).get('fills_after') == 1,
            'missing_quote_retains_holding': steps.get('missing_quote', {}).get('open_positions') == 1 and steps.get('missing_quote', {}).get('fills_after') == 1,
            'original_partial_and_trailing_close': len(fills) == 3 and [f['kind'] for f in fills] == ['buy', 'sell', 'sell'] and not book.positions(),
            'committed_fill_events_match_ledger': {r['event_key'] for r in fill_events} == {'fill:' + f['id'] for f in fills},
            'ordered_unique_events': len({r['seq'] for r in events}) == len(events) and len({r['event_key'] for r in events}) == len(events),
            'cost_model_retained': all(bool(f.get('execution_costs')) for f in fills),
            'replay_identity_bound': marker['account_id'] == book.identity['account_id'],
        }
        result = {'passed': all(checks.values()), 'checks': checks, 'mode': 'REPLAY',
                  'synthetic': True, 'excluded_from_strategy': True, 'account_id': marker['account_id'],
                  'fills': fills, 'steps': steps, 'event_count': len(events), 'cursor': cursor(book.db),
                  'network_calls': 0, 'paid_calls': 0, 'telegram_sends': 0, 'signatures': 0, 'broadcasts': 0,
                  'scope': 'Offline existing-engine presentation and failure handling; not live provider, strategy performance or execution readiness proof'}
        atomic(app / 'verification.json', encode(result))
        if not result['passed']:
            raise AssertionError(encode(checks))
        return result
    finally:
        book.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, help='Fresh isolated app root, or a previously prepared replay root')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--stage', choices=STAGES)
    action.add_argument('--play', action='store_true')
    action.add_argument('--verify', action='store_true')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--judge-delay', type=float, default=3.0)
    args = parser.parse_args()
    if not 0 <= args.interval <= 20 or not 0 <= args.judge_delay <= 20:
        parser.error('Replay delays must be 0..20 seconds')
    root = args.app_root or SOURCE / 'runtime/neon-experience-acceptance' / uuid.uuid4().hex
    if args.prepare:
        print(encode(prepare(root)), flush=True)
    elif args.stage:
        print(encode(run_stage(root, args.stage, delay=args.judge_delay)), flush=True)
    elif args.play:
        for name in STAGES:
            print(encode(run_stage(root, name, delay=args.judge_delay)), flush=True)
            if args.interval:
                time.sleep(args.interval)
        result = verify(root)
        print(encode({k: v for k, v in result.items() if k not in ('fills', 'steps')}), flush=True)
    else:
        result = verify(root)
        print(encode({k: v for k, v in result.items() if k not in ('fills', 'steps')}), flush=True)


if __name__ == '__main__':
    main()
