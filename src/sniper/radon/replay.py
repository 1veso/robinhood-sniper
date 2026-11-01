"""One chronological portfolio simulator using the actual Radon ledger and policy."""
from copy import deepcopy
from decimal import Decimal as D
import json
import statistics
import tempfile

from .book import Book
from .config import number, digest
from .source import validate_bundle
from .view import snapshot


def replay(bundle, cfg, leaders=None):
    validate_bundle(bundle, demo=True)
    leaders = sorted(set(leaders or bundle['openings']))
    value = deepcopy(bundle)
    value['openings'] = {k: v for k, v in value['openings'].items() if k in leaders}
    value['events'] = [r for r in value['events'] if r['leader'] in leaders]
    value['evidence_label'] = 'synthetic'  # Confine execution to a disposable ledger.
    all_events = value['events']
    begin, end = value['opening_at'], value['through']
    if not all_events or end <= begin:
        raise ValueError('No complete replay interval')
    quotes = bundle.get('quotes', [])
    def quote_at(mint, at):
        candidates = [q for q in quotes if q.get('mint') == mint and type(q.get('observed_at')) is int and q['observed_at'] <= at]
        if not candidates:
            raise ValueError('Archived quote missing; no leader-price fallback')
        return max(candidates, key=lambda q: q['observed_at'])
    with tempfile.TemporaryDirectory(prefix='radon-replay-') as root:
        book = Book(root, cfg, initialize=True, demo=True, clock=lambda: begin)
        try:
            # These are in-sample selected leaders, not a prospective shortlist.
            book.publish_shortlist(leaders, begin, 'conditional-in-sample')
            times = sorted({r['at'] for r in all_events} | {r['at']+cfg['execution_delay_seconds'] for r in all_events if r['at']+cfg['execution_delay_seconds'] <= end} | {end})
            for now in times:
                value['events'] = [r for r in all_events if r['at'] <= now]
                value['through'] = now
                book.ingest(value, now)
                # Extend research-only eligibility without pretending it was a historical selection.
                book.db.execute('UPDATE shortlists SET expires_at=?', (end+1,))
                for row in book.db.execute("SELECT * FROM events WHERE state='EXIT_PENDING' ORDER BY at,leader,sequence").fetchall():
                    if now < row['at']+cfg['execution_delay_seconds']:
                        continue
                    try:
                        p = book.position(row['leader'], row['mint'])
                        book.sell(row['id'], quote_at(row['mint'], now) if number(p['quantity']) else {}, now)
                    except (ValueError, KeyError, TypeError):
                        pass  # Unresolved exit remains visibly pending; no fictional sale.
                for row in book.db.execute("SELECT * FROM events WHERE state='BUY_PENDING' ORDER BY at,leader,sequence").fetchall():
                    if now < row['at']+cfg['execution_delay_seconds']:
                        continue
                    try:
                        book.buy(row['id'], quote_at(row['mint'], now), now, {'scope': 'Archived numerical gates only; X and historical safety evidence unavailable'}, replay=True)
                    except (ValueError, KeyError, TypeError) as exc:
                        book.skip(row['id'], str(exc), now)
            for mint in {r['mint'] for r in all_events}:
                try:
                    book.mark(quote_at(mint, end), end)
                except (ValueError, KeyError, TypeError):
                    pass
            result = snapshot(book.db, end)
            summary = result['summary']
            turnover = number(summary['buying_turnover'])
            pnl = D(summary['equity'])-number(cfg['starting_cash_usd']) if summary['equity'] is not None else None
            profit_by_mint = {}
            for row in book.db.execute("SELECT mint,payload FROM fills WHERE side='sell'"):
                profit_by_mint[row['mint']] = profit_by_mint.get(row['mint'], D(0)) + D(json.loads(row['payload'])['realized_pnl'])
            for p in result['positions']:
                if p['unrealized_pnl'] is not None:
                    profit_by_mint[p['mint']] = profit_by_mint.get(p['mint'], D(0)) + D(p['unrealized_pnl'])
            positive = sum((max(D(0), v) for v in profit_by_mint.values()), D(0))
            share = max((max(D(0), v) for v in profit_by_mint.values()), default=D(0))/positive if positive else D(0)
            return {'kind': 'historical_conditional_replay', 'input_label': bundle['evidence_label'], 'source_hash': digest(bundle),
                'from': begin, 'through': end, 'leaders': leaders, 'summary': summary,
                'copy_multiple': str(1+pnl/turnover) if pnl is not None and turnover else None,
                'largest_winner_share': str(share), 'one_coin_flag': share >= number(cfg['one_coin_share']),
                'buy_fills': book.db.execute("SELECT count(*) FROM fills WHERE side='buy'").fetchone()[0],
                'skipped': book.db.execute("SELECT count(*) FROM events WHERE state='SKIPPED'").fetchone()[0],
                'pending_exits': result['pending_exits'], 'profit_by_mint': {k: str(v) for k, v in profit_by_mint.items()},
                'coverage': 'Same fixed tickets, episode caps, cash, routes, fees, delay, partial exits and mark rules. Archived numeric gates only; X and safety eligibility not backfilled.',
                'selection_bias': 'In-sample leaderboard/candidate selection; not out-of-sample returns or a guaranteed executable outcome'}
        finally:
            book.close()


