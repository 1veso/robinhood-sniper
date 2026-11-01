"""Factual, account-bound state. Missing historical evidence stays missing."""
from sniper.adapters.solana import validate_address
from sniper.jev import numeric, untrusted
from sniper.krypton.checks import hard_checks, all_pass
from .config import digest, number

METRICS = ('age_s','market_cap_usd','liquidity_usd','holders_now','holders_then','price_now','price_then',
    'buys_previous_5m','sells_previous_5m','buys_latest_5m','sells_latest_5m','top10_share','dev_share',
    'social_mentions_now','social_mentions_then','independent_buyers','largest_buyer_share_pct',
    'exit_capacity_usd','exit_impact_pct','exit_ticket_usd')
COUNTS = ('holders_now','holders_then','buys_previous_5m','sells_previous_5m','buys_latest_5m','sells_latest_5m',
          'social_mentions_now','social_mentions_then','independent_buyers')


def cycle_id(account, mint, pool):
    return digest({'account_id':account,'chain':'solana','mint':validate_address(mint),'pool':validate_address(pool)})


def whale_checks(e, cfg, now):
    q=e['quote']; valid=[]
    for p in e.get('purchases',[]):
        if (p.get('wallet') in cfg['tracked_wallets'] and p.get('action')=='BUY' and p.get('chain')=='solana'
            and p.get('mint')==q['mint'] and p.get('pool')==q['pool'] and p.get('source')=='solana_finalized_cp_swap'
            and type(p.get('occurred_at')) is int and 0<=now-p['occurred_at']<=cfg['whale_max_age_seconds']
            and type(p.get('observed_at')) is int and p['occurred_at']<=p['observed_at']<=now
            and p.get('transaction_signature') and p.get('source_ref') and type(p.get('slot')) is int and p['slot']>0
            and number(p.get('input_raw',0))>0 and number(p.get('output_raw',0))>0): valid.append(p)
    return valid


def build(book, lead, e, ticket, now):
    q=e['quote']; facts=e['facts']; sup=e.get('supplement') or {}
    checks=hard_checks(book.cfg,q,facts,e['lp'],now)
    purchases=whale_checks(e,book.cfg,now)
    ref='nitrogen:evidence:'+digest(e)
    refs={ref:{'provider':'nitrogen_public_reads','reference':ref,'observed_at':now,'payload_sha256':digest(e)}}
    obs={k:'missing' for k in METRICS}
    def put(k,v,at):
        if v is not None: obs[k]={'value':float(number(v)),'observed_at':at,'source_ref':ref}
    put('price_now',q.get('price'),q.get('observed_at'));put('liquidity_usd',q.get('liquidity'),q.get('observed_at'))
    put('market_cap_usd',q.get('market_cap'),q.get('observed_at'))
    if facts.get('complete_owner_coverage') is True:
        put('holders_now',facts.get('owners'),facts.get('observed_at'));put('top10_share',facts.get('top10_share'),facts.get('observed_at'))
    if sup:
        if (sup.get('account_id')!=book.identity or sup.get('mint')!=q['mint'] or sup.get('pool')!=q['pool']
            or sup.get('demo') is not book.demo): raise ValueError('Supplement account/token/evidence binding invalid')
        allowed={'account_id','mint','pool','demo','snapshot_at','observations','source_refs','window','trade_windows','social_windows','independence_review','exit_route'}
        if set(sup)-allowed: raise ValueError('Nonfactual supplement fields are not allowed')
        if set(sup.get('observations',{}))-set(METRICS): raise ValueError('Unexpected observation')
        if set(refs)&set(sup.get('source_refs',{})): raise ValueError('Duplicate source reference')
        refs.update(sup.get('source_refs',{}))
        for k,v in sup.get('observations',{}).items():
            # Timed supplemental snapshots take precedence; current checks still run separately.
            obs[k]=v
    return {'schema':'nitrogen_dossier_v1','account_id':book.identity,'cycle_id':lead['id'],'lead_id':lead['lead_id'],
        'chain':'solana','mint':q['mint'],'pool':q['pool'],'venue':'raydium_cp_classic_spl','snapshot_at':now,
        'first_seen':lead['first_seen'],'identity_source_ref':ref,'ticket_source':{'policy_hash':digest(book.cfg),'observed_at':now,'formula':'cash-limited configured equity percentage less modeled costs and rent'},'ticket_usd':str(ticket),'evidence_label':'synthetic' if book.demo else 'real_data_paper',
        'observations':obs,'source_refs':refs,'window':sup.get('window','missing'),
        'trade_windows':sup.get('trade_windows','missing'),'social_windows':sup.get('social_windows','missing'),
        'independence_review':sup.get('independence_review','missing'),'exit_route':sup.get('exit_route','missing'),
        'risk':{'passed':all_pass(checks),'checks':checks,'source_ref':ref,'observed_at':now},
        'whale':{'purchases':purchases,'source_ref':ref,'observed_at':now},
        'on_chain_reason':lead['reason'], 'limitations':['Direct finalized classic-SPL Raydium CP purchases only.',
            'Pool age and liquidity alone do not establish launch age, independent controllers or executable exit capacity.',
            'All fills use a modeled cost scenario; no wallet execution.']}


