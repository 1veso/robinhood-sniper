"""Own account authorization; conservative shared and local paid reservations."""
from sniper.model_budget import jev_uncapped
from contextlib import ExitStack, closing
from decimal import Decimal as D
import json
from pathlib import Path
import time

from rug_desk.book import process_lock
from sniper.advisor import DailyBudget, _unique_object
from sniper.jev_review import settings as shared_settings
from .config import encode, digest, number
from .judge import Client, validate, MODEL, BINDING


class BudgetBlocked(RuntimeError): pass


class Model:
    def __init__(self,root,book,clock=time.time,transport=None):
        self.root,self.book,self.clock,self.transport=Path(root),book,clock,transport

    def binding(self):
        return {'bot_id':'nitrogen','account_id':self.book.identity,'config_hash':digest(self.book.cfg),'judge_binding':BINDING,'model':MODEL}

    def authorization(self):
        value=json.loads((self.book.folder/'paid-jev.json').read_text(),object_pairs_hook=_unique_object)
        if self.book.demo or value.get('enabled') is not True or value.get('operator_authorized') is not True or value.get('binding')!=self.binding() or not value.get('approval_reference'):raise PermissionError('Own account-bound paid authorization required')
        for raw, in self.book.db.execute('SELECT payload FROM api_requests'):
            prior=json.loads(raw)
            if prior.get('state') in ('CLAIMED','RESERVED','ERROR') and number(prior.get('reservation_usd','0'))>0 and prior.get('usage',{}).get('billed_usd') is None:
                raise PermissionError('Interrupted or failed paid request requires invoice reconciliation')
        limit=number(value.get('daily_budget_usd'),True)
        return min(limit,number(self.book.cfg['model_daily_limit_usd']))

    def gate(self):
        try:
            local=self.authorization();shared=number(shared_settings(self.root)['daily_budget_usd'])
            reserve=number(self.book.cfg['model_reservation_usd'])
            if min(local,shared)<reserve:raise PermissionError('Spending ceiling unavailable')
            if not (self.root/'runtime/typesafe-api-key.txt').is_file():raise PermissionError('TypeSafe credential missing')
            return {'enabled':True,'model':MODEL,'blockers':[]}
        except (OSError,ValueError,KeyError,TypeError,PermissionError):
            return {'enabled':False,'model':MODEL,'blockers':['NITROGEN-bound authorization, TypeSafe credential, shared/local spending ceilings and reconciled failed requests required']}

    def ask(self,assessment,state):
        if not self.gate()['enabled']:raise PermissionError('No request sent')
        request='nitrogen:jev:'+assessment
        audit=dict(self.binding(),assessment=assessment,request_id=request,at=int(self.clock()),state='CLAIMED',reservation_usd='0',dossier_hash=digest(state),budget_scope='JEV_ONLY')
        with process_lock(self.root/'runtime/jev-review/paid-request.lock'):
            if not self.gate()['enabled']:raise PermissionError('Authorization changed')
            with self.book.transaction():self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',(request,encode(audit)))
            try:
                with ExitStack() as stack:
                    budgets=[stack.enter_context(closing(DailyBudget(path,limit,uncapped=jev_uncapped(self.root)))) for path,limit in (
                        (self.book.folder/'jev-budget.sqlite3',self.authorization()),
                        (self.root/'runtime/jev-review/budget.sqlite3',number(shared_settings(self.root)['daily_budget_usd'])))]
                    reserve=number(self.book.cfg['model_reservation_usd'])
                    try:
                        for b in budgets:
                            b.reserve(request,reserve,int(self.clock()))
                            b.db.execute('CREATE TABLE IF NOT EXISTS advisory_request_accounts(request_id TEXT PRIMARY KEY,account_id TEXT NOT NULL)')
                            b.db.execute('INSERT INTO advisory_request_accounts VALUES(?,?)',(request,self.book.identity));b.db.commit()
                    except Exception:raise BudgetBlocked('Local/shared spending reservation failed; no call') from None
                    audit.update(state='RESERVED',reservation_usd=str(reserve))
                    self.book.db.execute('UPDATE api_requests SET payload=? WHERE id=?',(encode(audit),request))
                    key=(self.root/'runtime/typesafe-api-key.txt').read_text(encoding='utf-8-sig').strip()
                    client=Client(key,**({'transport':self.transport} if self.transport else {}))
                    raw=client.ask(state);response=validate(raw)
                    estimate=D(response['usage']['input_tokens'])*D('.042')/1000000
                    audit.update(response=response,usage=dict(response['usage'],billed_usd=None,estimated_usd=str(estimate),cost_verified=False),state='RECEIVED')
                    if estimate>reserve:raise ValueError('Usage exceeds reservation; billing reconciliation required')
                    # TypeSafe usage is not an invoice: keep reservations rather than fabricating a billed charge.
                    return {'request_id':request,'response':response}
            except Exception:
                audit['state']='ERROR';raise
            finally:self.book.db.execute('UPDATE api_requests SET payload=? WHERE id=?',(encode(audit),request))


