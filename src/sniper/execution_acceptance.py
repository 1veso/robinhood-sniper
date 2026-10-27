"""Synthetic execution lifecycle acceptance. Never connected to a chain or signer."""
from copy import deepcopy
import json
from pathlib import Path

from .execution import ExecutionJournal,AutomaticExecutor

WALLET='0x'+'1'*40;BASE='0x'+'2'*40;TOKEN='0x'+'3'*40;POOL='0x'+'4'*40


class FixtureBackend:
    is_test=True
    def __init__(self,journal):
        self.journal=journal;self.nonce=0;self.receipts={};self.submissions=[];self.disconnect=True
    def prepare(self,i):
        return {'order_id':i['id'],'policy_hash':i['policy_hash'],'nonce':self.nonce,'expires_at':i['deadline'],
            'max_fee_wei':'10','intent':deepcopy(i),'tx':{'nonce':self.nonce,'value':0}}
    def sign(self,p):
        if not self.journal.signing_authorized(p):raise PermissionError('No durable reservation')
        identity='0x'+format(self.nonce+1,'064x')
        return {'nonce':self.nonce,'raw_transaction':identity,'tx_hash':identity,'prepared':deepcopy(p)}
    def broadcast(self,s):
        self.submissions.append(s['tx_hash'])
        if self.disconnect:self.disconnect=False;raise TimeoutError('Fixture accepted submission then disconnected')
        return s['tx_hash']
    def reconcile(self,o):
        if o['id'] not in self.receipts:return {'status':'pending'}
        i=o['intent'];self.nonce=max(self.nonce,o['nonce']+1)
        return {'status':'confirmed','receipt':{'tx_hash':o['tx_hash'],'block_hash':'0x'+'a'*64,
            'block_number':100+self.nonce,'observed_at':self.receipts[o['id']],'canonical':True,'finalized':True},
            'fill':{'input_raw':i['amount_in_raw'],'output_raw':i['quoted_output_raw'],'fee_wei':'3',
            'wallet':WALLET,'token_in':i['token_in'],'token_out':i['token_out']}}


def run_acceptance(folder):
    folder=Path(folder)
    if folder.exists():raise ValueError('Acceptance requires a new isolated directory')
    folder.mkdir(parents=True)
    p={'account_id':'isolated-executor-acceptance','chain_id':4663,'wallet':WALLET,'base_token':BASE,
        'capital_raw':1000,'max_order_raw':5,'max_total_exposure_raw':20,'daily_loss_raw':20,
        'max_gas_wei_day':100,'max_tx_gas_wei':10,'acceptance_mode':True,'deployment_authorized':False}
    path=folder/'ledger.sqlite3';j=ExecutionJournal(path,p,create=True);b=FixtureBackend(j);e=AutomaticExecutor(j,b)
    def intent(name,now,side='buy',amount=5,output=100,reason='entry'):
        value={**{k:j.policy[k] for k in ('account_id','chain_id','wallet','policy_hash')},'id':name,
            'kind':'swap','side':side,'token_in':BASE if side=='buy' else TOKEN,'token_out':TOKEN if side=='buy' else BASE,
            'pool':POOL,'amount_in_raw':str(amount),'quoted_output_raw':str(output),'min_output_raw':str(output),
            'quote_at':now,'deadline':now+30,'reason':reason}
        if side=='buy':value['entry_evidence']={'safety_passed':True,'required_provider_response':True,
            'safety_at':now,'liquidity_usd':'30000','buyers':[{'wallet':'0x'+str(n)*40,'tx_hash':'0x'+str(n)*64,
            'pool':POOL,'token':TOKEN,'observed_at':now} for n in (5,6)]}
        else:value['position_id']='entry'
        return value
    steps=[]
    try:
        j.enqueue(intent('entry',1000),1000);steps.append(e.tick(1000));steps.append(e.tick(1001))
        assert j.order('entry')['status']=='UNKNOWN' and j.snapshot()['fill_count']==0
        signed=j.order('entry')['tx_hash'];j.close()
        j=ExecutionJournal(path,p);b.journal=j;e=AutomaticExecutor(j,b)
        b.receipts['entry']=1002;steps.append(e.tick(1002))
        assert j.order('entry')['tx_hash']==signed and j.snapshot()['fill_count']==1
        for name,now,amount,output,reason in [('partial',1010,50,6,'take_profit'),('close',1020,50,4,'trailing_stop')]:
            j.enqueue(intent(name,now,'sell',amount,output,reason),now)
            steps.extend([e.tick(now),e.tick(now+1)]);b.receipts[name]=now+2;steps.append(e.tick(now+2))
        before=j.snapshot();e.tick(1024);after=j.snapshot()
        assert before['cash_raw']==after['cash_raw']=='1005' and after['fill_count']==3 and not after['positions']
        assert after['native_fees_wei']=='9'
        result={'passed':True,'mode':'SYNTHETIC_EXECUTOR_ACCEPTANCE','network_calls':0,'real_signing_calls':0,
            'real_broadcast_calls':0,'excluded_from_strategy_performance':True,'steps':steps,'snapshot':after,
            'checks':['no fill at submission','timeout retains identity','journal reopened before receipt',
                'entry then partial profit then close','exact fee/cost-basis accounting','duplicate poll is idempotent']}
        (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result
    finally:j.close()
