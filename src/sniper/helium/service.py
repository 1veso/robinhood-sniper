"""Helium's own candidate team. It neither supervises nor changes another bot."""
import time
from sniper.adapters.solana import request_budget
from sniper.krypton.checks import hard_checks, all_pass, first_failure
from sniper.krypton.config import number
from sniper.krypton.service import error_reason, watch as baseline_watch
from .analyst import Grok
from .providers import Providers, ProviderDeferred
from .reviewer import Reviewer


def gates(book, model, reviewer, now):
    analyst = dict(model.gate(), checked_at=now)
    review = dict(reviewer.gate(), checked_at=now)
    blockers = list(analyst.get('blockers', [])) + list(review.get('blockers', []))
    binding = book.team_binding_current()
    if not binding:
        blockers.append('Team policy changed; new entries require an explicit migration')
    combined = {'enabled':bool(analyst.get('enabled') and review.get('enabled') and binding),
                'blockers':blockers,'checked_at':now}
    book.set('model_gate',analyst)
    book.set('reviewer_gate',review)
    book.set('team_gate',combined)
    return combined


def scan(root, book, providers=None, model=None, reviewer=None, clock=time.time):
    providers = providers or Providers(root,book,clock)
    model = model or Grok(root,book,clock)
    reviewer = reviewer or Reviewer(root,book,clock)
    now = int(clock())
    run_id = book.run_start('scan',now)
    errors = []
    try:
        gates(book,model,reviewer,now)
        book.recover_tasks(now)
        book.expire_pending(now)
        if not book.team_binding_current():
            raise ValueError('Team policy mismatch; entries blocked, independent watcher remains available')
        with request_budget(clock()+43,clock=clock):
            try:
                book.discover(providers.profiles(),int(clock()))
            except ProviderDeferred:
                pass
            except Exception as exc:
                errors.append('profiles: '+error_reason(exc))
            for candidate in book.pending():
                mint = candidate['mint']
                evidence = {'quote':{},'facts':{},'lp':{}}
                task_id = None
                try:
                    task_id = book.begin_task(mint,int(clock()))
                    quote = providers.quote(mint,candidate['pool'])
                    book.pin(mint,quote['pool'])
                    evidence['quote'] = quote
                    book.handoff(task_id,'integrity','Integrity','Check current token and pool evidence',int(clock()))
                    problems = []
                    if all_pass(hard_checks(book.cfg,quote,{}, {},int(clock()))[:2]):
                        try:
                            evidence['facts'] = providers.holders(mint,book.cfg)
                        except Exception as exc:
                            problems.append('On-chain evidence: '+error_reason(exc))
                        try:
                            evidence['lp'] = providers.lp(mint,quote['pool'])
                        except Exception as exc:
                            problems.append('LP provider: '+error_reason(exc))
                    checks = hard_checks(book.cfg,quote,evidence['facts'],evidence['lp'],int(clock()))
                    if not all_pass(checks):
                        book.record_artifact(task_id,'evidence',evidence,int(clock()))
                        book.reject(mint,{'reason':first_failure(checks),'checks':checks,
                                         'evidence':evidence,'errors':problems},int(clock()))
                        continue
                    book.handoff(task_id,'analysis','Analyst',
                                 'Hard checks passed; checking authorized model readiness',int(clock()))
                    gate = gates(book,model,reviewer,int(clock()))
                    if not gate['enabled']:
                        book.record_artifact(task_id,'evidence',evidence,int(clock()))
                        book.reject(mint,{'reason':'Required Helium model access is disabled or unverified',
                                         'checks':checks,'evidence':evidence,'team_gate':gate},int(clock()),action='BLOCKED')
                        continue
                    evidence['quote'] = providers.costs(quote)
                    book.record_artifact(task_id,'evidence',evidence,int(clock()))
                    book.handoff(task_id,'analysis','Analyst','Obtain the baseline cited Grok risk assessment',int(clock()))
                    analysis = model.ask(evidence)
                    book.record_artifact(task_id,'analysis',analysis,int(clock()))
                    if analysis.get('missing_data') or number(analysis.get('risk')) > number(book.cfg['grok_max_risk']):
                        book.reject(mint,{'reason':'Grok risk or missing data blocks entry','checks':checks,
                                         'evidence':evidence,'grok':analysis},int(clock()))
                        continue
                    book.handoff(task_id,'review','Reviewer','Verify support for the analyst rationale',int(clock()))
                    review = reviewer.ask(evidence,analysis,task_id)
                    book.record_artifact(task_id,'review',review,int(clock()))
                    if review.get('decision') != 'BUY':
                        action = 'WATCH' if review.get('decision') == 'WATCH' else 'SKIP'
                        book.reject(mint,{'reason':'Jev did not accept the evidence-supported rationale; no entry',
                                         'checks':checks,'evidence':evidence,'grok':analysis,'review':review},
                                    int(clock()),action=action)
                        continue
                    book.handoff(task_id,'execution','Executor',
                                 'Recheck original risk, costs and bound team evidence before entry',int(clock()))
                    book.enter(mint,evidence,analysis,int(clock()),
                               lambda:gates(book,model,reviewer,int(clock()))['enabled'])
                except Exception as exc:
                    reason = 'Team evidence/model/portfolio unavailable: '+error_reason(exc)
                    errors.append(reason)
                    if task_id and not book.db.execute(
                            "SELECT 1 FROM team_artifacts WHERE task_id=? AND kind='evidence'",(task_id,)).fetchone():
                        book.record_artifact(task_id,'evidence',evidence,int(clock()))
                    book.reject(mint,{'reason':reason,'evidence':evidence,
                        'checks':hard_checks(book.cfg,evidence['quote'],evidence['facts'],evidence['lp'],int(clock()))},
                        int(clock()),action='ERROR')
    except Exception as exc:
        errors.append(error_reason(exc))
    finally:
        book.run_finish(run_id,int(clock()),errors)
        book.publish()
    return errors


def watch(root, book, providers=None, clock=time.time):
    """Exit processing has no dependency on either model or the team task queue."""
    return baseline_watch(root,book,providers or Providers(root,book,clock),clock)
