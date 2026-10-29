"""Deterministic crew evidence review. Packets and rechecks grant no trade authority."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json

from .jev import POLICY
from .jev_dossier import digest, encode, validate

REVISION = 'crew_review_v1'
ROLES = {
    'Professor': 'Bounded orchestration and handoffs',
    'Berlin': 'Existing strategy and model decision context',
    'Tokyo': 'Attributable discovery observations',
    'Rio': 'Recorded price and volume evidence',
    'Denver': 'Social-source integrity and explicit unknowns',
    'Lisbon': 'Holder and on-chain evidence integrity',
    'Stockholm': 'Unchanged ticket and supported exit feasibility',
    'Nairobi': 'Immutable decision packet',
    'Helsinki': 'Account binding and separately attributed costs',
    'Palermo': 'Independent deterministic veto',
}
PROOF_FIELDS = ('venue_phase', 'independence_review', 'exit_route')


def _number(value, *, positive=False):
    if isinstance(value,bool):raise ValueError('Boolean measurement')
    n=Decimal(str(value))
    if not n.is_finite() or n<0 or (positive and n<=0):raise ValueError('Invalid measurement')
    return n


def _same_number(a,b):
    a,b=_number(a),_number(b)
    return abs(a-b)<=max(Decimal('1e-9'),abs(b)*Decimal('1e-12'))


def require_program_review(payload, *, now):
    """Recheck dated immutable program pins at the actual final decision time."""
    from . import solana_cpmm as cp
    if type(now)is not int:raise ValueError('Integer program review clock required')
    required={'scope','account_id','config_sha256','review_reference','reviewed_at','expires_at','program_pins'}
    expected=None
    try:
        for name in ('venue','exit'):
            component=payload[name];context=component['context'];proof=component['program_review']
            pins=proof['program_pins'];stamp=context['block_time'];captured=component['observed_at']
            if (component['status']!='VALID' or set(proof)!=required
                    or proof['scope']!='CREW_READ_ONLY_PROGRAM_REVIEW'
                    or proof['account_id']!=payload['account_id'] or proof['config_sha256']!=payload['config_sha256']
                    or not isinstance(proof['review_reference'],str) or not proof['review_reference'].strip()
                    or any(type(x)is not int for x in (proof['reviewed_at'],proof['expires_at'],stamp,captured))
                    or not 0<=proof['reviewed_at']<=stamp<=captured<=now<proof['expires_at']
                    or not isinstance(pins,dict) or set(pins)!={cp.PROGRAM,cp.TOKEN,cp.ATA}
                    or pins!=context['program_observations'] or context['commitment']!='finalized'
                    or component['review_sha256']!=digest(proof)
                    or (expected is not None and expected!=proof)):
                raise ValueError('Program review binding, pins or validity changed')
            expected=proof
    except (KeyError,TypeError) as exc:
        raise ValueError('Complete dated program review required') from exc
    return expected


def _holders(payload):
    """Independently reconcile the producer's bounded owner-count arithmetic."""
    from .adapters.solana import validate_address
    h=payload['holder_coverage'];o=payload['observations']['holders_now'];c=h['context']
    rows=c['covered_accounts']
    if not isinstance(rows,list) or not 0<len(rows)<=20 or c['accounts_examined']!=len(rows):raise ValueError('Unbounded holder proof')
    seen=set();owners=set();total=0
    for r in rows:
        address=validate_address(r['address']);owner=validate_address(r['owner']);balance=int(r['balance_raw'])
        if address in seen or balance<0 or str(balance)!=str(r['balance_raw']):raise ValueError('Invalid holder balance')
        seen.add(address);total+=balance
        if balance:owners.add(owner)
    supply=int(c['minted_supply_raw'])
    if (supply<=0 or total!=supply or total!=int(c['covered_balance_raw']) or sorted(owners)!=c['positive_owners']
            or c['commitment']!='finalized' or type(c['slot'])is not int or c['slot']<0
            or not isinstance(c['block_hash'],str) or not c['block_hash']
            or h['status']!='VALID' or o['status']!='VALID' or h['value']!=len(owners) or o['value']!=len(owners)
            or h['observed_at']!=o['observed_at'] or o['observed_at']!=c['block_time']
            or o.get('source_observation_time_known') is not True):raise ValueError('Holder coverage contradiction')
    mapped=payload['dossier_fields']['observations']['holders_now']
    if mapped!={'value':len(owners),'observed_at':o['observed_at']}:raise ValueError('Normalized holder contradiction')


