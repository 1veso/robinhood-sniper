"""Ordered fail-closed provider checks; passing is not a guarantee against rugs."""
from decimal import Decimal
import time
from rug_desk import config
from rug_desk.book import number
from sniper.adapters.solana import RugcheckClient, validate_address

ORDER = ('not_rugged', 'authority_revoked', 'freeze_revoked', 'no_danger_flags',
         'risk_score', 'lp_locked', 'holders_distributed', 'creator_holding', 'no_insiders')


def normalize(mint, raw, now):
    validate_address(mint)
    if not isinstance(raw, dict) or raw.get('mint') != mint:
        raise ValueError('Explicit report mint identity required')
    out = {'mint': mint, 'pool': None, 'observed_at': now, 'checks': {}, 'details': {
        'limitations': 'Provider screening only. Report retrieval does not prove fresh on-chain state, executable routes, or independent controllers.'}, 'failure': None}
    stage = ORDER[0]
    def require(name, passed, detail=None):
        out['checks'][name] = passed
        if detail is not None:
            out['details'][name] = detail
        if passed is not True:
            out['failure'] = name + (': required evidence missing' if passed is None else ': rejected')
            return False
        return True
    try:
        if not require(stage, not raw['rugged'] if type(raw.get('rugged')) is bool else None):
            return out
        token = raw.get('token')
        token = token if isinstance(token, dict) else {}
        for stage, field in (('authority_revoked', 'mintAuthority'), ('freeze_revoked', 'freezeAuthority')):
            source = token if field in token else raw
            value = source.get(field)
            passed = value is None if field in source else None
            if field in source and value is not None and (not isinstance(value, str) or not value):
                passed = None
            if not require(stage, passed):
                return out
        stage = 'no_danger_flags'
        risks = raw.get('risks')
        passed = None
        if isinstance(risks, list) and all(isinstance(r, dict) and r.get('level') in ('info', 'warn', 'warning', 'danger') for r in risks):
            passed = not any(r['level'] == 'danger' for r in risks)
        if not require(stage, passed):
            return out
        stage = 'risk_score'
        score = number(raw['score_normalised'])
        if score > 100:
            raise ValueError('Invalid normalized score')
        if not require(stage, score <= config.FACTS['max_risk_score'], str(score)):
            return out
        stage = 'lp_locked'
        markets = raw['markets']
        if not isinstance(markets, list) or not markets:
            raise ValueError('Missing markets')
        choices, ids = [], set()
        for market in markets:
            pool = validate_address(market.get('pubkey') or market.get('address'))
            if pool in ids:
                raise ValueError('Duplicate safety pool')
            ids.add(pool)
            lp = market['lp']
            choices.append((number(lp['baseUSD']) + number(lp['quoteUSD']), pool, lp))
        _, pool, lp = max(choices, key=lambda row: row[0])
        out['pool'] = pool
        locked = number(lp['lpLockedPct'])
        if locked > 100:
            raise ValueError('Invalid lock percentage')
        if not require(stage, locked >= config.FACTS['min_lp_locked_pct'], str(locked)):
            return out
        stage = 'holders_distributed'
        supply = number(token['supply'], 'raw supply', positive=True)
        if supply != supply.to_integral_value() or type(token.get('decimals')) is not int or not 0 <= token['decimals'] <= 18:
            raise ValueError('Unknown raw token units')
        holders, known = raw['topHolders'], raw.get('knownAccounts', {})
        if not isinstance(holders, list) or not holders or not isinstance(known, dict):
            raise ValueError('Missing holder evidence')
        addresses, owners, previous, filtered = set(), set(), Decimal(101), []
        total_pct = Decimal(0)
        for holder in holders:
            address, owner = validate_address(holder['address']), validate_address(holder['owner'])
            if address in addresses or owner in owners:
                raise ValueError('Duplicate account or unresolved multi-account owner concentration')
            addresses.add(address)
            owners.add(owner)
            amount, pct = number(holder['amount']), number(holder['pct'])
            if amount != amount.to_integral_value() or amount > supply or pct > previous or pct > 100:
                raise ValueError('Holder units/order inconsistent')
            if abs(amount / supply * 100 - pct) > Decimal('0.01'):
                raise ValueError('Holder percentage does not reconcile with raw supply')
            previous, total_pct = pct, total_pct + pct
            tag = known.get(address) or known.get(owner) or {}
            if not isinstance(tag, dict):
                raise ValueError('Unknown exclusion metadata')
            if str(tag.get('type', '')).upper() not in ('AMM', 'LOCKER'):
                filtered.append(holder)
        complete = type(raw.get('totalHolders')) is int and raw['totalHolders'] == len(holders)
        if total_pct > Decimal('100.01') or not filtered or (len(filtered) < 10 and not complete):
            raise ValueError('Insufficient holder coverage')
        top10 = sum((number(h['pct']) for h in filtered[:10]), Decimal(0))
        if not require(stage, top10 <= config.FACTS['max_top10_pct'], {'pct': str(top10), 'exclusions': 'Provider-tagged AMM/LOCKER only', 'coverage': 'Ordered reported accounts, unique reported owners'}):
            return out
        stage = 'creator_holding'
        balance = number(raw['creatorBalance'])
        if balance == 0:
            creator_pct = Decimal(0)  # A confirmed zero is unit-independent.
        else:
            creator = validate_address(raw['creator'])
            matching = [h for h in holders if h['owner'] == creator]
            if len(matching) != 1 or number(matching[0]['amount']) != balance:
                raise ValueError('Creator raw units or account coverage unverified')
            creator_pct = balance / supply * 100
        if not require(stage, creator_pct <= config.FACTS['max_dev_pct'], str(creator_pct)):
            return out
        stage = 'no_insiders'
        count = number(raw['graphInsidersDetected'])
        if count != count.to_integral_value():
            raise ValueError('Insider count is not an integer')
        flags = [h.get('insider') for h in filtered]
        passed = False if count > config.FACTS['max_insiders'] or any(v is True for v in flags) else (True if all(type(v) is bool for v in flags) else None)
        require(stage, passed)
        return out
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        require(stage, None, 'Field missing, malformed, incompatible or semantically unresolved')
        return out


class Rugcheck:
    def __init__(self, client=None, clock=time.time):
        self.client, self.clock = client or RugcheckClient(timeout=15), clock

    def report(self, mint):
        raw = self.client.raw_report(mint)
        return normalize(mint, raw, int(self.clock()))
