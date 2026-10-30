"""Deterministic hard checks. Missing facts are UNKNOWN, never a pass."""
from collections import defaultdict
from decimal import Decimal as D
from sniper.adapters.solana import validate_address
from .config import number

TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'


def check(name, state, value=None, reason='', scope=''):
    return {'name':name,'state':state,'value':value,'reason':reason,'scope':scope}


def parsed(account, kind):
    if not isinstance(account,dict) or account.get('owner') != TOKEN_PROGRAM or account.get('executable') is not False:
        raise ValueError('Only classic SPL token accounts are supported')
    data=account.get('data',{}).get('parsed',{})
    if data.get('type') != kind or not isinstance(data.get('info'),dict): raise ValueError('Invalid parsed token account')
    return data['info']


def raw_amount(value):
    if not isinstance(value,str) or not value.isascii() or not value.isdigit(): raise ValueError('Invalid raw token amount')
    return int(value)


def holder_facts(mint, mint_account, accounts, *, complete):
    info=parsed(mint_account,'mint'); supply=raw_amount(info['supply']); decimals=info['decimals']
    if supply<=0 or type(decimals) is not int or not 0<=decimals<=18: raise ValueError('Invalid token supply/decimals')
    owners=defaultdict(int); seen=set()
    for address,account in accounts:
        validate_address(address)
        if address in seen: raise ValueError('Duplicate token account')
        seen.add(address); item=parsed(account,'account')
        if item.get('mint')!=mint: raise ValueError('Token account mint mismatch')
        owner=validate_address(item['owner']); amount=item['tokenAmount']
        if amount.get('decimals')!=decimals: raise ValueError('Decimals mismatch')
        owners[owner]+=raw_amount(amount['amount'])
    total=sum(owners.values())
    if total>supply: raise ValueError('Holder amounts exceed supply')
    covered=complete and total==supply
    values=sorted(owners.values(),reverse=True)
    return {'supply_raw':str(supply),'decimals':decimals,'covered_supply_raw':str(total),
        'complete_owner_coverage':covered,'token_accounts':len(seen),'owners':sum(v>0 for v in values),
        'max_single_holder':str(D(values[0])/supply) if values else None,
        'top10_share':str(D(sum(values[:10]))/supply) if values else None,
        'mint_disabled':info.get('mintAuthority') is None if 'mintAuthority' in info else None,
        'freeze_disabled':info.get('freezeAuthority') is None if 'freezeAuthority' in info else None,
        'scope':'Token-account owners aggregated; distinct owners do not prove controller independence or creator identity'}


def hard_checks(cfg, quote, facts, lp, now):
    result=[]
    try:
        validate_address(quote['mint']); validate_address(quote['pool'])
        fresh=type(quote.get('observed_at')) is int and 0<=now-quote['observed_at']<=cfg['quote_max_age_seconds']
        liquidity=number(quote.get('liquidity'))
        result.append(check('liquidity','PASS' if fresh and liquidity>=number(cfg['min_liquidity_usd']) else 'FAIL',str(liquidity),'Fresh pinned pool liquidity required','DEX Screener indicative pool observation'))
        created=quote.get('created_at_ms')
        age=(D(now)*1000-created)/60000 if type(created) is int and 0<created<=now*1000 else None
        result.append(check('pool_age','UNKNOWN' if age is None else 'PASS' if age>=cfg['min_pool_age_minutes'] else 'FAIL',str(age) if age is not None else None,'Minimum pool age in minutes'))
    except (KeyError,ValueError,TypeError):
        result.extend([check('liquidity','UNKNOWN',reason='Missing or invalid pinned quote'),check('pool_age','UNKNOWN')])
    fresh=type(facts.get('observed_at')) is int and 0<=now-facts['observed_at']<=cfg['evidence_max_age_seconds']
    bound=facts.get('mint')==quote.get('mint') and facts.get('source')=='solana_finalized_rpc' and fresh
    for key,limit in [('max_single_holder','max_single_holder'),('top10_share','max_top10_share')]:
        value=facts.get(key); state='UNKNOWN'
        if bound and value is not None:
            try:
                n=number(value)
                if n>1: raise ValueError('Invalid share')
                state='FAIL' if n>number(cfg[limit]) else 'PASS' if facts.get('complete_owner_coverage') is True else 'UNKNOWN'
            except ValueError: pass
        result.append(check(key,state,value,'Complete bounded owner coverage required; partial samples are lower bounds',facts.get('scope','')))
    for key in ('mint_disabled','freeze_disabled'):
        value=facts.get(key)
        result.append(check(key,'PASS' if bound and value is True else 'FAIL' if bound and value is False else 'UNKNOWN',value,'Current classic-SPL mint proof required'))
    lp_bound=(lp.get('mint')==quote.get('mint') and lp.get('pool')==quote.get('pool')
        and lp.get('source')=='rugcheck' and type(lp.get('observed_at')) is int
        and 0<=now-lp['observed_at']<=cfg['evidence_max_age_seconds'])
    # This policy accepts a provider-confirmed classification, never independent
    # on-chain LP proof. The distinction survives into decisions and the UI.
    state=lp.get('state','UNKNOWN') if lp_bound else 'UNKNOWN'
    if state not in ('PASS','FAIL','UNKNOWN'): state='UNKNOWN'
    result.append(check('lp_locked',state,lp.get('reported_locked_pct'),
        lp.get('reason','Pool-specific provider LP evidence unavailable'),lp.get('scope','')))
    if lp.get('danger') is True: result.append(check('rug_flags','FAIL',reason='Provider reports a dangerous/rugged token'))
    return result


def all_pass(checks):
    return bool(checks) and all(c.get('state')=='PASS' for c in checks)


def first_failure(checks):
    return next((c['name']+': '+c['state']+' — '+c.get('reason','') for c in checks if c.get('state')!='PASS'),None)