def _exit(payload,dossier):
    """Recompute fee-inclusive exact-size math; this is not wallet simulation."""
    from . import solana_cpmm as cp
    from .adapters.solana_execution import USDC
    r=payload['exit'];s=r['state'];mint=dossier['mint'];ticket=_number(dossier['ticket_usd'],positive=True)
    usd=_number(r['base_usd'],positive=True);price=_number(r['reference_price_usd'],positive=True)
    if {s['mint0'],s['mint1']}!={mint,USDC}:raise ValueError('Unsupported route assets')
    direction=0 if s['mint0']==mint else 1
    if type(s[f'decimals{direction}'])is not int or not 0<=s[f'decimals{direction}']<=18:raise ValueError('Invalid precision')
    raw=int((ticket/usd*10**6).to_integral_value(rounding=ROUND_DOWN))
    acquired=cp.quote_output(s,USDC,raw)
    paper=int((ticket/price*10**s[f'decimals{direction}']).to_integral_value(rounding=ROUND_UP))
    covered=max(acquired,paper);output=cp.quote_output(s,mint,covered)
    for key,value in (('input_usdc_raw',raw),('buy_quantity_raw',acquired),('paper_quantity_upper_bound_raw',paper),('sell_quantity_raw',covered),('sell_output_usdc_raw',output)):
        if str(value)!=r[key]:raise ValueError('Route arithmetic contradiction')
    a,b=s[f'reserve{direction}'],s[f'reserve{1-direction}']
    impact=max(Decimal(0),1-Decimal(output)*a/(Decimal(covered)*b))*100
    low,high=covered,min(a,2**64-1)
    for _ in range(48):
        if low>=high:break
        mid=(low+high+1)//2
        try:cp.quote_output(s,mint,mid)
        except ValueError:high=mid-1
        else:low=mid
    capacity=Decimal(low)*b/a/10**6*usd
    context=r['context'];v=payload['venue']
    if (r['status']!='VALID' or v['status']!='VALID' or v['phase']!='AMM' or context!=v['context']
            or context['commitment']!='finalized' or not context.get('block_hash') or not context.get('program_observations')
            or not r.get('review_sha256') or r['review_sha256']!=v.get('review_sha256')
            or not _same_number(r['ticket_usd'],ticket) or not _same_number(r['exit_price_impact_pct'],impact)
            or not _same_number(r['exit_capacity_usd'],capacity) or capacity<ticket
            or r.get('wallet_simulated') is not False or r.get('network_costs_included') is not False
            or r.get('real_money_enabled') is not False):raise ValueError('Route evidence contradiction')
    obs=payload['dossier_fields']['observations']
    for key,value in (('exit_capacity_usd',capacity),('exit_price_impact_pct',impact),('exit_quote_ticket_usd',ticket)):
        if not _same_number(obs[key]['value'],value) or obs[key]['observed_at']!=r['observed_at']:raise ValueError('Normalized route contradiction')


def _model_ready(judgment,dossier,baseline,now):
    """Check the existing bound Grok result; role flags cannot substitute for it."""
    from rug_desk import config
    from rug_desk.grok import SEARCHES,is_x_url
    if not isinstance(judgment,dict):return False
    try:
        binding=judgment['binding'];at=judgment['judged_at'];usage=judgment['usage']
        return (judgment['verdict']=='BUY' and judgment['validated'] is True and judgment['model']==config.MODEL
            and _number(config.MIN_CONFIDENCE)<=_number(judgment['confidence'])<=1
            and binding['account_id']==dossier['account_id'] and binding['settings_sha256']==baseline['policy_sha256']
            and type(at)is int and 0<=at<=dossier['snapshot_at']<=now and now-at<=POLICY['max_age_seconds']
            and isinstance(judgment['request_id'],str) and bool(judgment['request_id']) and usage['cost_verified'] is True
            and isinstance(judgment.get('citations'),list) and bool(judgment['citations'])
            and all(is_x_url(x) for x in judgment['citations'])
            and isinstance(judgment.get('search_calls'),list) and bool(judgment['search_calls'])
            and all(x in SEARCHES for x in judgment['search_calls']))
    except (KeyError,TypeError,ValueError,ArithmeticError):return False