def reconcile(book,invoice):
    """Explicit provider invoice import, attributed only to this account/request."""
    required={'scope','account_id','request_id','provider_invoice_reference','billed_usd'}
    if (not isinstance(invoice,dict) or set(invoice)!=required or invoice['scope']!='TYPESAFE_PROVIDER_INVOICE'
        or invoice['account_id']!=book.identity or not isinstance(invoice['provider_invoice_reference'],str)
        or not invoice['provider_invoice_reference'].strip()):raise ValueError('Bound provider invoice required')
    bill=number(invoice['billed_usd']);request=invoice['request_id']
    row=book.db.execute('SELECT payload FROM api_requests WHERE id=?',(request,)).fetchone()
    if not row:raise ValueError('Invoice request does not belong to NITROGEN')
    audit=json.loads(row[0])
    if audit.get('account_id')!=book.identity or number(audit.get('reservation_usd','0'))==0:raise ValueError('No attempted paid request to reconcile')
    if audit.get('invoice') not in (None,invoice):raise ValueError('Conflicting invoice; no overwrite')
    payload={'provider':'typesafe','account_id':book.identity,'invoice':invoice}
    # Crash recovery is idempotent across budget files; known charges never become free retries.
    paths=(book.folder/'jev-budget.sqlite3',book.root/'runtime/jev-review/budget.sqlite3')
    if audit.get('budget_scope')!='JEV_ONLY':
        # Historical requests retain their original third-ledger reservation.
        paths=(*paths,book.root/'rug_desk/runtime/ai-budget.sqlite3')
    for path in paths:
        with closing(DailyBudget(path,0)) as budget:
            linked=budget.db.execute('SELECT account_id FROM advisory_request_accounts WHERE request_id=?',(request,)).fetchone()
            if not linked or linked[0]!=book.identity:raise ValueError('Budget account binding mismatch')
            prior=budget.db.execute('SELECT billed_usd,payload FROM advisory_usage WHERE request_id=?',(request,)).fetchone()
            if prior and (number(prior[0])!=bill or json.loads(prior[1])!=payload):raise ValueError('Conflicting budget invoice')
            with budget.db:budget.db.execute('INSERT OR IGNORE INTO advisory_usage VALUES(?,?,?)',(request,str(bill),encode(payload)))
    with book.transaction():
        current=json.loads(book.db.execute('SELECT payload FROM api_requests WHERE id=?',(request,)).fetchone()[0])
        if current.get('invoice') not in (None,invoice):raise ValueError('Invoice changed concurrently')
        current['invoice']=invoice;current['usage']=dict(current.get('usage',{}),billed_usd=str(bill),cost_verified=True)
        book.db.execute('UPDATE api_requests SET payload=? WHERE id=?',(encode(current),request))
    return {'request_id':request,'billed_usd':str(bill),'account_id':book.identity}
