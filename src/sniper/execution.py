"""Deterministic, durable execution authority and accounting.

This journal is deliberately separate from the paper ledger. A quote, signature
or submission is never a fill. Only reconciled canonical receipts change cash
and holdings. Native fees remain separate from base-token P&L unless separately
valued; this module never assumes that a stablecoin is worth one US dollar.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid


OPEN = ('QUEUED', 'PREPARING', 'PREPARED', 'SIGNING', 'SIGNED', 'SUBMITTED', 'UNKNOWN', 'WAITING_APPROVAL', 'INCLUDED')
SIGNED = ('SIGNED', 'SUBMITTED', 'UNKNOWN', 'INCLUDED')
TERMINAL = ('CONFIRMED', 'REVERTED', 'REJECTED', 'EXPIRED', 'REVIEW')
RESERVING = OPEN + ('REVIEW',)


class LeaseLost(RuntimeError):
    """A worker must not commit or send after another worker takes over."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def raw(value, name, minimum=0):
    if type(value) is int:
        return integer(value, name, minimum)
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise ValueError(f'{name} must be an unsigned integer amount')
    return integer(int(value), name, minimum)


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise ValueError('Invalid EVM address')
    return value.lower()


def tx_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise ValueError('Invalid transaction/block hash')
    return value.lower()


def policy_hash(policy):
    return digest({k: v for k, v in policy.items() if k != 'policy_hash'})


def validate_policy(policy):
    p = dict(policy)
    if not isinstance(p.get('account_id'), str) or not p['account_id'].strip():
        raise ValueError('Execution account identity is required')
    integer(p.get('chain_id'), 'chain_id', 1)
    p['wallet'] = address(p['wallet'])
    p['base_token'] = address(p['base_token'])
    for name in ('capital_raw', 'max_order_raw', 'max_total_exposure_raw', 'daily_loss_raw',
                 'max_gas_wei_day', 'max_tx_gas_wei'):
        p[name] = raw(p[name], name)
    for name, default, minimum in (('max_open_positions', 4, 1), ('max_entries_per_day', 6, 1),
            ('max_quote_age_seconds', 30, 1), ('max_evidence_age_seconds', 300, 1),
            ('minimum_buyers', 2, 2), ('buyer_window_seconds', 900, 1),
            ('cooldown_seconds', 86400, 0), ('rebroadcast_limit', 2, 0),
            ('rebroadcast_seconds', 30, 1), ('lease_seconds', 180, 30)):
        p[name] = integer(p.get(name, default), name, minimum)
    p.setdefault('min_liquidity_usd', '30000')
    liquidity = Decimal(str(p['min_liquidity_usd']))
    if not liquidity.is_finite() or liquidity < 30000:
        raise ValueError('Liquidity policy must retain the approved floor')
    for key in ('deployment_authorized', 'acceptance_mode'):
        if type(p.get(key, False)) is not bool:
            raise ValueError('Execution authority must be an explicit boolean')
        p.setdefault(key, False)
    if p['deployment_authorized'] and p['acceptance_mode']:
        raise ValueError('Acceptance authority cannot activate real funds')
    if p['max_order_raw'] > p['max_total_exposure_raw'] or p['max_total_exposure_raw'] > p['capital_raw']:
        raise ValueError('Execution allocation exceeds approved capital')
    if p['max_tx_gas_wei'] > p['max_gas_wei_day']:
        raise ValueError('Transaction gas budget exceeds daily budget')
    if p['deployment_authorized'] and (not p['capital_raw'] or p['wallet'] == '0x'+'0'*40):
        raise ValueError('Live deployment requires a funded-policy identity')
    mode=p.get('exit_settlement_mode','finalized_only')
    if mode not in ('finalized_only','confirmed_exit_management'):
        raise ValueError('Unsupported exit settlement mode')
    if mode=='confirmed_exit_management':
        if not isinstance(p.get('provisional_exit_review_reference'),str) or not p['provisional_exit_review_reference'].strip():
            raise ValueError('Provisional exit management requires an explicit review')
        if type(p.get('max_unfinalized_transactions'))is not int or not 1<=p['max_unfinalized_transactions']<=8:
            raise ValueError('Bounded unfinalized transaction capacity is required')
        if p.get('require_finalized') is not True:
            raise ValueError('Provisional management cannot weaken finalized accounting')
    p['policy_hash'] = policy_hash(p)
    return p