def _owner(criterion):
    if 'social' in criterion: return 'Denver'
    if any(x in criterion for x in ('exit', 'ticket')): return 'Stockholm'
    if any(x in criterion for x in ('price', 'window')): return 'Rio'
    if any(x in criterion for x in ('holder', 'buyer', 'independence', 'venue', 'provenance', 'dossier')): return 'Lisbon'
    if any(x in criterion for x in ('account', 'baseline')): return 'Helsinki'
    return 'Tokyo'


def review(dossier, baseline, *, now, producer_records=(), role_inputs=None, model_evidence=None):
    """Review facts independently; PASS means no factual veto, never an order.

    Producer records are immutable payloads read from the evidence store. Their
    hashes must match dossier references. Model availability is reported without
    calling a model or promoting another analyst's opinion to evidence.
    """
    if type(now) is not int or now < 0: raise ValueError('Integer review clock required')
    d, b = deepcopy(dossier), deepcopy(baseline)
    if not isinstance(d, dict) or not isinstance(b, dict): raise ValueError('Dossier and baseline required')
    findings = []
    refs = d.get('source_refs') if isinstance(d.get('source_refs'), dict) else {}

    def fail(criterion, status='MISSING', source_refs=(), detail='', retry=True):
        findings.append({'criterion': criterion, 'owner': _owner(criterion),
            'evidence_refs': sorted(set(source_refs)), 'status': status,
            'recheckable': retry, 'detail': detail})

    try:
        evidence = validate(d, now)
        for criterion in evidence['missing_required']:
            fail(criterion, 'STALE' if 'stale' in criterion else 'MISSING')
    except (ValueError, KeyError, TypeError, ArithmeticError):
        evidence = {'missing_required': ['dossier_contract'], 'missing_optional': [], 'computed': {}}
        fail('dossier_contract', 'INVALID', refs, 'Existing Jev factual contract failed', False)
    if (b.get('account_id') != d.get('account_id') or b.get('candidate_id') != d.get('candidate_id')
            or b.get('source') != 'rug_desk' or b.get('policy_sha256') is None):
        fail('baseline_account_binding', 'FAILED', detail='This comparison is scoped to the existing Xenon/Argon source', retry=False)
    if b.get('eligible') is not True:
        fail('baseline_hard_rejection', 'FAILED', detail='Existing entry checks did not pass', retry=False)
    snapshot = d.get('snapshot_at') if type(d.get('snapshot_at')) is int else -1
    if type(b.get('at')) is not int or b['at'] > now or b['at'] > snapshot:
        fail('baseline_timestamp', 'INVALID', retry=False)

    records = {}
    for payload in producer_records:
        if not isinstance(payload, dict):
            fail('producer_provenance', 'INVALID', retry=False); continue
        h = digest(payload)
        if (not isinstance(payload.get('dossier_fields'),dict)
                or any(not isinstance(payload.get(k),dict) for k in ('venue','exit','independence'))):
            fail('producer_provenance', 'INVALID', retry=False); continue
        records[h] = payload
    claimed = set()
    for ref, src in refs.items():
        if not isinstance(src, dict) or not isinstance(ref,str): continue
        if not ref.startswith('rug_desk:crew:'): continue
        h = ref.removeprefix('rug_desk:crew:')
        claimed.add(h)
        p = records.get(h)
        if p is None or src.get('payload_sha256') != h or src.get('reference') != ref:
            fail('producer_provenance', 'MISSING', [ref], 'Immutable producer payload unavailable'); continue
        if (p.get('schema_version') != 1 or not p.get('producer_revision')
                or any(p.get(k) != d.get(k) for k in ('account_id', 'mint', 'pool'))
                or p.get('config_sha256') != b.get('policy_sha256')):
            fail('producer_provenance_binding', 'CONTRADICTORY', [ref], retry=False)
        observed, received = p.get('observed_at'), p.get('received_at')
        if (type(observed) is not int or type(received) is not int
                or not 0 <= observed <= received <= snapshot <= now
                or src.get('observed_at') != observed):
            fail('producer_provenance_time', 'CONTRADICTORY', [ref], 'Observation and receipt must predate the decision snapshot', False)
        if p.get('holder_coverage',{}).get('status')=='VALID':
            try:_holders(p)
            except (KeyError,ValueError,TypeError,ArithmeticError):fail('holder_coverage_arithmetic','CONTRADICTORY',[ref],retry=False)
        if p.get('exit',{}).get('status')=='VALID':
            try:_exit(p,d)
            except (KeyError,ValueError,TypeError,ArithmeticError):fail('exit_quote_arithmetic','CONTRADICTORY',[ref],retry=False)
        if any(p.get(k,{}).get('status')=='VALID' for k in ('exit','venue')):
            try:require_program_review(p,now=now)
            except (KeyError,ValueError,TypeError,ArithmeticError):fail('venue_program_review','STALE_OR_UNBOUND',[ref])

    def record_for(ref):
        return records.get(ref.removeprefix('rug_desk:crew:')) if isinstance(ref, str) and ref.startswith('rug_desk:crew:') else None

    # Refuse invented historical or controller measurements from a current
    # candidate journal row. Present market/provider observations retain scope.
    observations = d.get('observations') if isinstance(d.get('observations'),dict) else {}
    for name, o in observations.items():
        if not isinstance(o, dict): continue
        ref = o.get('source_ref'); p = record_for(ref)
        needs_producer = name in ('price_then', 'holders_now', 'holders_then', 'independent_buyers',
                                 'largest_buyer_share_pct', 'social_mentions_now', 'social_mentions_then')
        if p is None:
            if needs_producer: fail('provenance_' + name, 'MISSING', [ref] if isinstance(ref,str) else [])
            continue
        mapped = name[:-5] + '_now' if name.endswith('_then') else name
        source = (p.get('dossier_fields', {}).get('observations') or {}).get(mapped)
        if not isinstance(source, dict) or any(source.get(k) != o.get(k) for k in ('value', 'observed_at')):
            fail('provenance_' + name, 'CONTRADICTORY', [ref], retry=False)
        if name in ('holders_now','holders_then'):
            measurement=(p.get('observations') or {}).get('holders_now')
            if (not isinstance(measurement,dict) or measurement.get('source_observation_time_known') is not True
                    or p.get('holder_coverage',{}).get('status')!='VALID'):
                fail('holder_observation_time_unknown', 'MISSING', [ref],
                     'Complete canonical holder coverage and an actual observation time are required; provider capture time cannot establish the window')
    for field in PROOF_FIELDS:
        proof = d.get(field)
        if field == 'venue_phase':
            if proof in ('UNKNOWN', None): continue
            ref = d.get('venue_phase_source_ref'); component = 'venue'
        else:
            if not isinstance(proof, dict) or proof.get('verified') is not True: continue
            ref = proof.get('source_ref'); component = 'exit' if field == 'exit_route' else 'independence'
        p = record_for(ref)
        if p is None:
            fail('provenance_' + field, 'MISSING', [ref] if isinstance(ref,str) else []); continue
        expected = p.get('dossier_fields', {}).get(field)
        actual = {k:v for k,v in proof.items() if k != 'source_ref'} if isinstance(proof,dict) else proof
        if p.get(component, {}).get('status') != 'VALID' or expected != actual:
            fail('provenance_' + field, 'CONTRADICTORY', [ref], 'Producer did not establish this scoped claim', False)

    findings.sort(key=lambda f: (f['owner'], f['criterion'], f['status'], f['evidence_refs']))
    role_inputs = deepcopy(role_inputs or {})
    if set(role_inputs) - set(ROLES): raise ValueError('Unknown crew responsibility')
    roles = []
    for role, responsibility in ROLES.items():
        owned = [f for f in findings if f['owner'] == role]
        state = 'VETO' if owned else 'COMPLETE'
        note = 'Deterministic responsibility; no model call'
        if role == 'Berlin':
            state = 'COMPLETE' if _model_ready(model_evidence,d,b,now) else 'UNKNOWN'
            note = 'Bound existing model judgment checked; paid authorization remains with the strategy gate' if state=='COMPLETE' else 'Model judgment and authorization remain with the existing strategy gate'
        if role == 'Denver' and any(observations.get(k) is None for k in ('social_mentions_now', 'social_mentions_then')):
            state = 'UNKNOWN'; note = 'Optional social evidence unavailable; no social claim inferred'
        if role == 'Helsinki': note = 'Account binding only; actual model/API invoices are accounted separately and may remain unknown'
        if role == 'Palermo': state = 'VETO' if findings else 'COMPLETE'
        external = role_inputs.get(role)
        if external is not None:
            if (not isinstance(external,dict) or set(external) - {'status','reason','evidence_refs'}
                    or external.get('status') not in ('COMPLETE','UNKNOWN','BLOCKED')
                    or not isinstance(external.get('reason',''),str)
                    or not isinstance(external.get('evidence_refs',[]),list)
                    or any(r not in refs for r in external.get('evidence_refs',[]))):
                raise ValueError('Invalid role availability observation')
            # Availability can downgrade a role; it cannot erase a factual veto
            # or assert completion of an otherwise unknown model/social job.
            if state != 'VETO' and external['status'] in ('UNKNOWN','BLOCKED'): state = external['status']
            note = external.get('reason',note)
        roles.append({'role':role,'responsibility':responsibility,'status':state,
                      'evidence_refs':sorted(refs),'criteria':[x['criterion'] for x in owned],'detail':note})
    body = {'schema_version':1,'revision':REVISION,'candidate_id':d.get('candidate_id'),
        'account_id':d.get('account_id'),'mint':d.get('mint'),'pool':d.get('pool'),
        'snapshot_at':d.get('snapshot_at'),'reviewed_at':now,
        'dossier_sha256':digest(d),'baseline_sha256':digest(b),'producer_sha256':sorted(claimed),
        'verdict':'VETO' if findings else 'PASS','findings':findings,'roles':roles,'role_inputs':role_inputs,
        'model_evidence':deepcopy(model_evidence),
        'crew_complete':all(r['status']=='COMPLETE' for r in roles),
        'recheck_policy':{'delay_seconds':POLICY['watch_delay_seconds'],'max_attempts':POLICY['max_watch_refreshes']},
        'execution_authority':False,'model_calls':0,
        'meaning':'PASS means no deterministic factual veto; it is not a model approval, entry, or complete crew mission.'}
    return {'packet_id':digest(body),**body}