def validate(d, cfg, now):
    missing=[]; values={}
    if d['schema']!='nitrogen_dossier_v1' or d['chain']!='solana' or d['venue']!='raydium_cp_classic_spl': raise ValueError('Unsupported dossier')
    if cycle_id(d['account_id'],d['mint'],d['pool'])!=d['cycle_id']: raise ValueError('Candidate binding invalid')
    if type(d['snapshot_at']) is not int or not 0<=now-d['snapshot_at']<=cfg['evidence_max_age_seconds']: missing.append('stale_snapshot')
    if not d['risk']['passed']: missing.append('risk_not_passed')
    if not d['whale']['purchases']: missing.append('qualifying_tracked_purchase')
    refs=d['source_refs']
    for key,r in refs.items():
        if (not isinstance(key,str) or len(key)>200 or not isinstance(r,dict) or set(r)!={'provider','reference','observed_at','payload_sha256'}
            or not isinstance(r['provider'],str) or not isinstance(r['reference'],str) or len(r['reference'])>500
            or type(r['observed_at']) is not int or not 0<=r['observed_at']<=now
            or not isinstance(r['payload_sha256'],str) or len(r['payload_sha256'])!=64
            or any(c not in '0123456789abcdef' for c in r['payload_sha256'])): raise ValueError('Invalid source provenance')
    if set(d['observations'])!=set(METRICS): raise ValueError('Invalid metric fields')
    for k,o in d['observations'].items():
        if o=='missing' or o is None:
            missing.append(k);continue
        if not isinstance(o,dict) or set(o)!={'value','observed_at','source_ref'} or o['source_ref'] not in refs or type(o['observed_at']) is not int or not 0<=o['observed_at']<=now: raise ValueError('Invalid observation '+k)
        v=numeric(o['value']);values[k]=v
        if k in COUNTS and v!=int(v): raise ValueError('Noninteger count '+k)
        if k in ('top10_share','dev_share') and v>1: raise ValueError('Invalid holder share')
        if k in ('exit_impact_pct','largest_buyer_share_pct') and v>100: raise ValueError('Invalid percentage')
        if not k.endswith('_then') and now-o['observed_at']>cfg['evidence_max_age_seconds']: missing.append('stale_'+k)
    w=d['window']
    if not isinstance(w,dict) or set(w)!={'start','end'} or type(w['start']) is not int or type(w['end']) is not int or w['end']-w['start']!=3600 or not 0<=now-w['end']<=cfg['evidence_max_age_seconds']:
        missing.append('matching_one_hour_window')
    else:
        for k in ('holders_now','price_now','holders_then','price_then'):
            o=d['observations'][k]
            if isinstance(o,dict) and o['observed_at']!=w['start' if k.endswith('_then') else 'end']: missing.append('window_'+k)
    for field,names,width in [('trade_windows',('previous','latest'),300),('social_windows',('then','now'),3600)]:
        windows=d[field]
        if not isinstance(windows,dict) or set(windows)!=set(names): missing.append(field);continue
        valid=True
        for name in names:
            win=windows[name]
            if not isinstance(win,dict) or set(win)!={'start','end'} or any(type(t) is not int for t in win.values()) or win['end']-win['start']!=width: valid=False
        if not valid or windows[names[0]]['end']!=windows[names[1]]['start'] or not 0<=now-windows[names[1]]['end']<=cfg['evidence_max_age_seconds']: missing.append(field)
        else:
            keys=(('buys_previous_5m','sells_previous_5m'),('buys_latest_5m','sells_latest_5m')) if field=='trade_windows' else (('social_mentions_then',),('social_mentions_now',))
            # Count observations are retrieved together now, with explicit event windows.
            for group in keys:
                for k in group:
                    o=d['observations'][k]
                    if isinstance(o,dict) and not 0<=now-o['observed_at']<=cfg['evidence_max_age_seconds']:missing.append('stale_'+k)
    for key in ('independence_review','exit_route'):
        proof=d[key]
        if not isinstance(proof,dict) or set(proof)!={'verified','observed_at','source_ref','mint','pool'} or proof.get('source_ref') not in refs or proof.get('mint')!=d['mint'] or proof.get('pool')!=d['pool'] or proof.get('verified') is not True or type(proof.get('observed_at')) is not int or not 0<=now-proof['observed_at']<=cfg['evidence_max_age_seconds']: missing.append(key)
    if 'exit_ticket_usd' in values and abs(number(values['exit_ticket_usd'])-number(d['ticket_usd']))>number('.000001'): missing.append('exit_quote_size_mismatch')
    for k in ('price_now','price_then','holders_then','exit_capacity_usd'):
        if k in values and values[k]<=0: missing.append('nonpositive_'+k)
    computed={}
    if not missing:
        computed={'holder_growth_pct':(values['holders_now']/values['holders_then']-1)*100,
                  'price_change_1h_pct':(values['price_now']/values['price_then']-1)*100,
                  'ticket_exit_capacity_pct':float(number(d['ticket_usd']))/values['exit_capacity_usd']*100}
    return {'missing':sorted(set(missing)),'computed':computed}


def state(d, validation):
    # Drop narrative checks/reasons and quote metadata; refs remain explicitly untrusted.
    from copy import deepcopy
    out=deepcopy(d);out['computed']=validation['computed']
    out['risk']['checks']=[{k:c.get(k) for k in ('name','state','value')} for c in d['risk']['checks']]
    out['on_chain_reason']={k:d['on_chain_reason'][k] for k in ('kind','transaction_signature','observed_at','source_ref')}
    for r in out['source_refs'].values():
        r['reference']=untrusted(r['reference']);r['provider']=untrusted(r['provider'])
    out['whale']['purchases']=[{k:p[k] for k in ('wallet','mint','pool','transaction_signature','occurred_at','observed_at','slot','input_mint','output_mint','input_raw','output_raw')} for p in d['whale']['purchases']]
    return out
