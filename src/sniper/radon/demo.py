"""Synthetic fixtures and a separate, durable offline demo. Never opens the paper account."""
from pathlib import Path
import time
import uuid

from rug_desk.book import atomic
from sniper.helium.demo import address
from .book import Book
from .config import load, encode, digest, policy_hash
from .service import report


def quote(mint, now, price='1'):
    return {'mint': mint, 'pool': address(80), 'price': price, 'liquidity': '100000', 'volume_24h': '200000',
        'created_at_ms': (now-3600)*1000, 'observed_at': now, 'sol_usd': '150', 'sol_observed_at': now,
        'source': 'synthetic', 'executable': False}


def bundle(now, rows=None):
    return {'version': 'radon_export_v1', 'source_id': 'synthetic-acceptance', 'evidence_label': 'synthetic',
        'authorized': True, 'authorization_reference': 'synthetic fixture only', 'complete': True,
        'coverage': 'Fictional normalized swaps; no public feed claimed', 'opening_at': now-100,
        'through': now, 'openings': {address(1): {'verified': True, 'sequence': 0, 'balances': {}, 'evidence': 'synthetic zero opening'},
            address(2): {'verified': True, 'sequence': 0, 'balances': {}, 'evidence': 'synthetic zero opening'}},
        'events': rows or []}


def event(id, leader, sequence, at, side='buy', quantity='100', mint=None):
    return {'id': id, 'leader': leader, 'sequence': sequence, 'at': at, 'side': side,
            'quantity': quantity, 'mint': mint or address(20), 'route': 'raydium'}


def evidence(book, row, q, now):
    """An explicitly synthetic journal for integration tests and demo only."""
    if not book.demo:
        raise ValueError('Synthetic model evidence is confined to demo accounts')
    mint = q['mint']
    out = {'quote': q, 'facts': {'mint': mint, 'observed_at': now, 'mint_disabled': True, 'freeze_disabled': True,
        'complete_owner_coverage': True, 'max_single_holder': '0.01', 'top10_share': '0.10'},
        'lp': {'mint': mint, 'pool': q['pool'], 'observed_at': now, 'state': 'PASS'}}
    result = {'request_id': 'synthetic:'+row['id'], 'validated': True, 'verdict': 'BUY', 'account_id': book.identity,
        'policy_hash': policy_hash(book.cfg), 'event_id': row['id'], 'judged_at': now, 'synthetic': True}
    book.db.execute('INSERT OR REPLACE INTO api_requests VALUES(?,?)', (result['request_id'], encode({
        'state': 'RECEIVED', 'evidence_hash': digest(out), 'result': result, 'usage': {'billed_usd': '0'}, 'synthetic': True})))
    return dict(out, x=result)


def demo(root):
    now = int(time.time())-5; cfg = load(Path(root)/'radon_bot')
    target = Path(root)/'runtime/radon-demo'/uuid.uuid4().hex[:10]
    atomic(target/'radon_bot/config.json', encode(cfg))
    book = Book(target, cfg, initialize=True, demo=True, clock=lambda: now)
    mint, a, b = address(20), address(1), address(2)
    rows = [event('a1', a, 1, now-40), event('a2', a, 2, now-30), event('b1', b, 1, now-20), event('b2', b, 2, now-10)]
    value = bundle(now, rows)
    try:
        from .replay import research
        history = bundle(now-60)
        history['opening_at'] = now-60-cfg['lookback_days']*86400
        history['events'], history['quotes'] = [], []
        for i in range(12):
            at = now-3600+i*240
            history['events'].extend([event('history-buy-'+str(i),a,2*i+1,at), event('history-sell-'+str(i),a,2*i+2,at+120,'sell','100')])
            history['quotes'].extend([quote(mint,at+5),quote(mint,at+125,'2')])
        history['quotes'].append(quote(mint,now-60,'2'))
        research_result = research(history, cfg)
        book.db.execute('INSERT INTO research VALUES(?,?,?)', ('synthetic-only',now-50,encode(research_result)))
        book.publish_shortlist([a, b], now-50, 'synthetic-only')
        book.ingest(value, now)
        for row in rows:
            q = quote(mint, now)
            try:
                book.buy(row['id'], q, now, evidence(book, row, q, now))
            except ValueError as exc:
                book.skip(row['id'], str(exc), now)
        rows.append(event('a-half', a, 3, now, 'sell', '100'))
        value['events'] = rows; value['through'] = now+5
        book.ingest(value, now+5)
        book.sell('a-half', quote(mint, now+5, '1.5'), now+5)
        book.clock = lambda: now+5
        path = report(book, clock=lambda: now+5)
        return {'label': 'SYNTHETIC DEMO — NOT MARKET PERFORMANCE', 'root': str(target), 'report': path, 'account_id': book.identity}
    finally:
        book.close()