def holding_times(bundle, leader):
    balances = {m: number(q) for m, q in bundle['openings'][leader]['balances'].items()}
    first, closed = {}, []
    for row in sorted((r for r in bundle['events'] if r['leader'] == leader), key=lambda r: r['sequence']):
        mint, qty = row['mint'], number(row['quantity'])
        held = balances.get(mint, D(0))
        if row['side'] == 'buy':
            if held == 0:
                first[mint] = row['at']
            balances[mint] = held+qty
        elif row['side'] == 'sell':
            if qty > held:
                raise ValueError('Incomplete holding-time inventory')
            balances[mint] = held-qty
            if balances[mint] == 0 and mint in first:
                closed.append(row['at']-first.pop(mint))
        else:
            raise ValueError('Unsupported activity prevents complete holding-time reconstruction')
    return {'median_hold_seconds': statistics.median(closed) if closed else None, 'closed_episodes': len(closed)}


def research(bundle, cfg):
    validate_bundle(bundle, demo=True)
    ranking = []
    for leader in bundle['openings']:
        row = {'leader': leader, 'eligible': False}
        try:
            holds = holding_times(bundle, leader)
            result = replay(bundle, cfg, [leader])
            row.update(holds, replay=result)
            buys = sum(r['side'] == 'buy' for r in bundle['events'] if r['leader'] == leader)
            row['history_buys'] = buys
            kol = bundle.get('kols', {}).get(leader)
            kol_ok = not bundle['openings'][leader].get('is_kol') or (isinstance(kol, dict)
                and kol.get('verified') is True and kol.get('wallet_on_fomo') == leader == kol.get('wallet_on_kolscan')
                and isinstance(kol.get('source'), str) and bool(kol['source'])
                and type(kol.get('observed_at')) is int and 0 <= bundle['through']-kol['observed_at'] <= 86400)
            row['kol_identity'] = 'verified' if kol and kol_ok else 'not_required' if not bundle['openings'][leader].get('is_kol') else 'unknown_or_mismatch'
            row['eligible'] = bool(kol_ok and buys >= cfg['min_history_buys'] and holds['median_hold_seconds'] is not None
                and holds['median_hold_seconds'] >= cfg['min_median_hold_seconds'] and result['copy_multiple'] is not None
                and D(result['copy_multiple']) >= number(cfg['min_copy_multiple']) and result['pending_exits'] == 0)
        except (ValueError, KeyError, TypeError) as exc:
            row['incomplete_reason'] = str(exc)
        ranking.append(row)
    ranking.sort(key=lambda r: D(r.get('replay', {}).get('summary', {}).get('net_account_return') or '-999'), reverse=True)
    selected = [r['leader'] for r in ranking if r['eligible']][:cfg['max_shortlist']]
    overlap = {}
    for event in bundle['events']:
        if event['leader'] in selected and event['side'] == 'buy':
            overlap.setdefault(event['mint'], set()).add(event['leader'])
    return {'ranking': ranking, 'selected': selected, 'combined_portfolio': replay(bundle, cfg, selected) if selected else None,
        'overlap': {m: sorted(leaders) for m, leaders in overlap.items() if len(leaders)>1},
        'kind': 'historical_conditional_research', 'source_hash': digest(bundle), 'input_label': bundle['evidence_label'],
        'coverage': bundle['coverage'], 'selection_bias': 'Frozen only on publication; only later source events are prospective paper candidates'}
