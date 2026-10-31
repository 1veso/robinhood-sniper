"""Explicitly authorized, credential-bound use of observed free API capacity."""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit, parse_qs


class BudgetUnavailable(RuntimeError):
    pass


class BlockscoutBudget:
    COST = 20

    def __init__(self, cache_root, key):
        self.path = cache_root / 'blockscout-usage.json'
        policy_path = cache_root.parent / 'blockscout-free-capacity.json'
        self.policy = None
        if policy_path.exists():
            p = json.loads(policy_path.read_text())
            if (p.get('schema') != 'blockscout_free_capacity_v1'
                    or p.get('operator_authorized') is not True
                    or p.get('paid_allowed') is not False
                    or p.get('key_sha256') != hashlib.sha256(key.encode()).hexdigest()
                    or p.get('request_cost_credits') != self.COST
                    or type(p.get('daily_request_limit')) is not int
                    or not 1000 <= p['daily_request_limit'] <= 4000
                    or p.get('daily_credit_limit') != p['daily_request_limit'] * self.COST
                    or p.get('verified_rps') != 5
                    or type(p.get('verified_remaining_credits')) is not int
                    or not 0 <= p['verified_remaining_credits'] <= 100000
                    or not p.get('verification_reference')):
                raise BudgetUnavailable('Blockscout free-capacity authorization is invalid or credential changed')
            self.policy = p
        self.reserved = None

    @staticmethod
    def day():
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def write(self, value):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(self.path)

    def reserve(self, url):
        day = self.day()
        previous = json.loads(self.path.read_text()) if self.path.exists() else {}
        usage = previous if previous.get('day') == day else {'day': day, 'requests': 0}
        count = usage.get('requests', 0)
        if type(count) is not int or count < 0:
            raise BudgetUnavailable('Blockscout usage is malformed; no quota reset permitted')
        limit = self.policy['daily_request_limit'] if self.policy else 1000
        if count >= limit:
            raise BudgetUnavailable('Daily Blockscout request budget exhausted; resumes next UTC day')
        if self.policy:
            if previous.get('capacity_halt'):
                raise BudgetUnavailable('Blockscout free-capacity evidence is unresolved; verification is required before retry')
            parts = urlsplit(url); q = parse_qs(parts.query)
            if (parts.scheme != 'https' or parts.netloc != 'api.blockscout.com' or parts.path != '/v2/api'
                    or q.get('chain_id') != ['4663'] or q.get('module') != ['account']
                    or q.get('action') not in (['txlist'], ['txlistinternal'], ['tokentx'])):
                raise BudgetUnavailable('Endpoint is outside the verified free history allowance')
            credits = usage.get('reserved_credits', count * self.COST)
            if type(credits) is not int or credits < count * self.COST:
                raise BudgetUnavailable('Blockscout credit counter is malformed')
            remaining = usage.get('provider_credits_remaining')
            if remaining is None and self.policy.get('verified_day') == day:
                remaining = self.policy['verified_remaining_credits']
            if remaining is not None and (type(remaining) is not int or remaining < self.COST):
                raise BudgetUnavailable('Observed Blockscout credits exhausted; no overage permitted')
            if credits + self.COST > self.policy['daily_credit_limit']:
                raise BudgetUnavailable('Local Blockscout free-credit ceiling reached')
            usage = {**usage, 'reserved_credits': credits + self.COST}
            if remaining is not None:
                usage['provider_credits_remaining'] = remaining - self.COST
        self.reserved = {**usage, 'day': day, 'requests': count + 1}
        self.write(self.reserved)  # Reserve before dispatch, including failures.

    def observe(self, headers):
        if not self.policy:
            return
        try:
            remaining = int(headers.get('x-credits-remaining'))
            rps = int(headers.get('x-ratelimit-limit'))
            if not 0 <= remaining <= 100000 or rps != 5:
                raise ValueError('unverified capacity')
        except (ValueError, TypeError, AttributeError):
            self.unknown()
            raise BudgetUnavailable('Blockscout response did not verify free-capacity limits') from None
        prior = self.reserved.get('provider_credits_remaining')
        self.reserved['provider_credits_remaining'] = min(prior, remaining) if prior is not None else remaining
        self.reserved['verified_rps'] = rps
        self.write(self.reserved)

    def unknown(self):
        if self.policy and self.reserved:
            self.reserved['capacity_halt'] = 'missing_response_capacity_evidence'
            self.write(self.reserved)
