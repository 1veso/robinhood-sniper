"""Allowlisted observations and code-computed metrics, with explicit unknowns."""
from decimal import Decimal
import hashlib
import json
import math

from .adapters.solana import validate_address
from .jev import POLICY, numeric, untrusted

METRICS = ('price_now', 'price_then', 'holders_now', 'holders_then', 'independent_buyers',
    'largest_buyer_share_pct', 'exit_capacity_usd', 'exit_price_impact_pct', 'exit_quote_ticket_usd',
    'liquidity_usd', 'reported_top10_pct', 'reported_dev_pct', 'risk_score', 'buys_1h', 'sells_1h',
    'buys_previous_5m', 'sells_previous_5m', 'buys_latest_5m', 'sells_latest_5m',
    'social_mentions_now', 'social_mentions_then', 'follower_growth_24h', 'developer_outcomes_7d')
REQUIRED = ('price_now', 'price_then', 'holders_now', 'holders_then', 'independent_buyers',
            'largest_buyer_share_pct', 'exit_capacity_usd', 'exit_price_impact_pct', 'exit_quote_ticket_usd')


def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(obj):
    return hashlib.sha256(encode(obj).encode()).hexdigest()


def holder_concentration(holders, infrastructure, *, pool):
    """Only individually identified, corroborated infrastructure can be excluded.

    Program ownership alone never excludes an account. This helper does not
    establish independent controllers or repair incomplete holder coverage.
    """
    excluded = set()
    for item in infrastructure:
        if (item.get('verified') is not True or item.get('pool') != pool
                or item.get('role') not in ('bonding_curve', 'pool_vault', 'lp_locker')
                or not isinstance(item.get('source_ref'), str) or not item['source_ref']):
            raise ValueError('Infrastructure identity evidence required')
        excluded.add(validate_address(item['address']))
    seen, retained = set(), []
    for row in holders:
        address = validate_address(row['address'])
        if address in seen:
            raise ValueError('Duplicate holder account')
        seen.add(address)
        pct = numeric(row['pct'], 0, 100)
        if address not in excluded:
            retained.append(pct)
    if not excluded <= seen or sum(numeric(r['pct'], 0, 100) for r in holders) > 100.01:
        raise ValueError('Incomplete exclusion identity or invalid supply percentages')
    return {'top10_reported_accounts_pct': sum(sorted(retained, reverse=True)[:10]),
            'excluded_accounts': sorted(excluded), 'controller_independence': 'UNKNOWN'}


def build(source, identity, row):
    """Read existing Solana check evidence; do not consume analyst judgment text."""
    raw = json.loads(row['payload'])
    facts = raw.get('facts') or {}
    quote = raw.get('pool') or {}
    mint = validate_address(row['mint'])
    pool = quote.get('pool') or facts.get('pool')
    if pool is not None:
        validate_address(pool)
    at = row['at']
    ref = source + ':checks:' + mint + ':' + str(at)
    refs = {ref: {'provider': 'local_candidate_journal', 'reference': ref,
                  'observed_at': at, 'payload_sha256': digest({'facts': facts, 'pool': quote})}}
    obs = {name: None for name in METRICS}
    invalid = []
    def add(name, value, observed):
        if value is None:
            return
        # Recorded prices use exact decimal strings; conversion is explicit here.
        try:
            if isinstance(value, bool): raise ValueError('Boolean observation')
            n = float(Decimal(str(value)))
            if not math.isfinite(n) or n < 0: raise ValueError('Nonfinite observation')
        except (ValueError, ArithmeticError):
            invalid.append(name); return
        obs[name] = {'value': n, 'observed_at': observed, 'source_ref': ref}
    add('price_now', quote.get('price'), quote.get('observed_at', at))
    add('liquidity_usd', quote.get('liquidity'), quote.get('observed_at', at))
    for name in ('buys', 'sells'):
        add(name + '_1h', (quote.get('h1') or {}).get(name), quote.get('observed_at', at))
    details = facts.get('details') or {}
    holder_detail = details.get('holders_distributed') or {}
    if not isinstance(holder_detail, dict):
        invalid.append('reported_top10_pct'); holder_detail = {}
    add('reported_top10_pct', holder_detail.get('pct'), facts.get('observed_at', at))
    add('reported_dev_pct', details.get('creator_holding'), facts.get('observed_at', at))
    add('risk_score', details.get('risk_score'), facts.get('observed_at', at))
    from rug_desk import config
    from rug_desk.market import check_limits
    from rug_desk.rugcheck import ORDER
    eligible = False
    try:
        eligible = (facts.get('mint') == mint and facts.get('pool') == pool
            and facts.get('failure') is None and all(facts.get('checks', {}).get(k) is True for k in ORDER)
            and quote.get('mint') == mint and check_limits(quote, quote['observed_at']) is None
            and (raw.get('stage') == 'judge' or (raw.get('stage') == 'entry' and raw.get('entry') is True)))
        if raw.get('entry') is True:
            eligible = eligible and check_limits(raw['refreshed_pool'], raw['refreshed_pool']['observed_at']) is None
    except (ValueError, KeyError, TypeError):
        pass
    dossier = {'schema_version': 1, 'candidate_id': source + ':' + identity['account_id'] + ':' + mint + ':' + str(pool),
        'account_id': identity['account_id'], 'chain': 'solana', 'mint': mint, 'pool': pool, 'curve': None,
        'venue_phase': 'UNKNOWN', 'venue_phase_source_ref': None, 'snapshot_at': at,
        'window': {'start': None, 'end': None}, 'ticket_usd': float(config.TICKET_USD),
        'observations': obs, 'source_refs': refs, 'invalid_fields': invalid,
        'provider_checks': {k: facts.get('checks', {}).get(k) for k in ORDER},
        'provider_checks_source_ref': ref,
        'independence_review': None, 'exit_route': None,
        'holder_exclusions': [], 'symbol_untrusted': untrusted(quote.get('symbol', '')),
        'limitations': ['Existing pool prices are indicative, not executable exit capacity.',
            'Reported holder concentration is provider-scoped; infrastructure identities and independence are unverified.',
            'Token checks are recorded provider observations, not independently verified LP or controller evidence.',
            'No historical holders, social growth or developer outcomes are inferred.']}
    baseline = {'source': source, 'account_id': identity['account_id'], 'candidate_id': dossier['candidate_id'],
        'at': at, 'verdict': 'BUY' if raw.get('entry') is True else 'SKIP',
        'analyst_verdict': (raw.get('judgment') or {}).get('verdict'),
        'stage': row['stage'], 'reason': row['reason'], 'eligible': eligible,
        'position_id': raw.get('position_id'), 'policy_sha256': identity['config_sha256']}
    if not invalid: dossier.pop('invalid_fields')
    return dossier, baseline