class ExecutionJournal:
    """One independent wallet/account/policy, protected by SQLite transactions."""

    validate_policy = staticmethod(validate_policy)
    address = staticmethod(address)
    tx_hash = staticmethod(tx_hash)
    block_hash = staticmethod(tx_hash)

    def __init__(self, path, policy, *, create=False):
        self.path = Path(path)
        self.policy = self.validate_policy(policy)
        # Signing grants are process-local and monotonic. A persisted prepared
        # transaction alone cannot keep a dead/expired worker's signer active.
        self._signing_deadlines = {}
        self._broadcast_grants = {}
        if not create and not self.path.is_file():
            raise ValueError('Established execution journal is missing')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            existing_tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            new_journal = not existing_tables
            if existing_tables and ('execution_metadata' not in existing_tables or
                    any(not name.startswith('execution_') and name != 'sqlite_sequence' for name in existing_tables)):
                raise ValueError('Refusing to initialize execution in another account database')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            if not create and not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_metadata'").fetchone():
                raise ValueError('Not an established execution journal')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS execution_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_orders(
                    id TEXT PRIMARY KEY,intent TEXT NOT NULL,intent_digest TEXT NOT NULL,
                    status TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                    prepared TEXT,signed TEXT,tx_hash TEXT UNIQUE,nonce INTEGER,
                    reservation_raw TEXT NOT NULL DEFAULT '0',gas_reservation_wei TEXT NOT NULL DEFAULT '0',
                    attempts INTEGER NOT NULL DEFAULT 0,last_broadcast_at INTEGER,parent_id TEXT,
                    receipt TEXT,receipt_digest TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS execution_positions(
                    position_id TEXT PRIMARY KEY,token TEXT NOT NULL,pool TEXT NOT NULL,
                    original_qty_raw TEXT NOT NULL,remaining_qty_raw TEXT NOT NULL,
                    cost_basis_raw TEXT NOT NULL,entry_cost_raw TEXT NOT NULL,
                    opened_at INTEGER NOT NULL,peak_price TEXT NOT NULL,take_profit_done INTEGER NOT NULL DEFAULT 0,
                    closed_at INTEGER,realized_base_raw TEXT NOT NULL DEFAULT '0');
                CREATE TABLE IF NOT EXISTS execution_fills(
                    tx_hash TEXT PRIMARY KEY,order_id TEXT NOT NULL,side TEXT NOT NULL,
                    input_raw TEXT NOT NULL,output_raw TEXT NOT NULL,fee_wei TEXT NOT NULL,
                    realized_base_raw TEXT NOT NULL,block_number INTEGER NOT NULL,block_hash TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,success INTEGER NOT NULL,receipt_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,order_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_notices(
                    id TEXT PRIMARY KEY,at INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,ack_at INTEGER);
            ''')
            with self.transaction():
                saved = self._get('policy_hash')
                if saved:
                    if saved != self.policy['policy_hash']:
                        raise ValueError('Execution account/policy binding differs; migration is required')
                elif create and new_journal:
                    self._set('policy_hash', self.policy['policy_hash'])
                    self._set('policy', encode(self.policy))
                    self._set('cash_raw', self.policy['capital_raw'])
                    self._set('entry_halt', '')
                    self._set('full_halt', '')
                    self._set('schema_version', '1')
                    self._audit(None, 'initialized', {'account_id': self.policy['account_id'], 'acceptance': self.policy['acceptance_mode']}, int(time.time()))
                else:
                    raise ValueError('Execution identity missing; established account cannot be reset')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _get(self, key):
        r = self.db.execute('SELECT value FROM execution_metadata WHERE key=?', (key,)).fetchone()
        return r[0] if r else None

    def _set(self, key, value):
        self.db.execute('INSERT INTO execution_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def _audit(self, order_id, kind, payload, now):
        self.db.execute('INSERT INTO execution_audit(at,order_id,kind,payload) VALUES(?,?,?,?)', (now, order_id, kind, encode(payload)))

    def _notice(self, order_id, kind, payload, now):
        key = f'{order_id}:{kind}'
        self.db.execute('INSERT OR IGNORE INTO execution_notices VALUES(?,?,?,?,NULL)', (key, now, kind, encode(payload)))

    @staticmethod
    def _order(row):
        if row is None:
            return None
        result = dict(row)
        for name in ('intent', 'prepared', 'signed', 'receipt'):
            result[name] = json.loads(result[name]) if result[name] else None
        return result

    def order(self, order_id):
        return self._order(self.db.execute('SELECT * FROM execution_orders WHERE id=?', (order_id,)).fetchone())

    def orders(self):
        return [self._order(r) for r in self.db.execute('SELECT * FROM execution_orders ORDER BY created_at,id')]

    def positions(self):
        return [{**dict(r), 'take_profit_done': bool(r['take_profit_done'])}
                for r in self.db.execute("SELECT * FROM execution_positions WHERE closed_at IS NULL ORDER BY opened_at,position_id")]

    def management_positions(self):
        """Operational confirmed holdings; never used as finalized P&L/cash."""
        positions={p['position_id']:dict(p) for p in self.positions()}
        if self.policy.get('exit_settlement_mode')!='confirmed_exit_management':return list(positions.values())
        included=sorted((o for o in self.orders() if o['status']=='INCLUDED'),key=lambda o:o['nonce'])
        for o in included:
            result=o['receipt'];i=o['intent']
            if not result or result.get('outcome')!='confirmed' or i.get('kind','swap')!='swap':continue
            f=result['fill'];amount_in,amount_out=int(f['input_raw']),int(f['output_raw'])
            if i['side']=='buy':
                positions[o['id']]={'position_id':o['id'],'token':i['token_out'],'pool':i['pool'],
                    'original_qty_raw':str(amount_out),'remaining_qty_raw':str(amount_out),'cost_basis_raw':str(amount_in),
                    'entry_cost_raw':str(amount_in),'opened_at':result['receipt']['observed_at'],
                    'peak_price':self._get('provisional_peak:'+o['id']) or str(Decimal(amount_in)/amount_out),
                    'take_profit_done':False,'provisional':True}
            else:
                p=positions.get(i['position_id'])
                if not p or amount_in>int(p['remaining_qty_raw']):raise ValueError('Provisional sale exceeds derived holdings')
                qty,basis=int(p['remaining_qty_raw']),int(p['cost_basis_raw'])
                p.update(remaining_qty_raw=str(qty-amount_in),cost_basis_raw=str(basis-(basis if amount_in==qty else basis*amount_in//qty)),
                         take_profit_done=p['take_profit_done'] or i.get('reason')=='take_profit',provisional=True)
                if amount_in==qty:positions.pop(i['position_id'])
        return list(positions.values())

    def notices(self):
        return [{**dict(r), 'payload': json.loads(r['payload'])}
                for r in self.db.execute('SELECT * FROM execution_notices WHERE ack_at IS NULL ORDER BY at,id')]

    def acknowledge_notice(self, notice_id, now):
        integer(now, 'now')
        with self.transaction():
            self.db.execute('UPDATE execution_notices SET ack_at=COALESCE(ack_at,?) WHERE id=?', (now, notice_id))

    def snapshot(self):
        orders = self.orders()
        fills = [dict(r) for r in self.db.execute('SELECT * FROM execution_fills ORDER BY observed_at,tx_hash')]
        return {'account_id': self.policy['account_id'], 'policy_hash': self.policy['policy_hash'],
                'chain_id': self.policy['chain_id'], 'wallet': self.policy['wallet'],
                'execution_target': 'automatic', 'deployment_authorized': self.policy['deployment_authorized'],
                'acceptance_mode': self.policy['acceptance_mode'], 'cash_raw': self._get('cash_raw'),
                'reserved_base_raw': str(sum(int(o['reservation_raw']) for o in orders if o['status'] in RESERVING)),
                'reserved_gas_wei': str(sum(int(o['gas_reservation_wei']) for o in orders if o['status'] in RESERVING)),
                'native_fees_wei': str(sum(int(f['fee_wei']) for f in fills)),
                'realized_base_before_native_fees_raw': str(sum(int(f['realized_base_raw']) for f in fills)),
                'pnl_complete': False, 'cost_scope': 'Base-token deltas and native fees separately; no assumed USD valuation',
                'positions': self.positions(), 'management_positions': self.management_positions(),
                'provisional_transaction_count':sum(o['status']=='INCLUDED' for o in orders),
                'exit_settlement_mode':self.policy.get('exit_settlement_mode','finalized_only'),
                'order_counts': {s: sum(o['status'] == s for o in orders) for s in OPEN+TERMINAL},
                'entry_halt': self._get('entry_halt') or None, 'full_halt': self._get('full_halt') or None,
                'fill_count': sum(f['success'] == 1 and f['side'] in ('buy', 'sell') for f in fills)}

    def set_halt(self, kind, reason='operator policy halt', now=None):
        now = int(time.time()) if now is None else integer(now, 'now')
        if kind not in ('entries', 'all', None):
            raise ValueError('Halt must be entries, all, or None')
        with self.transaction():
            self._set('entry_halt', reason if kind else '')
            self._set('full_halt', reason if kind == 'all' else '')
            self._audit(None, 'halt', {'kind': kind, 'reason': reason}, now)

    def update_position_peak(self, position_id, price, at):
        integer(at, 'at')
        try:
            value = Decimal(str(price))
        except InvalidOperation:
            raise ValueError('Invalid peak price') from None
        if not value.is_finite() or value <= 0:
            raise ValueError('Invalid peak price')
        with self.transaction():
            row = self.db.execute('SELECT peak_price FROM execution_positions WHERE position_id=? AND closed_at IS NULL', (position_id,)).fetchone()
            if row and value > Decimal(row[0]):
                self.db.execute('UPDATE execution_positions SET peak_price=? WHERE position_id=?', (str(value), position_id))
                self._audit(position_id, 'peak', {'price': str(value)}, at)
            elif not row:
                position=next((p for p in self.management_positions() if p['position_id']==position_id),None)
                if position and position.get('provisional') and value>Decimal(position['peak_price']):
                    self._set('provisional_peak:'+position_id,str(value))
                    self._audit(position_id,'provisional_peak',{'price':str(value)},at)

    def _validate_intent(self, intent, now):
        p = self.policy
        i = json.loads(encode(intent))
        if not isinstance(i.get('id'), str) or not 1 <= len(i['id']) <= 240:
            raise ValueError('Intent identity required')
        for name in ('account_id', 'chain_id', 'wallet', 'policy_hash'):
            if i.get(name) != p[name]:
                raise ValueError('Intent execution authority mismatch')
        if i.get('kind', 'swap') != 'swap' or i.get('side') not in ('buy', 'sell'):
            raise ValueError('Only strategy swap intents may be queued directly')
        for name in ('token_in', 'token_out', 'pool'):
            i[name] = address(i[name])
        if i['token_in'] == i['token_out'] or i['token_in' if i['side'] == 'buy' else 'token_out'] != p['base_token']:
            raise ValueError('Intent must use the bound base token')
        for name in ('amount_in_raw', 'min_output_raw', 'quoted_output_raw'):
            i[name] = str(raw(i[name], name, 1))
        if int(i['min_output_raw']) > int(i['quoted_output_raw']):
            raise ValueError('Minimum output exceeds quote')
        integer(i['quote_at'], 'quote_at')
        integer(i['deadline'], 'deadline')
        if not 0 <= now-i['quote_at'] <= p['max_quote_age_seconds'] or not now < i['deadline'] <= now+300:
            raise ValueError('Stale quote or invalid execution deadline')
        if i['side'] == 'buy':
            e = i.get('entry_evidence', {})
            if e.get('safety_passed') is not True or e.get('required_provider_response') is not True:
                raise ValueError('Current mandatory entry safety evidence is missing')
            if type(e.get('safety_at')) is not int or not 0 <= now-e['safety_at'] <= p['max_evidence_age_seconds']:
                raise ValueError('Entry safety evidence is stale')
            try:
                liquidity = Decimal(str(e.get('liquidity_usd')))
            except InvalidOperation:
                raise ValueError('Missing liquidity evidence') from None
            if not liquidity.is_finite() or liquidity < Decimal(str(p['min_liquidity_usd'])):
                raise ValueError('Insufficient liquidity evidence')
            buyers, transactions = set(), set()
            for b in e.get('buyers', []):
                if (type(b.get('observed_at')) is not int or not 0 <= now-b['observed_at'] <= p['buyer_window_seconds']
                        or address(b.get('pool')) != i['pool'] or address(b.get('token')) != i['token_out']):
                    raise ValueError('Buyer evidence does not match current token/pool/window')
                buyers.add(address(b['wallet'])); transactions.add(tx_hash(b['tx_hash']))
            if min(len(buyers), len(transactions)) < p['minimum_buyers']:
                raise ValueError('Distinct buyer/transaction quorum is missing')
        return i

    @staticmethod
    def day_start(now):
        return now//86400*86400

    def _entry_limits(self, intent, now, exclude_id=None):
        p = self.policy
        if self._get('entry_halt') or self._get('full_halt'):
            raise ValueError('New entries are halted')
        if any(o['status']=='INCLUDED' and not (o['parent_id']==intent['id'] and o['intent'].get('kind') in ('approval','approval_reset')) for o in self.orders()):
            raise ValueError('Unfinalized exposure blocks additional entries')
        amount = int(intent['amount_in_raw'])
        if not 0 < amount <= p['max_order_raw']:
            raise ValueError('Order exceeds approved amount')
        pending = [o for o in self.orders() if o['status'] in RESERVING and not o['parent_id'] and o['intent']['side'] == 'buy' and o['id'] != exclude_id]
        positions = self.positions()
        if len(positions)+len(pending) >= p['max_open_positions']:
            raise ValueError('Position limit reached')
        asset = intent['token_out']
        if any(x['token'] == asset for x in positions) or any(x['intent']['token_out'] == asset for x in pending):
            raise ValueError('Duplicate position')
        closed = self.db.execute('SELECT MAX(closed_at) FROM execution_positions WHERE token=?', (asset,)).fetchone()[0]
        if closed is not None and now-closed < p['cooldown_seconds']:
            raise ValueError('Re-entry cooldown')
        reserved = sum(int(o['reservation_raw']) for o in pending)
        if amount+reserved > int(self._get('cash_raw')):
            raise ValueError('Insufficient unreserved capital')
        exposure = sum(int(x['cost_basis_raw']) for x in positions)
        if amount+reserved+exposure > p['max_total_exposure_raw']:
            raise ValueError('Account exposure limit')
        day = self.day_start(now)
        entries = self.db.execute("SELECT COUNT(*) FROM execution_fills WHERE observed_at>=? AND side='buy' AND success=1", (day,)).fetchone()[0]
        if entries+len(pending) >= p['max_entries_per_day']:
            raise ValueError('Daily entry limit')
        pnl = sum(int(r[0]) for r in self.db.execute('SELECT realized_base_raw FROM execution_fills WHERE observed_at>=?', (day,)))
        if p['daily_loss_raw'] == 0 or pnl <= -p['daily_loss_raw']:
            raise ValueError('Daily realized base loss limit')

    def enqueue(self, intent, now=None):
        now = int(time.time()) if now is None else integer(now, 'now')
        # Idempotent replay is accepted even after its quote expires.
        original = self.order(intent.get('id'))
        if original:
            normalized = dict(intent)
            for key in ('amount_in_raw','min_output_raw','quoted_output_raw'):
                if key in normalized: normalized[key] = str(raw(normalized[key], key, 1))
            for key in ('token_in','token_out','pool'):
                if key in normalized: normalized[key] = self.address(normalized[key])
            if digest(normalized) != original['intent_digest']:
                raise ValueError('Conflicting intent replay')
            return original
        i = self._validate_intent(intent, now)
        with self.transaction():
            existing = self.order(i['id'])
            if existing:
                if existing['intent_digest'] != digest(i): raise ValueError('Conflicting intent replay')
                return existing
            if self._get('full_halt'):
                raise ValueError('All execution is halted')
            reserve = 0
            if i['side'] == 'buy':
                self._entry_limits(i, now)
                reserve = int(i['amount_in_raw'])
            else:
                position = next((p for p in self.management_positions() if p['position_id']==i.get('position_id')),None)
                if not position or i['token_in'] != position['token'] or i['pool'] != position['pool']:
                    raise ValueError('Exit does not match owned position')
                reserved_sells = sum(int(o['intent']['amount_in_raw']) for o in self.orders()
                                     if o['status'] in RESERVING and o['status']!='INCLUDED' and not o['parent_id'] and o['intent'].get('position_id') == i['position_id'])
                if int(i['amount_in_raw'])+reserved_sells > int(position['remaining_qty_raw']):
                    raise ValueError('Exit exceeds unreserved holdings')
            self.db.execute('INSERT INTO execution_orders(id,intent,intent_digest,status,created_at,updated_at,reservation_raw) VALUES(?,?,?,?,?,?,?)',
                            (i['id'], encode(i), digest(i), 'QUEUED', now, now, str(reserve)))
            self._audit(i['id'], 'reserved', {'base_raw': str(reserve), 'side': i['side']}, now)
        return self.order(i['id'])

    def signing_authorized(self, prepared):
        """Signer callback: exact durable reservation, transaction and gas ceiling."""
        order = self.order(prepared.get('order_id'))
        if not order or order['status'] != 'SIGNING' or order['prepared'] != prepared:
            return False
        if prepared.get('policy_hash') != self.policy['policy_hash'] or self._get('full_halt'):
            return False
        if (prepared.get('worker_fence') != self._get('worker_owner')
                or str(prepared.get('lease_epoch')) != self._get('worker_lease_epoch')):
            return False
        grant = (order['id'], prepared.get('worker_fence'), prepared.get('lease_epoch'))
        if time.monotonic() >= self._signing_deadlines.get(grant, 0):
            return False
        if order['intent']['side'] == 'buy' and self._get('entry_halt'):
            return False
        if self.policy['deployment_authorized'] and self.policy['wallet'] != '0x'+'0'*40:
            return True
        return self.policy['acceptance_mode']

    def broadcast_authorized(self, signed):
        """Last-moment transport callback after slow RPC validation.

        The current worker grants sending the exact persisted bytes. A restart
        may send an older signing worker's bytes, but cannot reuse its authority.
        """
        identity = signed.get('tx_hash') if isinstance(signed, dict) else None
        grant = self._broadcast_grants.get(identity)
        if not grant:
            return False
        owner, epoch, deadline = grant
        if (time.monotonic() >= deadline or self._get('worker_owner') != owner
                or self._get('worker_lease_epoch') != str(epoch) or self._get('full_halt')):
            return False
        row = self.db.execute('SELECT * FROM execution_orders WHERE tx_hash=?', (identity,)).fetchone()
        order = self._order(row)
        if not order or order['status'] not in SIGNED or order['signed'] != signed:
            return False
        if order['intent']['side'] == 'buy' and self._get('entry_halt'):
            return False
        return self.policy['deployment_authorized'] or self.policy['acceptance_mode']

    def _status(self, order_id, status, now, error=None, *, notify=True):
        self.db.execute('UPDATE execution_orders SET status=?,updated_at=?,error=? WHERE id=?', (status, now, error, order_id))
        self._audit(order_id, status.lower(), {'error': error}, now)
        if notify and status in ('UNKNOWN', 'REVIEW', 'REVERTED', 'REJECTED'):
            self._notice(order_id, status.lower(), {'order_id': order_id, 'status': status, 'error': error}, now)

    def _settled(self, order, result, now):
        """Chain-specific metadata hook inside the atomic settlement transaction."""

    def expire_signed(self, order, evidence, now, fence):
        raise ValueError('This chain has no positive signed-expiry proof implementation')

    def record_inclusion(self,order,result,now,fence):
        """Persist verified provisional flows without posting cash, fills or P&L."""
        r=result.get('receipt') or {};f=result.get('fill') or {};i=order['intent']
        if (self.policy.get('exit_settlement_mode')!='confirmed_exit_management' or r.get('canonical') is not True
                or r.get('finalized') is not False or result.get('outcome') not in ('confirmed','reverted')
                or self.tx_hash(r.get('tx_hash'))!=order['tx_hash']):
            raise ValueError('Unreviewed or invalid provisional receipt')
        self.block_hash(r['block_hash']);integer(r['block_number'],'block_number')
        integer(r['observed_at'],'observed_at')
        if integer(r.get('confirmation_depth'),'confirmation_depth')<self.policy['confirmations']:
            raise ValueError('Provisional receipt depth insufficient')
        amount_in,amount_out=raw(f.get('input_raw',0),'input_raw'),raw(f.get('output_raw',0),'output_raw')
        if raw(f.get('fee_wei'),'fee_wei')>int(order['gas_reservation_wei']):raise ValueError('Provisional expense exceeds reserve')
        if result['outcome']=='confirmed' and i.get('kind','swap')=='swap':
            if (amount_in!=int(i['amount_in_raw']) or amount_out<int(i['min_output_raw'])
                    or f.get('wallet')!=self.policy['wallet'] or f.get('token_in')!=i['token_in'] or f.get('token_out')!=i['token_out']):
                raise ValueError('Provisional flow identity/amount mismatch')
        elif amount_in or amount_out:raise ValueError('Non-swap outcome cannot create holdings')
        # Depth naturally changes; block, outcome and flows must remain identical.
        identity=lambda v: (v['receipt']['block_hash'],v['receipt']['block_number'],v['outcome'],v['fill'])
        if order['status']=='INCLUDED' and identity(order['receipt'])!=identity(result):
            raise ValueError('Provisional outcome changed')
        with self.transaction():
            fence()
            self.db.execute('UPDATE execution_orders SET receipt=?,receipt_digest=? WHERE id=?',(encode(result),digest(result),order['id']))
            self._status(order['id'],'INCLUDED',now,notify=False)
            if order['status']!='INCLUDED':
                self._notice(order['id'],'provisional_inclusion',{'order_id':order['id'],'tx_hash':order['tx_hash'],
                    'scope':'Verified confirmed flows for exit management only; no finalized fill or P&L'},now)
            if order['parent_id'] and result['outcome']=='confirmed':
                if self.order(order['parent_id'])['status']=='WAITING_APPROVAL':
                    self._status(order['parent_id'],'QUEUED',now,notify=False)

    def _finalize(self, order, result, now, *, fence=None):
        receipt = result.get('receipt') or {}
        fill = result.get('fill') or {}
        identity = self.tx_hash(receipt.get('tx_hash', order['tx_hash']))
        block_hash = self.block_hash(receipt['block_hash'])
        block_number = integer(receipt['block_number'], 'block_number')
        at = integer(receipt.get('observed_at', now), 'observed_at')
        if identity != order['tx_hash'] or result.get('status') not in ('confirmed', 'reverted'):
            raise ValueError('Receipt identity/outcome mismatch')
        if order['status']=='INCLUDED':
            previous=order['receipt']
            fields=('tx_hash','block_hash','block_number')
            if (any(previous['receipt'].get(k)!=receipt.get(k) for k in fields)
                    or previous['outcome']!=result['status'] or previous['fill']!=fill):
                raise ValueError('Final receipt conflicts with provisional inclusion')
        # Adapter contract requires explicit canonical, depth-finalized evidence.
        if receipt.get('canonical') is not True or not (receipt.get('finalized') is True or receipt.get('finality') == 'finalized'):
            raise ValueError('Receipt lacks canonical finality evidence')
        success = result['status'] == 'confirmed'
        fee = raw(fill.get('fee_wei', receipt.get('fee_wei')), 'fee_wei')
        if fee > int(order['gas_reservation_wei']):
            raise ValueError('Actual native fee exceeds signed reservation')
        amount_in = raw(fill.get('input_raw', 0), 'input_raw')
        amount_out = raw(fill.get('output_raw', 0), 'output_raw')
        intent = order['intent']
        approval = intent.get('kind') in ('approval', 'approval_reset')
        if not success and (amount_in or amount_out):
            raise ValueError('Reverted transaction cannot have a swap fill')
        if success and not approval:
            if (self.address(fill['wallet']) != self.policy['wallet'] or self.address(fill['token_in']) != intent['token_in']
                    or self.address(fill['token_out']) != intent['token_out']):
                raise ValueError('Receipt token/account mismatch')
            requested = int(intent['amount_in_raw'])
            if not 0 < amount_in <= requested or amount_out <= 0:
                raise ValueError('Invalid actual swap amounts')
            if amount_out*requested < int(intent['min_output_raw'])*amount_in:
                raise ValueError('Actual output violates signed bound')
        elif approval and (amount_in or amount_out):
            raise ValueError('Approval is not a swap')
        evidence_digest = digest(result)
        with self.transaction():
            if fence is not None:
                fence()
            old = self.db.execute('SELECT receipt_digest FROM execution_fills WHERE tx_hash=?', (identity,)).fetchone()
            if old:
                if old[0] != evidence_digest:
                    raise ValueError('Conflicting receipt replay')
                return
            cash = int(self._get('cash_raw')); realized = 0
            if success and not approval:
                if intent['side'] == 'buy':
                    if amount_in > cash:
                        raise ValueError('Actual spend exceeds execution cash')
                    cash -= amount_in
                    self.db.execute('INSERT INTO execution_positions(position_id,token,pool,original_qty_raw,remaining_qty_raw,cost_basis_raw,entry_cost_raw,opened_at,peak_price) VALUES(?,?,?,?,?,?,?,?,?)',
                        (order['id'], intent['token_out'], intent['pool'], str(amount_out), str(amount_out), str(amount_in), str(amount_in), at, str(Decimal(amount_in)/Decimal(amount_out))))
                    peak=self._get('provisional_peak:'+order['id'])
                    if peak:self.db.execute('UPDATE execution_positions SET peak_price=? WHERE position_id=?',(peak,order['id']))
                else:
                    position = self.db.execute('SELECT * FROM execution_positions WHERE position_id=? AND closed_at IS NULL', (intent['position_id'],)).fetchone()
                    if not position or amount_in > int(position['remaining_qty_raw']):
                        raise ValueError('Canonical sale exceeds recorded holdings')
                    qty, basis = int(position['remaining_qty_raw']), int(position['cost_basis_raw'])
                    allocated = basis if amount_in == qty else basis*amount_in//qty
                    realized = amount_out-allocated; cash += amount_out
                    tp = bool(position['take_profit_done']) or (intent.get('reason') == 'take_profit' and amount_in == int(intent['amount_in_raw']))
                    self.db.execute('UPDATE execution_positions SET remaining_qty_raw=?,cost_basis_raw=?,realized_base_raw=?,take_profit_done=?,closed_at=? WHERE position_id=?',
                        (str(qty-amount_in), str(basis-allocated), str(int(position['realized_base_raw'])+realized), int(tp), at if qty == amount_in else None, intent['position_id']))
                self._set('cash_raw', cash)
            self.db.execute('INSERT INTO execution_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, order['id'], intent.get('kind') if approval else intent['side'], str(amount_in), str(amount_out), str(fee), str(realized), block_number, block_hash, at, int(success), evidence_digest))
            self.db.execute('UPDATE execution_orders SET receipt=?,receipt_digest=? WHERE id=?', (encode(result), evidence_digest, order['id']))
            self._settled(order, result, now)
            self._status(order['id'], 'CONFIRMED' if success else 'REVERTED', now)
            self._notice(order['id'], 'receipt', {'order_id': order['id'], 'tx_hash': identity, 'success': success,
                'kind': intent.get('kind', 'swap'), 'side': intent['side'], 'input_raw': str(amount_in), 'output_raw': str(amount_out),
                'fee_wei': str(fee), 'realized_base_before_native_fees_raw': str(realized), 'acceptance': self.policy['acceptance_mode']}, now)
            if order['parent_id']:
                parent=self.order(order['parent_id'])
                if parent['status']=='WAITING_APPROVAL':
                    self._status(order['parent_id'], 'QUEUED' if success else 'REJECTED', now,
                                 None if success else 'approval transaction reverted')


class AutomaticExecutor:
    """One bounded step per tick; a real backend is never enabled implicitly."""

    def __init__(self, journal, backend):
        self.journal = journal
        self.backend = backend
        self.owner = str(uuid.uuid4())
        self.epoch = None
        self.tick_started = None
        self.tick_now = None

    def _authorized(self):
        p = self.journal.policy
        if p['acceptance_mode']:
            return getattr(self.backend, 'is_test', False) is True
        return p['deployment_authorized'] is True and getattr(self.backend, 'is_test', False) is not True

    def _lease(self, now):
        j = self.journal
        with j.transaction():
            until = int(j._get('worker_lease_until') or 0)
            owner = j._get('worker_owner')
            if until > now and owner != self.owner:
                return False
            j._set('worker_owner', self.owner); j._set('worker_lease_until', now+j.policy['lease_seconds'])
            self.epoch = int(j._get('worker_lease_epoch') or 0)+1
            j._set('worker_lease_epoch', self.epoch)
            self.tick_started = time.monotonic(); self.tick_now = now
        return True

    def _fence(self):
        j = self.journal
        effective_now = self.tick_now + int(time.monotonic()-self.tick_started)
        if (j._get('worker_owner') != self.owner or j._get('worker_lease_epoch') != str(self.epoch)
                or int(j._get('worker_lease_until') or 0) <= effective_now):
            raise LeaseLost('Execution worker lease expired or was superseded')

    def _release(self):
        j = self.journal
        with j.transaction():
            if j._get('worker_owner') == self.owner and j._get('worker_lease_epoch') == str(self.epoch):
                j._set('worker_lease_until', 0)
            for grant in list(j._signing_deadlines):
                if grant[1] == self.owner:
                    j._signing_deadlines.pop(grant, None)
            for identity, grant in list(j._broadcast_grants.items()):
                if grant[0] == self.owner:
                    j._broadcast_grants.pop(identity, None)

    def tick(self, now=None):
        now = int(time.time()) if now is None else integer(now, 'now')
        j = self.journal
        if not self._authorized():
            return {'status': 'DISABLED', 'reason': 'No approved execution deployment; signer and broadcaster were not called'}
        if not self._lease(now):
            return {'status': 'BUSY'}
        try:
            signed = sorted((o for o in j.orders() if o['status'] in SIGNED),key=lambda o:o['nonce'])
            if signed:
                if j.policy.get('exit_settlement_mode')!='confirmed_exit_management':
                    return self._reconcile(signed[0], now)
                progress=None
                for order in signed:
                    progress=self._reconcile(order,now)
                    if progress['status'] not in ('INCLUDED','CONFIRMED','REVERTED'):return progress
                pending=[o for o in j.orders() if o['status']=='INCLUDED']
                if len(pending)>=j.policy['max_unfinalized_transactions']:
                    return {'status':'WAITING_FINALITY','reason':'Unfinalized capacity is fully reserved'}
            if j._get('full_halt'):
                return {'status': 'HALTED', 'reason': j._get('full_halt')}
            if any(o['status'] == 'REVIEW' for o in j.orders()):
                return {'status': 'REVIEW', 'reason': 'Unresolved execution review retains exposure and blocks new signing'}
            # A dead worker cannot have broadcast a PREPARING/PREPARED order.
            # SIGNING is held for review: no invented signature or new nonce.
            interrupted = [o for o in j.orders() if o['status'] == 'SIGNING']
            if interrupted:
                with j.transaction():
                    j._status(interrupted[0]['id'], 'REVIEW', now, 'signing process interrupted; reconcile signer service before recovery')
                    j._set('full_halt', 'interrupted signing requires recovery')
                return {'status': 'REVIEW', 'order_id': interrupted[0]['id']}
            candidates = [o for o in j.orders() if o['status'] in ('QUEUED','PREPARING','PREPARED')]
            candidates.sort(key=lambda o: (o['intent']['side'] != 'sell', o['created_at'], o['id']))
            included=[o for o in j.orders() if o['status']=='INCLUDED']
            if included:
                candidates=[o for o in candidates if o['intent']['side']=='sell' or all(
                    a['parent_id']==o['id'] and a['intent'].get('kind') in ('approval','approval_reset') for a in included)]
            if j._get('entry_halt'):
                candidates = [o for o in candidates if o['intent']['side'] == 'sell']
            if not candidates:
                return {'status': 'IDLE'}
            order = candidates[0]
            if order['intent'].get('kind', 'swap') == 'swap' and callable(getattr(self.backend, 'refresh_intent', None)):
                try:
                    order = self._refresh(order, now)
                except Exception as exc:
                    with j.transaction():
                        j._audit(order['id'], 'refresh_unavailable', {'error': type(exc).__name__}, now)
                    return {'status': 'PENDING', 'order_id': order['id'], 'error': type(exc).__name__}
            if now >= order['intent']['deadline']:
                with j.transaction():
                    j._status(order['id'], 'EXPIRED', now, 'unsigned intent expired')
                    if order['parent_id']: j._status(order['parent_id'], 'EXPIRED', now, 'approval prerequisite expired')
                return {'status': 'EXPIRED', 'order_id': order['id']}
            return self._prepare_and_sign(order, now)
        finally:
            self._release()

    def _refresh(self, order, now):
        """Refresh only unsigned market evidence, never trading authority."""
        j = self.journal
        if order['signed'] or order['tx_hash'] or order['attempts']:
            raise ValueError('A signed identity cannot be refreshed or replaced')
        new = self.backend.refresh_intent(json.loads(encode(order['intent'])))
        allowed = {'quoted_output_raw','min_output_raw','quote_at','deadline','block_number','block_hash',
                   'block_timestamp','fee','token_code_sha256','entry_evidence','provider_quote_digest','context_slot'}
        old_immutable = {k:v for k,v in order['intent'].items() if k not in allowed}
        new_immutable = {k:v for k,v in new.items() if k not in allowed}
        if old_immutable != new_immutable:
            raise ValueError('Refreshed quote changed immutable trading authority')
        new = j._validate_intent(new, now)
        with j.transaction():
            self._fence()
            current = j.order(order['id'])
            if current['signed'] or current['status'] not in ('QUEUED','PREPARING','PREPARED'):
                raise ValueError('Order is no longer eligible for quote refresh')
            if new['side'] == 'buy':
                j._entry_limits(new, now, exclude_id=order['id'])
            if current['intent'] != new:
                j.db.execute('UPDATE execution_orders SET intent=?,intent_digest=?,prepared=NULL,status=\'QUEUED\',updated_at=? WHERE id=?',
                             (encode(new), digest(new), now, order['id']))
                j._audit(order['id'], 'quote_revision', {'before': current['intent'], 'after': new}, now)
        return j.order(order['id'])

    def _prepare_and_sign(self, order, now):
        j = self.journal
        try:
            if order['intent'].get('kind', 'swap') == 'swap':
                j._validate_intent(order['intent'], now)
            with j.transaction():
                self._fence()
                j._status(order['id'], 'PREPARING', now)
            prepared = self.backend.prepare(order['intent'])
            self._fence()
            if prepared.get('requires_approval') is True:
                approval = prepared['approval_intent']
                if approval.get('parent_intent_id') != order['id'] or approval.get('kind') not in ('approval','approval_reset'):
                    raise ValueError('Invalid approval prerequisite')
                for name in ('account_id','chain_id','wallet','policy_hash','side','token_in','token_out','pool','amount_in_raw'):
                    if approval.get(name) != order['intent'].get(name):
                        raise ValueError('Approval prerequisite changed trading authority')
                amount = raw(approval['approval_amount_raw'], 'approval_amount_raw')
                if amount != (0 if approval['kind']=='approval_reset' else int(order['intent']['amount_in_raw'])):
                    raise ValueError('Approval must be exact, never unlimited')
                with j.transaction():
                    self._fence()
                    old = j.order(approval['id'])
                    if old:
                        raise ValueError('Repeated approval prerequisite requires reconciliation')
                    j.db.execute('INSERT INTO execution_orders(id,intent,intent_digest,status,created_at,updated_at,parent_id) VALUES(?,?,?,?,?,?,?)',
                        (approval['id'], encode(approval), digest(approval), 'QUEUED', now, now, order['id']))
                    j._status(order['id'], 'WAITING_APPROVAL', now)
                return {'status': 'WAITING_APPROVAL', 'order_id': order['id']}
            prepared = json.loads(encode(prepared))
            prepared['worker_fence'] = self.owner
            prepared['lease_epoch'] = self.epoch
            if prepared.get('order_id') != order['id'] or prepared.get('policy_hash') != j.policy['policy_hash']:
                raise ValueError('Prepared transaction authority mismatch')
            nonce = integer(prepared['nonce'], 'nonce')
            expires = integer(prepared['expires_at'], 'expires_at')
            if not now < expires <= order['intent']['deadline']:
                raise ValueError('Prepared transaction deadline invalid')
            gas = raw(prepared['max_fee_wei'], 'max_fee_wei', 1)
            with j.transaction():
                self._fence()
                if j._get('full_halt') or (j._get('entry_halt') and order['intent']['side']=='buy'):
                    raise ValueError('Execution halted before signing')
                if order['intent']['side'] == 'buy':
                    root = j.order(order['parent_id']) if order['parent_id'] else order
                    j._validate_intent(root['intent'], now)
                    j._entry_limits(root['intent'], now, exclude_id=root['id'])
                day = j.day_start(now)
                spent = sum(int(r[0]) for r in j.db.execute('SELECT fee_wei FROM execution_fills WHERE observed_at>=?', (day,)))
                reserved = sum(int(o['gas_reservation_wei']) for o in j.orders() if o['status'] in RESERVING and o['id']!=order['id'])
                if gas > j.policy['max_tx_gas_wei'] or spent+reserved+gas > j.policy['max_gas_wei_day']:
                    raise ValueError('Approved native fee budget exceeded')
                if j.db.execute("SELECT 1 FROM execution_orders WHERE nonce=? AND id!=? AND status IN ('SIGNED','SUBMITTED','UNKNOWN','SIGNING','PREPARED')", (nonce,order['id'])).fetchone():
                    raise ValueError('Wallet nonce is already reserved')
                j.db.execute('UPDATE execution_orders SET prepared=?,nonce=?,gas_reservation_wei=? WHERE id=?', (encode(prepared),nonce,str(gas),order['id']))
                j._status(order['id'], 'SIGNING', now)
            # The signer rechecks this callback after key unlock; a slow unlock
            # cannot outlive its worker lease even before a takeover occurs.
            grant = (order['id'], self.owner, self.epoch)
            j._signing_deadlines[grant] = self.tick_started + j.policy['lease_seconds']
            signed = self.backend.sign(prepared)
            self._fence()
            if signed.get('nonce') != nonce or not isinstance(signed.get('raw_transaction'), str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})+', signed['raw_transaction']):
                raise ValueError('Invalid signed transaction bytes/nonce')
            identity = j.tx_hash(signed['tx_hash'])
            with j.transaction():
                self._fence()
                current = j.order(order['id'])
                if current['status'] != 'SIGNING' or current['prepared'] != prepared:
                    raise LeaseLost('Signing reservation changed')
                j.db.execute('UPDATE execution_orders SET signed=?,tx_hash=? WHERE id=?', (encode(signed),identity,order['id']))
                j._status(order['id'], 'SIGNED', now)
            # Deliberately return before network transmission. The next tick
            # always reconciles this identity first, including after a crash.
            return {'status': 'SIGNED', 'order_id': order['id'], 'tx_hash': identity}
        except LeaseLost:
            return {'status': 'LEASE_LOST', 'order_id': order['id']}
        except Exception as exc:
            # Do not leak RPC URLs, keys, raw transactions or signer messages.
            with j.transaction():
                self._fence()
                current = j.order(order['id'])
                if current['status'] in SIGNED:
                    j._status(order['id'], 'UNKNOWN', now, type(exc).__name__)
                else:
                    j._status(order['id'], 'REJECTED', now, type(exc).__name__)
                    if order['parent_id']: j._status(order['parent_id'], 'REJECTED', now, 'approval prerequisite rejected')
            return {'status': j.order(order['id'])['status'], 'order_id': order['id'], 'error': type(exc).__name__}

    def _reconcile(self, order, now):
        j = self.journal
        try:
            result = self.backend.reconcile(order)
            self._fence()
            state = result.get('status')
            if state == 'included':
                j.record_inclusion(order,result,now,self._fence)
                return {'status':'INCLUDED','order_id':order['id']}
            if state == 'expired':
                j.expire_signed(order, result.get('evidence'), now, self._fence)
                return {'status': 'EXPIRED', 'order_id': order['id']}
            if state in ('confirmed','reverted'):
                j._finalize(order, result, now, fence=self._fence)
                return {'status': j.order(order['id'])['status'], 'order_id': order['id']}
            if state == 'reorged':
                with j.transaction():
                    self._fence()
                    j._status(order['id'], 'REVIEW', now, 'canonical chain changed; holdings not fabricated')
                    j._set('full_halt', 'reorganization requires canonical recovery')
                return {'status': 'REVIEW', 'order_id': order['id']}
            if order['status']=='INCLUDED' and state in ('pending','unknown'):
                raise ValueError('Previously verified provisional inclusion is no longer verifiable')
            if state not in ('pending','unknown'):
                raise ValueError('Unsupported reconciliation state')
            if (state == 'pending' and not j._get('full_halt') and not (j._get('entry_halt') and order['intent']['side'] == 'buy') and now < order['prepared']['expires_at']
                    and order['attempts'] <= j.policy['rebroadcast_limit']
                    and (order['last_broadcast_at'] is None or now-order['last_broadcast_at'] >= j.policy['rebroadcast_seconds'])):
                # Persist attempt/UNKNOWN before calling the provider. A crash
                # or timeout after acceptance cannot turn into a new order.
                with j.transaction():
                    self._fence()
                    j.db.execute('UPDATE execution_orders SET attempts=attempts+1,last_broadcast_at=? WHERE id=?', (now,order['id']))
                    j._status(order['id'], 'UNKNOWN', now, notify=False)
                j._broadcast_grants[order['tx_hash']] = (self.owner,self.epoch,self.tick_started+j.policy['lease_seconds'])
                try:
                    returned = self.backend.broadcast(order['signed'])
                finally:
                    j._broadcast_grants.pop(order['tx_hash'], None)
                self._fence()
                if j.tx_hash(returned) != order['tx_hash']:
                    raise ValueError('Provider returned a different transaction identity')
                with j.transaction():
                    self._fence()
                    j._status(order['id'], 'SUBMITTED', now)
                return {'status': 'SUBMITTED', 'order_id': order['id'], 'tx_hash': returned}
            with j.transaction():
                self._fence()
                j._status(order['id'], 'UNKNOWN' if state=='unknown' or now>=order['prepared']['expires_at'] else 'SUBMITTED', now,
                          'Unresolved transaction retains its nonce and exposure')
            return {'status': j.order(order['id'])['status'], 'order_id': order['id']}
        except LeaseLost:
            return {'status': 'LEASE_LOST', 'order_id': order['id']}
        except Exception as exc:
            with j.transaction():
                self._fence()
                if order['status']=='INCLUDED':
                    j._status(order['id'],'REVIEW',now,'Provisional receipt unavailable or changed')
                    j._set('full_halt','Provisional exposure requires canonical recovery')
                    return {'status':'REVIEW','order_id':order['id']}
                j._status(order['id'], 'UNKNOWN', now, type(exc).__name__)
            return {'status': 'UNKNOWN', 'order_id': order['id'], 'error': type(exc).__name__}