def validate_packet(packet, dossier, baseline, *, now, producer_records=()):
    """Validate a recorded PASS or VETO without granting execution authority."""
    if not isinstance(packet,dict): raise ValueError('Crew packet required')
    body={k:v for k,v in packet.items() if k!='packet_id'}
    if (packet.get('packet_id') != digest(body) or packet.get('revision') != REVISION
            or packet.get('dossier_sha256') != digest(dossier) or packet.get('baseline_sha256') != digest(baseline)
            or packet.get('verdict') not in ('PASS','VETO') or packet.get('execution_authority') is not False
            or type(packet.get('reviewed_at')) is not int or not dossier['snapshot_at'] <= packet['reviewed_at'] <= now
            or now-packet['reviewed_at'] > POLICY['max_age_seconds']):
        raise ValueError('Current immutable bound crew packet required')
    original = review(dossier,baseline,now=packet['reviewed_at'],producer_records=producer_records,role_inputs=packet.get('role_inputs'),model_evidence=packet.get('model_evidence'))
    if packet != original:
        raise ValueError('Deterministic review findings were changed')
    independent = review(dossier,baseline,now=now,producer_records=producer_records,
                         role_inputs=packet.get('role_inputs'),model_evidence=packet.get('model_evidence'))
    if packet['verdict']=='PASS' and independent['verdict']!='PASS':
        raise ValueError('Crew factual veto cannot be bypassed')
    if packet.get('crew_complete') is True and independent['crew_complete'] is not True:
        raise ValueError('Crew role evidence is no longer complete')
    return packet['verdict']


