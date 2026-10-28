"""Transactional account; JSON/CSV are recoverable views, never trading authority."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, default=str)


def number(value, name='number', *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'Invalid {name}')
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f'Invalid {name}')
    return value


def day(now):
    return datetime.fromtimestamp(now, ZoneInfo(config.TIMEZONE)).date().isoformat()


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def process_lock(path):
    """OS releases this lock after a crash; different jobs use different locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Another instance is running') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Book:
    def __init__(self, root=config.ROOT, *, initialize=False, mode='paper'):
        self.root = Path(root)
        self.runtime = self.root / 'runtime'
        path, marker = self.runtime / 'book.sqlite3', self.runtime / 'account.json'
        fingerprint = hashlib.sha256(encode(config.settings()).encode()).hexdigest()
        if initialize:
            with process_lock(self.runtime / 'initialize.lock'):
                if not path.exists() and not marker.exists():
                    # Existing exports are evidence of an account, never permission to reset it.
                    if any((self.root / p).exists() for p in (config.POSITIONS, config.TRADES, config.LOG)):
                        raise RuntimeError('Existing account exports require reconciliation')
                    if mode not in ('paper', 'manual') or not config.PAPER:
                        raise RuntimeError('Only paper and manual-record accounts are supported')
                    identity = {'account_id': str(uuid.uuid4()), 'mode': mode,
                                'config_sha256': fingerprint, 'created_at': int(time.time())}
                    # Marker first: interruption fails closed instead of creating a fresh account.
                    atomic(marker, encode(identity))
                    db = sqlite3.connect(path)
                    try:
                        db.executescript('''
                        PRAGMA journal_mode=WAL;
                        PRAGMA synchronous=FULL;
                        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE positions(id TEXT PRIMARY KEY,mint TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE fills(id TEXT PRIMARY KEY,at INTEGER NOT NULL,day TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE checks(mint TEXT PRIMARY KEY,at INTEGER NOT NULL,stage TEXT NOT NULL,verdict TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL);
                        CREATE TABLE events(id TEXT PRIMARY KEY,at INTEGER NOT NULL,message TEXT NOT NULL);
                        CREATE TABLE runs(id TEXT PRIMARY KEY,job TEXT NOT NULL,started INTEGER NOT NULL,finished INTEGER,status TEXT NOT NULL,detail TEXT NOT NULL);
                        ''')
                        db.execute('INSERT INTO metadata VALUES (?,?)', ('identity', encode(identity)))
                        db.commit()
                    finally:
                        db.close()
        if not path.is_file() or not marker.is_file():
            raise RuntimeError('Account missing; explicit initialization or backup recovery required')
        self.db = sqlite3.connect(f'file:{path.as_posix()}?mode=rw', uri=True, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        try:
            self.identity = json.loads(self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
            if self.identity != json.loads(marker.read_text(encoding='utf-8')):
                raise RuntimeError('Account identity mismatch')
            if self.identity['config_sha256'] != fingerprint or self.identity['mode'] != mode:
                raise RuntimeError('Account configuration/mode changed; review and migration required')
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Ledger integrity check failed')
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def positions(self, *, all_positions=False):
        rows = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM positions ORDER BY rowid')]
        return rows if all_positions else [p for p in rows if Decimal(p['remaining']) > 0]

    def fills(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM fills ORDER BY rowid')]

    def cost_policy(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='execution_cost_policy'").fetchone()
        return json.loads(row[0]) if row else None

    def enable_costs(self, now):
        from sniper.execution_costs import DEFAULT_POLICY
        with self.transaction():
            existing = self.cost_policy()
            if existing:
                return existing
            if self.identity['mode'] != 'paper' or self.fills() or (self.runtime / 'trial.json').exists():
                raise ValueError('Cost model activation requires an empty paper account before its trial')
            self.db.execute("INSERT INTO metadata VALUES ('execution_cost_policy',?)", (encode(DEFAULT_POLICY),))
            self.db.execute("INSERT INTO metadata VALUES ('execution_costs_started_at',?)", (str(now),))
            self._event('cost-model:' + DEFAULT_POLICY['model'], now,
                'RUG DESK — COST-AWARE PAPER MODEL ENABLED\nPaper fills now include assumed DEX/platform fees, liquidity impact, adverse slippage and current-SOL-valued network costs. Rent is reserved separately.\nReports include open losses, API spending and additional execution-cost stress. These remain estimates, not real fills. Live signing/broadcasting stays disabled; the unfunded xAI gate is unchanged.')
        atomic(self.runtime / 'execution-cost-policy.json', encode(DEFAULT_POLICY))
        return dict(DEFAULT_POLICY)

    def model_label(self):
        if self.cost_policy():
            from sniper.execution_costs import LABEL
            return LABEL
        return config.PAPER_FILL_MODEL

    def limits(self, mint, now):
        opened = self.positions()
        if any(p['mint'] == mint for p in opened):
            return 'duplicate open position'
        if len(opened) >= config.MAX_OPEN:
            return 'maximum open positions'
        fills = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM fills WHERE day=?', (day(now),))]
        if sum(f['kind'] == 'buy' for f in fills) >= config.MAX_ENTRIES_PER_DAY:
            return 'daily entry limit'
        if sum((Decimal(f['pnl']) for f in fills), Decimal(0)) <= -config.DAILY_LOSS_HALT_USD:
            return 'daily realized loss halt'
        if any(p['mint'] == mint and p.get('closed_at') is not None and
               now - p['closed_at'] < config.COOLDOWN_HOURS * 3600 for p in self.positions(all_positions=True)):
            return 're-entry cooldown'
        return None

    def claim(self, mint, now):
        with self.transaction():
            return bool(self.db.execute('INSERT OR IGNORE INTO checks VALUES (?,?,?,?,?,?)',
                (mint, now, 'interrupted', 'SKIP', 'Attempt claimed; completion not recorded', '{}')).rowcount)

    def checked(self, mint):
        return self.db.execute('SELECT 1 FROM checks WHERE mint=?', (mint,)).fetchone() is not None

    def finish(self, mint, now, stage, verdict, reason, payload):
        with self.transaction():
            self.db.execute('UPDATE checks SET at=?,stage=?,verdict=?,reason=?,payload=? WHERE mint=?',
                            (now, stage, verdict, reason, encode(payload), mint))

    def _save(self, p):
        self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?)', (p['id'], p['mint'], encode(p)))

    def _event(self, event_id, now, message):
        self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (event_id, now, message[:3800]))

    def event(self, event_id, now, message):
        with self.transaction():
            self._event(event_id, now, message)

    def _fill(self, fill):
        self.db.execute('INSERT INTO fills VALUES (?,?,?,?,?)',
                        (fill['id'], fill['at'], day(fill['at']), fill['kind'], encode(fill)))
        self._event('fill:' + fill['id'], fill['at'],
            f"RUG DESK {self.identity['mode'].upper()} {fill['kind'].upper()}\n"
            f"Mint: {fill['mint']}\nPool: {fill['pool']}\nQuantity: {fill['quantity']}\n"
            f"Price: ${fill['price']} | Realized P&L: ${fill['pnl']}\n{fill['reason']}\n"
            + (self.model_label() if self.identity['mode'] == 'paper' else 'User-confirmed fill; consult receipt evidence for chain verification.'))

    def open_paper(self, quote, judgment, now, *, entry_guard=None, entry_hook=None):
        if self.identity['mode'] != 'paper':
            raise RuntimeError('Paper entry cannot mutate a manual account')
        from rug_desk.market import check_limits, validate_quote
        validate_quote(quote, now)
        failure = check_limits(quote, now)
        if failure:
            raise ValueError(failure)
        if (judgment.get('verdict') != 'BUY'
                or not Decimal(str(config.MIN_CONFIDENCE)) <= number(judgment.get('confidence')) <= 1
                or judgment.get('validated') is not True):
            raise ValueError('Validated BUY required')
        execution = None
        if self.cost_policy():
            from sniper.execution_costs import simulate
            execution = simulate(self.cost_policy(), 'buy', quote, config.TICKET_USD, now)
        with self.transaction():
            if entry_guard:
                entry_guard()
            from sniper.jev_lifecycle import enabled_for
            if enabled_for(self.root.parent,self.identity['account_id']):
                from sniper.jev_admission import paper_guard
                paper_guard(self,judgment.get('jev_assessment_id'),quote['mint'],quote['pool'],now)
            failure = self.limits(quote['mint'], now)
            if failure:
                raise ValueError(failure)
            if execution:
                position = self._open({**quote, 'price': execution['fill_price']}, Decimal(execution['quantity']),
                    Decimal(execution['explicit_fee_usd']), now, 'paper:' + uuid.uuid4().hex, judgment, execution=execution)
            else:
                position = self._open(quote, config.TICKET_USD / number(quote['price'], positive=True),
                                     Decimal(0), now, 'paper:' + uuid.uuid4().hex, judgment)
            if entry_hook:
                entry_hook(position)
            return position

    def _open(self, quote, quantity, fee, now, fill_id, judgment, execution=None):
        price = number(quote['price'], positive=True)
        basis = (Decimal(execution.get('settlement_cash_usd', execution.get('reference_notional_usd')))
                 if execution else price * quantity + fee)
        p = {'id': uuid.uuid4().hex, 'mint': quote['mint'], 'pool': quote['pool'],
             'opened_at': now, 'original': str(quantity), 'remaining': str(quantity),
             'entry_price': str(price), 'entry_liquidity': str(number(quote['liquidity'], positive=True)),
             'cost_per_unit': str(price if execution else price + fee / quantity), 'peak': str(price), 'steps': 0,
             'remaining_cost_basis_usd': str(basis),
             'realized_pnl': str(-fee) if execution else '0', 'pending': None, 'judgment': judgment,
             'execution_model': execution['model'] if execution else 'legacy_or_manual',
             'rent_balance_lamports': execution.get('rent_capital_change_lamports', 0) if execution else 0,
             'rent_capital_reserved_usd': execution['rent_capital_reserved_usd'] if execution else '0'}
        self._save(p)
        self._fill({'id': fill_id, 'position_id': p['id'], 'mint': p['mint'], 'pool': p['pool'],
                    'kind': 'buy', 'at': now, 'quantity': str(quantity), 'price': str(price),
                    'fee': str(fee), 'pnl': str(-fee) if execution else '0', 'reason': 'entry', 'execution_costs': execution})
        return p

    def _sell(self, p, qty, price, fee, now, reason, fill_id, execution=None):
        remaining = Decimal(p['remaining'])
        if qty > remaining or qty <= 0:
            raise ValueError('Sale exceeds holdings or is nonpositive')
        remaining_basis = Decimal(p.get('remaining_cost_basis_usd', str(remaining * Decimal(p['cost_per_unit']))))
        allocated_basis = remaining_basis if qty == remaining else remaining_basis * qty / remaining
        proceeds = Decimal(execution['settlement_cash_usd']) if execution and 'settlement_cash_usd' in execution else qty * price
        pnl = proceeds - allocated_basis - fee
        p['remaining'] = str(remaining - qty)
        p['remaining_cost_basis_usd'] = str(remaining_basis - allocated_basis)
        p['realized_pnl'] = str(Decimal(p['realized_pnl']) + pnl)
        if execution and execution.get('model') == 'finalized_solana_receipt_v1':
            rent_balance = p.get('rent_balance_lamports', 0) + execution['rent_capital_change_lamports']
            if rent_balance < 0:
                raise ValueError('Rent refund exceeds the position reserve; reconcile the other token accounts')
            p['rent_balance_lamports'] = rent_balance
            p['rent_capital_reserved_usd'] = str(Decimal(rent_balance) / 10**9 * Decimal(execution['valuation']['sol_usd']))
        if remaining == qty:
            p['closed_at'] = now
        self._save(p)
        self._fill({'id': fill_id, 'position_id': p['id'], 'mint': p['mint'], 'pool': p['pool'],
                    'kind': 'sell', 'at': now, 'quantity': str(qty), 'price': str(price),
                    'fee': str(fee), 'pnl': str(pnl), 'reason': reason, 'execution_costs': execution})

    def confirmed_fill(self, fill_id, side, *, now, quantity, price, fee=0, position_id=None, quote=None, execution=None):
        """Records facts supplied by the operator. Contains no signing/execution code."""
        if self.identity['mode'] != 'manual' or side not in ('buy', 'sell'):
            raise ValueError('Confirmed fills require a separate manual-record account')
        if not isinstance(fill_id, str) or not 1 <= len(fill_id) <= 200:
            raise ValueError('A stable external fill ID is required')
        qty, price, fee = number(quantity, positive=True), number(price, positive=True), number(fee)
        with self.transaction():
            prior = self.db.execute('SELECT payload FROM fills WHERE id=?', (fill_id,)).fetchone()
            if prior:
                f = json.loads(prior[0])
                if (f['kind'], f['at'], Decimal(f['quantity']), Decimal(f['price']), Decimal(f['fee'])) != (side, now, qty, price, fee):
                    raise ValueError('Conflicting reuse of fill ID')
                if (side == 'sell' and f['position_id'] != position_id) or (side == 'buy' and (not quote or (f['mint'], f['pool']) != (quote['mint'], quote['pool']))):
                    raise ValueError('Fill ID belongs to another position')
                if f.get('execution_costs') != execution:
                    raise ValueError('Conflicting execution-cost evidence for fill ID')
                return f
            if side == 'buy':
                if any(p['mint'] == quote['mint'] for p in self.positions()):
                    raise ValueError('Duplicate manual open; reconcile actual fills first')
                return self._open({**quote, 'price': str(price)}, qty, fee, now, fill_id, {'manual_confirmation': True}, execution=execution)
            row = self.db.execute('SELECT payload FROM positions WHERE id=?', (position_id,)).fetchone()
            if not row:
                raise ValueError('Unknown position')
            p = json.loads(row[0])
            if now < p['opened_at']:
                raise ValueError('Sale predates entry')
            pending = p.get('pending')
            if pending:
                pending['confirmed'] = str(Decimal(pending.get('confirmed', '0')) + qty)
                if Decimal(pending['confirmed']) >= Decimal(pending['quantity']):
                    if pending['reason'].startswith('take_profit_'):
                        p['steps'] = max(p['steps'], pending['step'] + 1)
                    p['pending'] = None
            self._sell(p, qty, price, fee, now, pending['reason'] if pending else 'manual sale', fill_id, execution=execution)

    def confirmed_expense(self, expense_id, amount_usd, now, evidence):
        if self.identity['mode'] != 'manual':
            raise ValueError('Actual expenses require a separate manual account')
        amount = number(amount_usd)
        fill = {'id': expense_id, 'position_id': None, 'mint': 'native-network-fee', 'pool': '',
            'kind': 'expense', 'at': now, 'quantity': '0', 'price': '0', 'fee': str(amount),
            'pnl': str(-amount), 'reason': 'Finalized failed transaction fee; no swap fill', 'execution_costs': evidence}
        with self.transaction():
            old = self.db.execute('SELECT payload FROM fills WHERE id=?', (expense_id,)).fetchone()
            if old:
                if json.loads(old[0]) != fill:
                    raise ValueError('Conflicting expense ID')
                return
            self._fill(fill)

    def export(self):
        # Serializes exporters, preventing a late stale snapshot replacing a newer view.
        with self.transaction():
            atomic(self.root / config.POSITIONS, encode({'identity': self.identity, 'positions': self.positions()}))
            for name, rows, fields in (
                (config.TRADES, [{**f, 'execution_costs': encode(f.get('execution_costs'))} for f in self.fills()], ['id', 'position_id', 'mint', 'pool', 'kind', 'at', 'quantity', 'price', 'fee', 'pnl', 'reason', 'execution_costs']),
                (config.LOG, [dict(r) for r in self.db.execute('SELECT * FROM checks ORDER BY at,mint')], ['mint', 'at', 'stage', 'verdict', 'reason', 'payload'])):
                buffer = io.StringIO(newline='')
                writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
                atomic(self.root / name, buffer.getvalue())

    @contextmanager
    def run(self, job, now=None):
        run_id, started = uuid.uuid4().hex, int(time.time()) if now is None else now
        with self.transaction():
            self.db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)', (run_id, job, started, None, 'running', '{}'))
        detail = {}
        try:
            yield detail
        except Exception as exc:
            detail.update(error=type(exc).__name__, reason='Routine failed; inspect local diagnostics')
            status = 'error'
            raise
        else:
            status = 'ok'
        finally:
            with self.transaction():
                self.db.execute('UPDATE runs SET finished=?,status=?,detail=? WHERE id=?',
                    (int(time.time()), locals().get('status', 'interrupted'), encode(detail), run_id))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--enable-costs', action='store_true', help='Activate the cost scenario on an empty paper account')
    parser.add_argument('--manual-records', action='store_true')
    parser.add_argument('--confirm-fill', type=Path, help='JSON of an actual fill; bookkeeping only')
    args = parser.parse_args()
    if args.manual_records and args.root == config.ROOT:
        parser.error('Manual records require a different --root; never reuse the paper account')
    book = Book(args.root, initialize=args.initialize, mode='manual' if args.manual_records else 'paper')
    try:
        if args.enable_costs:
            book.enable_costs(int(time.time()))
        if args.confirm_fill:
            book.confirmed_fill(**json.loads(args.confirm_fill.read_text(encoding='utf-8')))
        book.export()
        print(encode({'identity': book.identity, 'open_positions': len(book.positions()), 'fills': len(book.fills())}))
    finally:
        book.close()


if __name__ == '__main__':
    main()
