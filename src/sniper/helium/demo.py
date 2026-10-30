"""Isolated four-candidate acceptance, with fictional data and no network calls."""
from pathlib import Path
import time
import uuid

from rug_desk.book import atomic
from sniper.krypton.demo import Fictional, FictionalGrok, address, offline
from .config import load, digest, encode, TEAM_POLICY_HASH, TEAM_POLICY
from .paper import Paper
from .reviewer import validate_response, decision
from .service import scan, watch


class FictionalProviders(Fictional):
    def profiles(self):
        return [address(n) for n in (1,2,3,5)]

    def quote(self,mint,pool=None):
        result = super().quote(address(1) if mint == address(5) else mint,pool)
        return dict(result,mint=mint,symbol='FICTIONAL-HELIUM-'+mint[:6])


def fictional_response(label='accept', support=.95):
    probabilities = {name:(.9 if name == label else .05) for name in ('accept','verify_more','reject')}
    return {'model':TEAM_POLICY['model'],'answers':{
        'review':{'type':'choice','choice':label,'confidence':.85,'probabilities':probabilities},
        'evidence_supported':{'type':'noul','noul':support}},
        'usage':{'input_tokens':1000,'output_tokens':0}}


class FictionalReviewer:
    def __init__(self,book,clock):
        if not book.demo:
            raise ValueError('Fictional reviewer requires an isolated demo account')
        self.book,self.clock,self.calls = book,clock,0
        self.label = None

    def gate(self):
        return {'enabled':True,'demo':True,'blockers':[]}

    def ask(self,evidence,analysis,task_id):
        self.calls += 1
        label = self.label or ('reject' if evidence['quote']['mint'] == address(5) else 'accept')
        response = validate_response(fictional_response(label,.1 if label == 'reject' else .95))
        return {'validated':True,'demo':True,'model':TEAM_POLICY['model'],
                'account_id':self.book.identity,'config_hash':digest(self.book.cfg),
                'team_policy_hash':TEAM_POLICY_HASH,'task_id':task_id,
                'evidence_hash':digest(evidence),'analysis_hash':digest(analysis),
                'judged_at':int(self.clock()),'request_id':'fixture:'+task_id,
                'decision':decision(response),'response':response}


@offline()
def demo(root):
    folder = Path(root)/'runtime/helium-acceptance'/('demo-'+uuid.uuid4().hex)
    now = [int(time.time())]
    clock = lambda:now[0]
    book = Paper(folder,load(Path(root)/'helium_bot'),initialize=True,demo=True,clock=clock)
    providers = FictionalProviders(clock)
    model = FictionalGrok(book,clock)
    reviewer = FictionalReviewer(book,clock)
    try:
        for _ in range(4):
            errors = scan(root,book,providers,model,reviewer,clock)
            if errors:
                raise AssertionError('Fictional team scan failed: '+str(errors))
            now[0] += 60
        providers.price = '2'
        if watch(root,book,providers,clock):
            raise AssertionError('Fictional exit failed')
        result = book.snapshot(clock())
        expected = {'BUY':1,'SELL':1,'SKIP':3}
        if (result['decision_counts'] != expected or result['summary']['fill_count'] != 2
            or model.calls != 3 or reviewer.calls != 2):
            raise AssertionError('Four-candidate lifecycle did not match expected outcomes')
        report = {'path':str(folder),'account_id':book.identity,'demo':True,
                  'excluded_from_strategy_performance':True,'fictional_tokens':4,
                  'external_calls':0,'mock_grok_calls':model.calls,'mock_jev_calls':reviewer.calls,
                  'fills':2,'completed_trades':1,'decisions':expected,
                  'team_tasks':book.db.execute('SELECT count(*) FROM team_tasks').fetchone()[0]}
        atomic(folder/'verification.json',encode(report))
        return report
    finally:
        book.close()