def validate(d, now):
    fields = {'schema_version','candidate_id','account_id','chain','mint','pool','curve','venue_phase',
              'venue_phase_source_ref','snapshot_at','window','ticket_usd','observations','source_refs',
              'independence_review','exit_route','holder_exclusions','symbol_untrusted','limitations',
              'provider_checks','provider_checks_source_ref'}
    if not isinstance(d, dict) or set(d)-{'invalid_fields'} != fields or type(d['schema_version']) is not int or d['schema_version'] != 1 or d['chain'] != 'solana':
        raise ValueError('Invalid dossier contract')
    if not isinstance(d.get('invalid_fields',[]), list) or d.get('invalid_fields'):
        raise ValueError('Source observations failed validation')
    if any(not isinstance(d[k], str) or not 1 <= len(d[k]) <= 300 for k in ('candidate_id','account_id')):
        raise ValueError('Missing candidate identity')
    validate_address(d['mint'])
    for key in ('pool', 'curve'):
        if d[key] is not None: validate_address(d[key])
    if type(d['snapshot_at']) is not int or d['snapshot_at'] < 0 or d['snapshot_at'] > now:
        raise ValueError('Invalid snapshot time')
    if d['venue_phase'] not in ('BONDING_CURVE', 'MIGRATING', 'AMM', 'UNKNOWN'):
        raise ValueError('Invalid venue phase')
    numeric(d['ticket_usd'], .000001)
    if not isinstance(d['source_refs'], dict) or not d['source_refs']:
        raise ValueError('Evidence references required')
    for key, ref in d['source_refs'].items():
        if (not isinstance(key, str) or not isinstance(ref, dict) or set(ref) != {'provider','reference','observed_at','payload_sha256'}
            or type(ref['observed_at']) is not int or not 0 <= ref['observed_at'] <= d['snapshot_at']
            or not isinstance(ref['reference'], str) or not ref['reference'].startswith(('rug_desk:', 'neon:', 'fixture:'))
            or not isinstance(ref['provider'], str) or not isinstance(ref['payload_sha256'], str) or len(ref['payload_sha256']) != 64):
            raise ValueError('Invalid evidence provenance')
    if not isinstance(d['observations'], dict) or set(d['observations']) != set(METRICS):
        raise ValueError('Unknown observation fields')
    missing = []
    from rug_desk.rugcheck import ORDER
    if (not isinstance(d['provider_checks'], dict) or set(d['provider_checks']) != set(ORDER)
        or d['provider_checks_source_ref'] not in d['source_refs']
        or any(v is not None and type(v) is not bool for v in d['provider_checks'].values())):
        raise ValueError('Invalid provider check evidence')
    if any(v is not True for v in d['provider_checks'].values()): missing.append('required_token_checks')
    if now - d['snapshot_at'] > POLICY['max_age_seconds']: missing.append('stale_snapshot')
    if d['pool'] is None: missing.append('exact_pool')
    if d['venue_phase'] in ('UNKNOWN','MIGRATING') or d['venue_phase_source_ref'] not in d['source_refs']:
        missing.append('verified_venue_phase')
    if d['venue_phase'] == 'BONDING_CURVE' and d['curve'] is None: missing.append('exact_curve')
    values = {}
    for key, observation in d['observations'].items():
        if observation is None:
            if key in REQUIRED: missing.append(key)
            continue
        if (not isinstance(observation, dict) or set(observation) != {'value','observed_at','source_ref'}
            or observation['source_ref'] not in d['source_refs'] or type(observation['observed_at']) is not int
            or not 0 <= observation['observed_at'] <= d['snapshot_at']):
            raise ValueError('Invalid timestamped observation')
        values[key] = numeric(observation['value'])
        if key in ('largest_buyer_share_pct','reported_top10_pct','reported_dev_pct','risk_score','exit_price_impact_pct') and values[key] > 100:
            raise ValueError('Invalid percentage')
        if key in ('holders_now','holders_then','independent_buyers') and values[key] != int(values[key]):
            raise ValueError('Count must be an integer')
        if key in REQUIRED and not key.endswith('_then') and d['snapshot_at']-observation['observed_at'] > POLICY['max_age_seconds']:
            missing.append('stale_' + key)
    w = d['window']
    if not isinstance(w, dict) or set(w) != {'start','end'}: raise ValueError('Invalid observation window')
    if (type(w['start']) is not int or type(w['end']) is not int or w['end']-w['start'] != 3600
        or w['start'] < 0 or not 0 <= d['snapshot_at']-w['end'] <= POLICY['max_age_seconds']):
        missing.append('matching_one_hour_window')
    else:
        for key in ('holders_then','price_then','holders_now','price_now'):
            o = d['observations'][key]
            if o is not None and o['observed_at'] != w['start' if key.endswith('_then') else 'end']:
                raise ValueError('Holder and price observation windows differ')
    for field in ('independence_review','exit_route'):
        proof = d[field]
        if proof is None:
            missing.append(field); continue
        if (not isinstance(proof, dict) or set(proof) != {'verified','observed_at','source_ref','mint','pool'}
            or type(proof['verified']) is not bool or proof['source_ref'] not in d['source_refs']
            or type(proof['observed_at']) is not int or proof['mint'] != d['mint'] or proof['pool'] != d['pool']):
            raise ValueError('Invalid scoped proof')
        if not proof['verified'] or not 0 <= d['snapshot_at']-proof['observed_at'] <= POLICY['max_age_seconds']:
            missing.append(field)
    if not isinstance(d['holder_exclusions'], list): raise ValueError('Invalid holder exclusions')
    for item in d['holder_exclusions']:
        if (set(item) != {'address','role','source_ref','verified','pool'} or item['source_ref'] not in d['source_refs']
            or item['verified'] is not True or item['pool'] != d['pool'] or item['role'] not in ('bonding_curve','pool_vault','lp_locker')):
            raise ValueError('Unverified infrastructure exclusion')
        validate_address(item['address'])
    if not isinstance(d['symbol_untrusted'], str) or not d['symbol_untrusted'].startswith('<untrusted>') or not d['symbol_untrusted'].endswith('</untrusted>'):
        raise ValueError('External text must be wrapped')
    if not isinstance(d['limitations'], list) or any(not isinstance(x,str) for x in d['limitations']):
        raise ValueError('Invalid limitations')
    computed = {}
    if all(k in values for k in REQUIRED):
        if min(values['price_then'], values['price_now'], values['holders_then'], values['exit_capacity_usd']) <= 0:
            raise ValueError('Nonpositive arithmetic denominator or price')
        if abs(values['exit_quote_ticket_usd'] - d['ticket_usd']) > .000001:
            raise ValueError('Exit quote does not match baseline ticket')
        computed = {'holder_growth_pct': (values['holders_now']/values['holders_then']-1)*100,
            'price_growth_pct': (values['price_now']/values['price_then']-1)*100,
            'ticket_exit_capacity_pct': d['ticket_usd']/values['exit_capacity_usd']*100}
    # Missing optional facts remain visible as unknowns, never inferred by Jev.
    return {'missing_required': sorted(set(missing)), 'missing_optional': [k for k,v in d['observations'].items() if v is None and k not in REQUIRED],
            'computed': computed}
