"""Berlin-day account report and durable Telegram delivery for recorded events."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import html
import json
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import Book, atomic, day, encode, process_lock
from sniper.telegram import AlertStore, TelegramClient


def notify(book, *, runtime=None, client_factory=TelegramClient):
    runtime = Path(runtime) if runtime else config.PROJECT / 'runtime'
    token, recipient = runtime / 'telegram-bot-token.txt', runtime / 'telegram-recipient.json'
    if not token.exists() or not recipient.exists():
        return {'configured': False, 'reason': 'Telegram pairing files missing'}
    with process_lock(book.runtime / 'notifications.lock'):
        path = book.runtime / 'notifications.sqlite3'
        prior = book.db.execute("SELECT value FROM metadata WHERE key='notifications'").fetchone()
        if prior and not path.exists():
            raise RuntimeError('Notification journal missing; restore to avoid duplicate alerts')
        outbox = AlertStore(path)
        try:
            if prior and prior[0] != outbox.identity:
                raise RuntimeError('Notification journal identity changed')
            with book.transaction():
                book.db.execute("INSERT OR IGNORE INTO metadata VALUES ('notifications',?)", (outbox.identity,))
            account = book.identity['account_id']
            for event in book.db.execute('SELECT * FROM events ORDER BY at,id'):
                outbox.enqueue(account + ':' + event['id'], {'account_id': account,
                    'event_id': event['id'], 'message': event['message']}, event['at'])
            if not outbox.counts().get('pending'):
                return {'configured': True, 'sent': 0, 'outbox': outbox.counts()}
            pairing = json.loads(recipient.read_text(encoding='utf-8'))
            client = client_factory(token.read_text(encoding='utf-8-sig').strip())
            me = client.call('getMe')
            if not isinstance(me, dict) or me.get('id') != pairing.get('bot_id'):
                raise ValueError('Telegram bot differs from the paired bot')
            outbox.bind(pairing['bot_id'], pairing['chat_id'])
            def valid(payload):
                row = book.db.execute('SELECT message FROM events WHERE id=?', (payload.get('event_id'),)).fetchone()
                if payload.get('account_id') != account or row is None or row[0] != payload.get('message'):
                    return False
                if payload['event_id'].startswith('exit:'):
                    pending_id = payload['event_id'].split(':', 1)[1]
                    return any((p.get('pending') or {}).get('id') == pending_id for p in book.positions())
                return True
            sent = 0
            for _ in range(10):
                count = outbox.deliver(client, pairing['chat_id'], valid)
                sent += count
                if not count:
                    break
            return {'configured': True, 'sent': sent, 'outbox': outbox.counts()}
        finally:
            outbox.close()


def summary(book, now):
    today = day(now)
    fills = book.fills()
    daily = [f for f in fills if day(f['at']) == today]
    checks = [dict(r) for r in book.db.execute('SELECT * FROM checks ORDER BY at DESC')]
    daily_checks = [c for c in checks if day(c['at']) == today]
    positions = book.positions(all_positions=True)
    closed = [p for p in positions if p.get('closed_at') is not None and day(p['closed_at']) == today]
    schedules = {}
    for job, cadence in (('watch', config.WATCH_SECONDS), ('scan', config.SCAN_SECONDS), ('report', 86400)):
        last = book.db.execute('SELECT * FROM runs WHERE job=? ORDER BY started DESC LIMIT 1', (job,)).fetchone()
        records = book.db.execute('SELECT started FROM runs WHERE job=? AND started>=? ORDER BY started', (job, now - 7 * 86400)).fetchall()
        gaps = sum(max(0, (b[0] - a[0]) // cadence - 1) for a, b in zip(records, records[1:]))
        schedules[job] = {'latest': dict(last) if last else None, 'estimated_missed_intervals': gaps,
                          'overdue': not last or now - last['started'] > cadence + 180}
    usage = []
    budget = book.runtime / 'ai-budget.sqlite3'
    if budget.exists():
        db = sqlite3.connect(f'file:{budget.as_posix()}?mode=ro', uri=True)
        try:
            db.row_factory = sqlite3.Row
            usage = [dict(r) for r in db.execute('SELECT r.*,u.billed_usd,u.payload FROM advisory_reservations r LEFT JOIN advisory_usage u USING(request_id) ORDER BY r.created_at DESC')]
        finally:
            db.close()
    from rug_desk.grok import Judge
    paid = Judge(book.root).recurring_enabled()
    return {'identity': book.identity, 'generated_at': now, 'day': today, 'timezone': config.TIMEZONE,
        'trial_status': 'NOT_STARTED: preflight/X evidence required' if not (book.runtime / 'trial.json').exists() else json.loads((book.runtime / 'trial.json').read_text()),
        'paid_ai_enabled': paid, 'checks_today': len(daily_checks),
        'rejections_today': dict(Counter(c['reason'] for c in daily_checks if c['verdict'] == 'SKIP')),
        'buy_signals_today': sum(c['verdict'] == 'BUY' for c in daily_checks),
        'paper_entries_today': sum(f['kind'] == 'buy' for f in daily),
        'open_positions': [p for p in positions if Decimal(p['remaining']) > 0],
        'realized_pnl_today': str(sum((Decimal(f['pnl']) for f in daily), Decimal(0))),
        'realized_pnl_total': str(sum((Decimal(f['pnl']) for f in fills), Decimal(0))),
        'closed_positions_today': len(closed), 'profitable_closed_positions_today': sum(Decimal(p['realized_pnl']) > 0 for p in closed),
        'profitable_exit_events_today': sum(f['kind'] == 'sell' and Decimal(f['pnl']) > 0 for f in daily),
        'schedules': schedules, 'api_usage': usage, 'recent_checks': checks[:50],
        'execution_model': config.PAPER_FILL_MODEL,
        'configuration_question': 'Does the 30% trailing distance retain gains after fees and delayed polls? Review recorded paths; no setting changed.',
        'limits_note': '$50 halt uses net realized Berlin-day P&L only; unrealized losses are excluded. Ten-minute stop checks are not exchange stop orders.',
        'limitations': 'Provider screens and X citations do not guarantee safety, independent controllers, executable orders or profit. This Solana account is separate from Robinhood results.'}


def write_report(book, now):
    out = summary(book, now)
    title = f"RUG DESK · {book.identity['mode'].upper()} · {out['day']}"
    cards = [('Paper entries today', out['paper_entries_today']), ('Open positions', len(out['open_positions'])),
             ('Realized P&L today', '$' + out['realized_pnl_today']), ('Checks today', out['checks_today'])]
    body = ''.join(f'<article><small>{html.escape(k)}</small><strong>{html.escape(str(v))}</strong></article>' for k,v in cards)
    status = 'Paid X judgment enabled' if out['paid_ai_enabled'] else 'X judgment blocked: paid access disabled / funded validation pending'
    document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + html.escape(title) + '</title>'
    document += '<style>body{font:16px system-ui;background:#101821;color:#ecf2f5;max-width:1100px;margin:40px auto;padding:24px}h1{font-size:32px}section{display:flex;gap:16px;flex-wrap:wrap}article{background:#20303f;padding:20px;min-width:180px;border-radius:12px}strong{display:block;font-size:28px;margin-top:8px}small{color:#bacbd8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#172430;padding:20px}p{line-height:1.6}.status{color:#ffd394}</style>'
    document += f'<h1>{html.escape(title)}</h1><p class="status">{html.escape(status)}</p><section>{body}</section>'
    document += '<p>' + html.escape(out['execution_model'] + '. ' + out['limits_note']) + '</p>'
    document += '<p>' + html.escape(out['limitations']) + '</p><details><summary>Account, checks, runs and evidence</summary><pre>' + html.escape(json.dumps(out, indent=2)) + '</pre></details></html>'
    atomic(book.root / 'report.html', document)
    atomic(book.runtime / 'report.json', encode(out))
    return out


def start_trial(book, now):
    """Explicit seven-day clock only after independent operational proof exists."""
    out = summary(book, now)
    if not out['paid_ai_enabled']:
        raise ValueError('Funded inference and X Search tests plus paid authorization are required')
    preflight_path = book.runtime / 'preflight.json'
    if not preflight_path.exists():
        raise ValueError('Host, provider, persistence and scheduler verification missing')
    preflight = json.loads(preflight_path.read_text(encoding='utf-8'))
    if (preflight.get('account_id') != book.identity['account_id']
            or preflight.get('config_sha256') != book.identity['config_sha256']
            or any(preflight.get(k) is not True for k in ('host_verified', 'public_providers_verified', 'persistence_verified', 'schedules_verified'))):
        raise ValueError('Preflight is incomplete or belongs to another account/configuration')
    for job, record in out['schedules'].items():
        if record['overdue'] or not record['latest'] or record['latest']['status'] != 'ok':
            raise ValueError(f'Completed on-time {job} run required')
    path = book.runtime / 'notifications.sqlite3'
    if not path.exists():
        raise ValueError('Telegram delivery unverified')
    db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    try:
        setup = db.execute('SELECT state,message_id FROM outbox WHERE token=?', (book.identity['account_id'] + ':setup',)).fetchone()
        if not setup or setup[0] != 'sent' or type(setup[1]) is not int:
            raise ValueError('Telegram server acknowledgement required')
    finally:
        db.close()
    with book.transaction():
        existing = book.db.execute("SELECT value FROM metadata WHERE key='trial'").fetchone()
        trial = json.loads(existing[0]) if existing else {'started_at': now, 'ends_at': now + 7 * 86400,
            'account_id': book.identity['account_id'], 'purpose': 'Operational paper observation; not proof of profit'}
        book.db.execute("INSERT OR IGNORE INTO metadata VALUES ('trial',?)", (encode(trial),))
        atomic(book.runtime / 'trial.json', encode(trial))
    return trial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    parser.add_argument('--manual-records', action='store_true')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--setup-notice', action='store_true')
    parser.add_argument('--start-trial', action='store_true')
    args = parser.parse_args()
    with process_lock(args.root / 'runtime' / 'report.lock'):
        book = Book(args.root, mode='manual' if args.manual_records else 'paper')
        try:
            if args.start_trial:
                print(encode(start_trial(book, int(time.time()))))
                return
            with book.run('report') as run:
                now = int(time.time())
                out = write_report(book, now)
                if args.setup_notice:
                    book.event('setup', now, 'RUG DESK — SOLANA PAPER SETUP\nSeparate ledger initialized. Watch: 10 minutes; scan: 30 minutes; daily report: 21:30 Europe/Berlin.\nPaid Grok/X calls remain disabled. Solana entries and its seven-day trial await funded X validation. Robinhood paper trial continues separately. No real-money trading.')
                elif args.send:
                    book.event('daily:' + out['day'], now,
                        f"RUG DESK {book.identity['mode'].upper()} — {out['day']} ({config.TIMEZONE})\nChecks: {out['checks_today']} | BUY signals: {out['buy_signals_today']} | Entries: {out['paper_entries_today']}\n"
                        f"Open: {len(out['open_positions'])} | Realized P&L: ${out['realized_pnl_today']}\nClosed positions: {out['closed_positions_today']} | Profitable closed: {out['profitable_closed_positions_today']}\n"
                        f"Paid X enabled: {out['paid_ai_enabled']}\n{config.PAPER_FILL_MODEL}\n{out['configuration_question']}")
                if args.send:
                    run['notifications'] = notify(book)
                run.update(day=out['day'], entries=out['paper_entries_today'], realized_pnl=out['realized_pnl_today'])
            write_report(book, int(time.time()))
            print(encode(run))
        finally:
            book.close()


if __name__ == '__main__':
    main()
