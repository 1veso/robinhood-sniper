"""Explicit, position-bound Radeon PAPER exits; no provider or live execution."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib
import json

from .ledger import ZERO, decimal, impact_fraction, json_text, timestamp

KEY = 'radeon_exit_policy'
TABLE = 'radeon_exit_positions'
RUNNER = {
    'name': 'runner-ladder-v1',
    'take_profits': [{'gain_pct': '100', 'fraction': '0.10'},
                     {'gain_pct': '300', 'fraction': '0.20'},
                     {'gain_pct': '600', 'fraction': '0.20'}],
    'stop_loss_pct': '35', 'trailing_stop_pct': '30',
    'protect_gain_pct': '40', 'time_stop_seconds': 21600,
    'time_stop_min_gain_pct': '20',
}
HARVEST = {
    'name': 'harvest-ladder-v1',
    'take_profits': [{'gain_pct': '40', 'fraction': '0.50'},
                     {'gain_pct': '80', 'fraction': '0.25'},
                     {'gain_pct': '160', 'fraction': '0.15'}],
    'stop_loss_pct': '35', 'trailing_stop_pct': '20',
    'protect_gain_pct': '20', 'time_stop_seconds': 21600,
    'time_stop_min_gain_pct': '20',
}


def validate(spec):
    if not isinstance(spec, dict) or set(spec) != set(RUNNER):
        raise ValueError('Invalid exit policy fields')
    if not isinstance(spec['name'], str) or not spec['name']:
        raise ValueError('Exit policy name is required')
    for field in ('stop_loss_pct', 'trailing_stop_pct'):
        if not ZERO < decimal(spec[field]) <= 100:
            raise ValueError('Invalid exit percentage')
    if spec['protect_gain_pct'] is not None and decimal(spec['protect_gain_pct']) <= 0:
        raise ValueError('Protection must arm above entry')
    if type(spec['time_stop_seconds']) is not int or spec['time_stop_seconds'] <= 0:
        raise ValueError('Invalid time stop')
    decimal(spec['time_stop_min_gain_pct'], nonnegative=True)
    levels = spec['take_profits']
    if not isinstance(levels, list) or not 1 <= len(levels) <= 8:
        raise ValueError('Invalid profit ladder')
    previous = total = ZERO
    for level in levels:
        if not isinstance(level, dict) or set(level) != {'gain_pct', 'fraction'}:
            raise ValueError('Invalid profit level')
        gain, fraction = decimal(level['gain_pct']), decimal(level['fraction'])
        if gain <= previous or not ZERO < fraction <= 1:
            raise ValueError('Profit levels must increase with positive fractions')
        previous, total = gain, total + fraction
    if total > 1:
        raise ValueError('Profit fractions exceed the original position')
    return deepcopy(spec)


def revision(spec):
    return hashlib.sha256(json_text(validate(spec)).encode()).hexdigest()


def legacy_policy(policy):
    return {'name': 'inherited-original-exits',
            'take_profits': [{'gain_pct': str(policy.take_profit_pct), 'fraction': str(policy.take_profit_fraction)}],
            'stop_loss_pct': str(policy.stop_loss_pct), 'trailing_stop_pct': str(policy.trailing_stop_pct),
            'protect_gain_pct': None, 'time_stop_seconds': policy.time_stop_seconds,
            'time_stop_min_gain_pct': str(policy.time_stop_min_gain_pct)}


def table_exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def binding(conn, position_id):
    if not table_exists(conn):
        return None
    row = conn.execute(f'SELECT * FROM {TABLE} WHERE position_id=?', (position_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item['policy'] = validate(json.loads(item['policy_json']))
    if revision(item['policy']) != item['revision']:
        raise ValueError('Position exit policy hash mismatch')
    if not 0 <= item['tp_stage'] <= len(item['policy']['take_profits']):
        raise ValueError('Invalid position profit-stage state')
    return item


def _bind(ledger, position, spec, *, inherited):
    ledger.conn.execute(f'''INSERT INTO {TABLE}
        (position_id,asset_id,pool_id,revision,policy_json,inherited,tp_stage)
        VALUES(?,?,?,?,?,?,?)''',
        (position['position_id'], position['asset_id'], position['pool_id'], revision(spec), json_text(spec),
         int(inherited), int(bool(position['tp_done'])) if inherited else 0))


def activate(ledger, policy, spec, *, now, expected_account, expected_source, expected_entry_revision, review_reference,
             expected_previous_revision=None):
    """Caller holds controller + worker locks. Existing positions are never repriced."""
    timestamp(now)
    spec = validate(spec)
    if not isinstance(review_reference, str) or not review_reference.strip():
        raise ValueError('Reviewed evidence reference is required')
    manifest = json.loads(ledger._get('trial_manifest'))
    if (ledger._get('execution_mode') != 'paper' or ledger._get('account_id') != expected_account
            or manifest['account_id'] != expected_account or manifest['source_id'] != expected_source
            or ledger._get('entry_revision') != expected_entry_revision
            or manifest.get('entry_revision') != expected_entry_revision
            or manifest['policy_version'] != policy.version or ledger._get('policy_version') != policy.version
            or ledger._get('policy') != json_text(policy.as_dict())):
        raise ValueError('Exit revision account/source/entry/monetary binding mismatch')
    rev = revision(spec)
    old = ledger._get(KEY)
    if old:
        current = json.loads(old)
        previous_rev = current['revision']
        event = ledger.conn.execute('SELECT payload FROM events WHERE event_id=? AND kind=?',
                                    ('exit-policy-revision:' + previous_rev, 'exit_policy_revision')).fetchone()
        if (current['account_id'] != expected_account or current['source_id'] != expected_source
                or current['entry_revision'] != expected_entry_revision
                or previous_rev != revision(current['policy'])
                or manifest.get('exit_revision') != previous_rev or event is None
                or json.loads(event[0])['new_trial'] != manifest or not table_exists(ledger.conn)):
            raise ValueError('Existing exit revision evidence is inconsistent')
        for position in ledger._positions():
            saved = binding(ledger.conn, position['position_id'])
            if saved is None or saved['asset_id'] != position['asset_id'] or saved['pool_id'] != position['pool_id']:
                raise ValueError('Existing position exit binding is missing or inconsistent')
        if previous_rev == rev and current['policy'] == spec:
            return manifest
        if expected_previous_revision != previous_rev:
            raise ValueError('A different exit revision is already active; explicit migration required')
        if ledger.conn.execute('SELECT 1 FROM events WHERE event_id=?', ('exit-policy-revision:' + rev,)).fetchone():
            raise ValueError('A historical exit revision cannot be silently reactivated')
    elif expected_previous_revision is not None:
        raise ValueError('Expected previous exit revision is missing')
    if now >= manifest['ends_at'] or now < int(ledger._get('last_processed_at') or 0):
        raise ValueError('Exit activation is outside the current trial time')
    active = {'revision': rev, 'policy': spec, 'activated_at': now,
              'account_id': expected_account, 'source_id': expected_source,
              'entry_revision': expected_entry_revision, 'review_reference': review_reference}
    updated = {**manifest, 'exit_revision': rev, 'exit_revision_started_at': now}
    with ledger.transaction():
        if not old:
            ledger.conn.execute(f'''CREATE TABLE {TABLE} (
            position_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, pool_id TEXT NOT NULL,
            revision TEXT NOT NULL, policy_json TEXT NOT NULL, inherited INTEGER NOT NULL,
            tp_stage INTEGER NOT NULL DEFAULT 0, armed_at INTEGER, last_tp_quote_at INTEGER)''')
            for position in ledger._positions():
                _bind(ledger, position, legacy_policy(policy), inherited=True)
        ledger.record_event('exit-policy-revision:' + rev, 'exit_policy_revision',
                            {**active, 'previous_trial': manifest, 'new_trial': updated,
                             'previous_exit_revision': json.loads(old)['revision'] if old else None,
                             **({'existing_positions_keep_bound_exits': True} if old else
                                {'existing_positions_keep_original_exits': True})}, now)
        ledger._set(KEY, json_text(active))
        ledger._set('trial_manifest', json.dumps(updated, sort_keys=True))
    return updated


class BoundRadeonExits:
    """Mixin used only by the independently activated Radeon entry engine."""
    def _position_opened(self, p, quote, now):
        super()._position_opened(p, quote, now)
        raw = self.ledger._get(KEY)
        if not raw:
            if table_exists(self.ledger.conn):
                raise ValueError('Active exit policy missing; new entry cannot be bound')
            return
        active = json.loads(raw)
        manifest = json.loads(self.ledger._get('trial_manifest'))
        if (active['revision'] != revision(active['policy'])
                or active['account_id'] != self.ledger._get('account_id')
                or active['entry_revision'] != self.ledger._get('entry_revision')
                or active['source_id'] != manifest['source_id']
                or active['revision'] != manifest.get('exit_revision')):
            raise ValueError('Active exit policy binding changed')
        _bind(self.ledger, p, active['policy'], inherited=False)

    def decision_context(self, candidate):
        context = super().decision_context(candidate)
        active = json.loads(self.ledger._get(KEY) or 'null')
        if active:
            context.update(exit_revision=active['revision'], exit_policy=active['policy'])
        return context

    def _exit_order(self, p, quote, now, peak):
        saved = binding(self.ledger.conn, p['position_id'])
        if saved is None:
            if table_exists(self.ledger.conn) or self.ledger._get(KEY):
                raise ValueError('Position exit binding missing')
            return super()._exit_order(p, quote, now, peak)
        if saved['asset_id'] != p['asset_id'] or saved['pool_id'] != p['pool_id']:
            raise ValueError('Position exit identity changed')
        spec, stage = saved['policy'], saved['tp_stage']
        gain = (quote.price_usd / p['entry_price'] - 1) * 100
        if spec['protect_gain_pct'] is not None and not stage:
            if saved['armed_at'] is None and gain >= decimal(spec['protect_gain_pct']):
                self.ledger.conn.execute(f'UPDATE {TABLE} SET armed_at=? WHERE position_id=?', (now, p['position_id']))
                self.ledger.record_event('exit-protection:' + p['position_id'], 'exit_protection_armed',
                    {'position_id': p['position_id'], 'exit_revision': saved['revision'],
                     'quote': quote.as_dict(), 'remaining_basis': p['remaining_basis']}, now)
                saved['armed_at'] = now
        gross = p['qty'] * quote.price_usd
        proceeds = gross * (1 - quote.sell_cost_bps / D(10000) - impact_fraction(gross, quote.liquidity_usd)) - quote.gas_usd
        if gain <= -decimal(spec['stop_loss_pct']):
            return p['qty'], 'stop_loss'
        if saved['armed_at'] is not None and not stage and proceeds <= p['remaining_basis']:
            return p['qty'], 'break_even_protection'
        if stage and quote.price_usd <= peak * (1 - decimal(spec['trailing_stop_pct']) / 100):
            return p['qty'], 'trailing_stop'
        levels = spec['take_profits']
        if (stage < len(levels) and gain >= decimal(levels[stage]['gain_pct'])
                and (saved['last_tp_quote_at'] is None or quote.observed_at > saved['last_tp_quote_at'])):
            reason = 'take_profit' if saved['inherited'] else 'take_profit_' + str(stage + 1)
            return min(p['qty'], p['qty0'] * decimal(levels[stage]['fraction'])), reason
        if now - p['opened_at'] >= spec['time_stop_seconds'] and gain < decimal(spec['time_stop_min_gain_pct']):
            return p['qty'], 'time_stop'
        return ZERO, None

    def _sell(self, p, qty, quote, now, reason):
        result = super()._sell(p, qty, quote, now, reason)
        saved = binding(self.ledger.conn, p['position_id'])
        if saved and result.get('verdict') == 'PAPER_SELL':
            if reason.startswith('take_profit'):
                self.ledger.conn.execute(f'UPDATE {TABLE} SET tp_stage=tp_stage+1,last_tp_quote_at=? WHERE position_id=?',
                                         (quote.observed_at, p['position_id']))
            self.ledger.record_event(f"exit-fill:{p['position_id']}:{now}:{reason}", 'exit_policy_fill',
                {'position_id': p['position_id'], 'exit_revision': saved['revision'], 'reason': reason,
                 'inherited_original_exits': bool(saved['inherited']), 'quantity': result['quantity']}, now)
        return result
