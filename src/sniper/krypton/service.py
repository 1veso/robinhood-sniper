"""Independent scan and exit jobs. Runtime model failure never disables exits."""
import time
from sniper.adapters.solana import request_budget
from .checks import hard_checks,all_pass,first_failure
from .config import number
from .grok import Grok
from .providers import Providers,ProviderDeferred


def error_reason(exc):
    # Provider transports already redact their URLs/headers. Validation errors
    # contain field names, not supplied credentials or complete provider bodies.
    return str(exc)[:250] if isinstance(exc,ValueError) else type(exc).__name__


def scan(root,book,providers=None,model=None,clock=time.time):
    providers=providers or Providers(root,book,clock);model=model or Grok(root,book,clock)
    now=int(clock());rid=book.run_start('scan',now);errors=[]
    try:
        book.set('model_gate',dict(model.gate(),checked_at=now))
        book.expire_pending(now)
        with request_budget(clock()+43,clock=clock):
            try:book.discover(providers.profiles(),int(clock()))
            except ProviderDeferred:pass
            except Exception as exc:errors.append('profiles: '+error_reason(exc))
            for candidate in book.pending():
                mint=candidate['mint'];now=int(clock())
                if now-candidate['first_seen']>book.cfg['candidate_expiry_seconds']:
                    book.reject(mint,{'reason':'Candidate expired without admission'},now);continue
                evidence={'quote':{},'facts':{},'lp':{}};problems=[]
                try:
                    q=providers.quote(mint,candidate['pool']);book.pin(mint,q['pool']);evidence['quote']=q
                    cheap=hard_checks(book.cfg,q,{}, {},int(clock()))[:2]
                    if all_pass(cheap):
                        try:evidence['facts']=providers.holders(mint,book.cfg)
                        except Exception as exc:problems.append('On-chain evidence: '+error_reason(exc))
                        try:evidence['lp']=providers.lp(mint,q['pool'])
                        except Exception as exc:problems.append('LP provider: '+error_reason(exc))
                    checks=hard_checks(book.cfg,q,evidence['facts'],evidence['lp'],int(clock()))
                    if not all_pass(checks):
                        book.reject(mint,{'checks':checks,'evidence':evidence,'reason':first_failure(checks),'errors':problems},int(clock()));continue
                    if not model.gate()['enabled']:
                        book.reject(mint,{'checks':checks,'evidence':evidence,'reason':'Paid Grok calls disabled','grok':{'state':'DISABLED'}},int(clock()));continue
                    evidence['quote']=providers.costs(q)
                    verdict=model.ask(evidence)
                    if verdict.get('missing_data') or number(verdict.get('risk'))>number(book.cfg['grok_max_risk']):
                        book.reject(mint,{'checks':checks,'evidence':evidence,'grok':verdict,'reason':'Grok risk or missing data blocks entry'},int(clock()));continue
                    book.enter(mint,evidence,verdict,int(clock()),lambda:model.gate()['enabled'])
                except Exception as exc:
                    errors.append('candidate: '+error_reason(exc))
                    book.reject(mint,{'reason':'Evidence/model/portfolio unavailable: '+error_reason(exc),'evidence':evidence,
                        'checks':hard_checks(book.cfg,evidence['quote'],evidence['facts'],evidence['lp'],int(clock()))},int(clock()),action='ERROR')
    except Exception as exc:errors.append(type(exc).__name__)
    finally:
        book.run_finish(rid,int(clock()),errors);book.publish()
    return errors


def watch(root,book,providers=None,clock=time.time):
    providers=providers or Providers(root,book,clock);start=clock();rid=book.run_start('watch',int(start));errors=[]
    try:
        positions=book.positions();positions.sort(key=lambda p:(p.get('last_attempt_at',0),p['mint']))
        with request_budget(start+43,clock=clock):
            for p in positions:
                if clock()>start+38:break
                try:book.watch(providers.costs(providers.quote(p['mint'],p['pool'])),int(clock()))
                except Exception as exc:
                    errors.append(p['mint']+': '+type(exc).__name__)
                    with book.transaction():book.decision(p['mint'],'ERROR',{'reason':'Exit quote unavailable; holding retained','error_type':type(exc).__name__},int(clock()))
                finally:
                    import json
                    row=book.db.execute('SELECT payload FROM positions WHERE mint=?',(p['mint'],)).fetchone()
                    if row:
                        current=json.loads(row[0]);current['last_attempt_at']=int(clock())
                        from .config import encode
                        book.db.execute('UPDATE positions SET payload=? WHERE mint=?',(encode(current),p['mint']))
        with book.transaction():book.daily(int(clock()))
    except Exception as exc:errors.append(type(exc).__name__)
    finally:book.run_finish(rid,int(clock()),errors);book.publish()
    return errors
