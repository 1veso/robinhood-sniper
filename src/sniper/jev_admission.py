"""Prospective Jev filter on the shared Solana entry path; never an executor."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import time

from .jev import MODEL, POLICY, JevClient, validate_response, gate
from .jev_dossier import build, validate, encode, digest
from .jev_review import Journal, settings, detail
from .jev_lifecycle import current, enabled_for
from .advisor import DailyBudget


def assessment(root,identity,facts,quote,now,*,client=None,enrich=None,baseline_buy=True,clock=time.time):
    """Only factual fields reach Jev; no Grok verdict/argument enters the state."""
    row={'mint':facts['mint'],'at':now,'stage':'judge','reason':'Prospective candidate before paper entry',
         'payload':encode({'facts':facts,'pool':quote,'stage':'judge','entry':False})}
    dossier,baseline=build('rug_desk',identity,row)
    baseline.update(verdict='BUY' if baseline_buy else 'SKIP',prospective=True,at=now)
    if enrich is not None:
        dossier=enrich(dossier)
    elif (Path(root)/'runtime/crew/evidence.sqlite3').is_file():
        # Only previously received, immutable, account/pool-bound facts are
        # consumed here. Collection runs separately and cannot delay entry.
        from .crew_capture import configuration, CachedEvidence
        from .crew_evidence import enrich as enrich_recorded
        from types import SimpleNamespace
        if configuration(SimpleNamespace(root=Path(root)/'rug_desk',identity=identity)):
            with closing(CachedEvidence(root)) as evidence:
                dossier=enrich_recorded(dossier,evidence,now=now)
    config=settings(root);budget=None;real_provider=False
    try:
        if client is None and config['enabled'] and config['paid_authorized'] and Decimal(config['daily_budget_usd'])>0:
            key=Path(root)/'runtime/typesafe-api-key.txt'
            if key.is_file():
                client=JevClient(key.read_text(encoding='utf-8-sig').strip())
                budget=DailyBudget(Path(root)/'runtime/jev-review/budget.sqlite3',Decimal(config['daily_budget_usd']))
                real_provider=True
        with closing(Journal(root)) as j:
            result=j.assess(dossier,baseline,now=now,client=client,
                reserve=(lambda rid,at:budget.reserve(rid,Decimal('.01'),at)) if budget else None,clock=clock)
        if real_provider and enrich is None and result['status']=='COMPLETE':
            from .jev_lifecycle import code_binding
            from rug_desk.book import atomic
            proof={'scope':'REAL_PROSPECTIVE_JEV','account_id':identity['account_id'],
                'code_binding':code_binding(root),'observed_at':result['finished'],'validated':True,
                'assessment_id':result['id'],'model':result['result']['response']['model']}
            atomic(Path(root)/'runtime/jev-review/prospective-verification.json',encode(proof))
        return result
    finally:
        if budget:budget.close()


def require_assessment(root,identifier,account,mint,pool,now):
    a=detail(root,identifier)
    d=a['dossier'];result=a['result'];evidence=validate(d,now)
    if (a['status']!='COMPLETE' or a['account_id']!=account or d['mint']!=mint or d['pool']!=pool
        or not a['baseline'].get('prospective') or a['baseline']['verdict']!='BUY'
        or not a['baseline']['eligible'] or evidence['missing_required']
        or d['ticket_usd']!=25 or a['finished']<d['snapshot_at']
        or result.get('policy')!=POLICY or result.get('proposed')!='BUY'
        or gate(validate_response(result.get('response')))[0]!='BUY'
        or type(a['finished'])is not int or not 0<=now-a['finished']<=POLICY['max_age_seconds']):
        raise ValueError('Current bound prospective Jev BUY required')
    return a


def live_cost_rows(root,account):
    """Actual invoices only. Unknown Jev bills reserve spending and block entries.

    TypeSafe's typed inference response has token usage, not an invoice. A separate
    provider billing reconciliation is required; an input-token estimate is never
    inserted as a known charge. No billing record is manufactured here.
    """
    from .jev_review import reader
    path=Path(root)/'runtime/jev-review/assessments.sqlite3'
    if not path.exists():return []
    with closing(reader(path)) as db:
        rows=db.execute('SELECT id,started,status,result FROM assessments WHERE account_id=? AND attempted=1',(account,)).fetchall()
    result=[]
    for row in rows:
        if row['status']=='BUDGET_BLOCKED':continue
        charge=None
        invoice=Path(root)/'runtime/jev-review/billing'/(row['id']+'.json')
        if invoice.exists():
            bill=json.loads(invoice.read_text(encoding='utf-8'))
            if (bill.get('scope')!='TYPESAFE_PROVIDER_INVOICE' or bill.get('account_id')!=account
                or bill.get('assessment_id')!=row['id'] or not bill.get('provider_invoice_reference')):
                raise ValueError('Jev invoice binding invalid')
            charge=Decimal(bill['billed_usd'])
            if not charge.is_finite() or charge<0:raise ValueError('Jev invoice amount invalid')
        result.append(('jev:'+row['id'],row['started'],str(charge) if charge is not None else None,'0.01'))
    return result


def paper_review(book,facts,quote,now,*,client=None,enrich=None,clock=time.time):
    root=book.root.parent
    if not enabled_for(root,book.identity['account_id']):return None
    require_paper_acceptance(root,book.identity['account_id'])
    config=settings(root)
    # Once promoted, disabling review blocks new entries. It does not silently remove the additional filter.
    if not config['enabled']:raise ValueError('Active-paper Jev filter is disabled')
    return assessment(root,book.identity,facts,quote,now,client=client,enrich=enrich,clock=clock)


def paper_guard(book,identifier,mint,pool,now,*,quote=None):
    root=book.root.parent
    if not enabled_for(root,book.identity['account_id']) or not settings(root)['enabled']:
        raise ValueError('Jev paper authority changed before entry')
    require_paper_acceptance(root,book.identity['account_id'])
    result=require_assessment(root,identifier,book.identity['account_id'],mint,pool,now)
    route=result['dossier'].get('exit_route') or {}
    if str(route.get('source_ref','')).startswith('rug_desk:crew:'):
        # A refreshed lower entry price can buy more tokens than an earlier
        # exit quote covers. Recheck quantity inside the existing transaction.
        if quote is None:raise ValueError('Fresh quote required for recorded crew exit coverage')
        from .crew_capture import CachedEvidence
        from .crew_comparison import require_quantity_coverage
        with closing(CachedEvidence(root)) as evidence:
            require_quantity_coverage(result['dossier'],evidence.records_for(result['dossier']),quote,book.cost_policy(),now)
    return result


def require_paper_acceptance(root,account):
    from .jev_lifecycle import code_binding,PAPER_CHECKS
    proof=json.loads((Path(root)/'runtime/jev-review/paper-acceptance.json').read_text(encoding='utf-8'))
    if (proof.get('scope')!='JEV_PAPER_INTEGRATION_ACCEPTANCE' or proof.get('account_id')!=account
        or proof.get('code_binding')!=code_binding(root) or set(proof.get('checks',{}))!=set(PAPER_CHECKS)
        or any(v!='PASS' for v in proof['checks'].values())):
        raise ValueError('Jev integration acceptance does not cover current code/account')


def live_binding(policy):
    if policy.get('strategy_id')!='pump_jev_v1':return
    if (policy.get('jev_policy_sha256')!=digest(POLICY) or not policy.get('paper_account_id')
        or policy['paper_account_id']==policy['account_id']):
        raise ValueError('Jev policy and separate paper/live identity binding required')


def reviewed_live_binding(policy,state):
    from .jev_deployment import operating_digest
    live_binding(policy)
    if (state.get('live_account_id')!=policy['account_id'] or state.get('paper_account_id')!=policy['paper_account_id']
        or state.get('reviewed_operating_sha256')!=operating_digest(policy)):
        raise ValueError('Solana operating policy differs from the reviewed deployment')


def live_guard(root,policy,intent,now):
    if policy.get('strategy_id')!='pump_jev_v1':return None
    live_binding(policy);state=current(root)
    config=settings(root)
    if not config['enabled'] or not config['paid_authorized']:
        raise ValueError('Jev live entry authorization disabled')
    if (state['stage']!='LIVE' or state['live_account_id']!=policy['account_id']
        or state['paper_account_id']!=policy['paper_account_id']):
        raise ValueError('Jev live deployment is not activated')
    reviewed_live_binding(policy,state)
    return require_assessment(root,intent.get('entry_evidence',{}).get('jev_assessment_id'),
        policy['account_id'],intent['token_out'],intent['pool'],now)


def live_review(root,policy,facts,quote,now):
    if policy.get('strategy_id')!='pump_jev_v1':return None
    live_binding(policy);state=current(root)
    if state['stage']!='LIVE' or state['live_account_id']!=policy['account_id']:
        raise ValueError('Jev live deployment is not activated')
    reviewed_live_binding(policy,state)
    identity={'account_id':policy['account_id'],'config_sha256':policy['strategy_binding']['settings_sha256']}
    return assessment(root,identity,facts,quote,now)