def require_packet(packet, dossier, baseline, *, now, producer_records=()):
    """Pure final-transaction guard; no DB mutation, provider call or model call."""
    if validate_packet(packet,dossier,baseline,now=now,producer_records=producer_records)!='PASS':
        raise ValueError('Current immutable bound crew PASS required')
    return packet


def _tables(db):
    if db.in_transaction: raise ValueError('Persist crew work outside the financial transaction')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS crew_packets(
        packet_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,account_id TEXT NOT NULL,
        snapshot_at INTEGER NOT NULL,reviewed_at INTEGER NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS crew_rechecks(
        request_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,revision TEXT NOT NULL,
        packet_id TEXT NOT NULL,due_at INTEGER NOT NULL,max_attempts INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,claimed_at INTEGER,
        finished_at INTEGER,result_packet_id TEXT,owners TEXT NOT NULL,
        UNIQUE(candidate_id,revision));
    ''')


def persist_review(db, packet):
    body={k:v for k,v in packet.items() if k!='packet_id'}
    if packet.get('packet_id') != digest(body) or packet.get('revision') != REVISION:
        raise ValueError('Invalid immutable packet')
    _tables(db)
    payload=encode(packet)
    with db:
        db.execute('INSERT OR IGNORE INTO crew_packets VALUES(?,?,?,?,?,?,?)',
            (packet['packet_id'],packet['candidate_id'],packet['account_id'],packet['snapshot_at'],packet['reviewed_at'],REVISION,payload))
        saved=db.execute('SELECT payload FROM crew_packets WHERE packet_id=?',(packet['packet_id'],)).fetchone()[0]
        if saved!=payload: raise ValueError('Immutable packet conflict')
    return json.loads(saved)


def request_recheck(db, packet_id, *, now):
    """One research refresh per candidate/revision; never clears seen-once admission."""
    _tables(db)
    row=db.execute('SELECT payload FROM crew_packets WHERE packet_id=?',(packet_id,)).fetchone()
    if row is None: raise ValueError('Persisted packet required')
    p=json.loads(row[0])
    if type(now) is not int or now < p['reviewed_at']: raise ValueError('Invalid recheck clock')
    owners=sorted({f['owner'] for f in p['findings'] if f['recheckable']})
    if not owners: return None
    identifier=digest({'candidate_id':p['candidate_id'],'revision':REVISION})
    with db:
        db.execute('INSERT OR IGNORE INTO crew_rechecks(request_id,candidate_id,revision,packet_id,due_at,max_attempts,state,owners) VALUES(?,?,?,?,?,?,?,?)',
            (identifier,p['candidate_id'],REVISION,packet_id,p['reviewed_at']+POLICY['watch_delay_seconds'],POLICY['max_watch_refreshes'],'PENDING',encode(owners)))
    return identifier


def claim_due_rechecks(db, *, now, limit=8):
    """Durable claims survive restart. A crash consumes the single bounded attempt."""
    if type(now) is not int or type(limit) is not int or not 1<=limit<=8: raise ValueError('Bounded recheck request required')
    _tables(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        rows=db.execute("SELECT request_id,packet_id,owners,attempts FROM crew_rechecks WHERE state='PENDING' AND due_at<=? AND attempts<max_attempts ORDER BY due_at,request_id LIMIT ?",(now,limit)).fetchall()
        claimed=[]
        for identifier,packet,owners,attempts in rows:
            db.execute("UPDATE crew_rechecks SET state='RUNNING',claimed_at=?,attempts=attempts+1 WHERE request_id=?",(now,identifier))
            claimed.append({'request_id':identifier,'packet_id':packet,'owners':json.loads(owners),'attempt':attempts+1})
        db.commit();return claimed
    except Exception:
        db.rollback();raise


def finish_recheck(db, request_id, result_packet_id, *, now):
    _tables(db)
    with db:
        row=db.execute('SELECT packet_id,due_at,state FROM crew_rechecks WHERE request_id=?',(request_id,)).fetchone()
        if row is None or row[2]!='RUNNING': raise ValueError('Claimed recheck required')
        old=db.execute('SELECT candidate_id,snapshot_at FROM crew_packets WHERE packet_id=?',(row[0],)).fetchone()
        new=db.execute('SELECT candidate_id,snapshot_at,reviewed_at FROM crew_packets WHERE packet_id=?',(result_packet_id,)).fetchone()
        if (new is None or old[0]!=new[0] or new[1]<=old[1] or new[1]<row[1]
                or type(now) is not int or now<new[2]):
            raise ValueError('Fresh due-time snapshot for the same candidate required')
        db.execute("UPDATE crew_rechecks SET state='COMPLETE',finished_at=?,result_packet_id=? WHERE request_id=?",(now,result_packet_id,request_id))


def recover_rechecks(db, *, now):
    """Mark interrupted bounded work; never reissue an ambiguous claimed attempt."""
    _tables(db)
    with db:
        return db.execute("UPDATE crew_rechecks SET state='INTERRUPTED',finished_at=? WHERE state='RUNNING' AND claimed_at<=?",(now,now-POLICY['max_age_seconds'])).rowcount
