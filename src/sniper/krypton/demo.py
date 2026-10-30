"""Three fictional tokens, isolated account, injected providers, no network."""
from pathlib import Path
from contextlib import contextmanager
import socket
from unittest.mock import patch
import time
import uuid
from .config import load,digest,encode
from .paper import Paper
from .service import scan,watch
from .grok import validate
from rug_desk.book import atomic


def address(n):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    raw=bytes([n])*32;value=int.from_bytes(raw,'big');out=''
    while value:value,r=divmod(value,58);out=alphabet[r]+out
    return out


class Fictional:
    def __init__(self,clock):self.clock=clock;self.price='1'
    def profiles(self):return [address(n) for n in (1,2,3)]
    def quote(self,mint,pool=None):
        return {'mint':mint,'pool':pool or address(4),'symbol':'FICTIONAL-'+str((1,2,3)[[address(n) for n in (1,2,3)].index(mint)]),
            'price':self.price,'liquidity':'100000','created_at_ms':(self.clock()-3600)*1000,
            'observed_at':self.clock(),'executable':False,'source':'fictional demo'}
    def holders(self,mint,cfg):return {'mint':mint,'source':'solana_finalized_rpc','observed_at':self.clock(),
        'complete_owner_coverage':True,'max_single_holder':'0.50' if mint==address(2) else '0.02','top10_share':'0.20',
        'mint_disabled':True,'freeze_disabled':True,'scope':'FICTIONAL demo; no chain proof'}
    def lp(self,mint,pool):return {'mint':mint,'pool':pool,'source':'rugcheck','observed_at':self.clock(),
        'state':'PASS','reported_locked_pct':'100','scope':'FICTIONAL provider fixture; not production evidence'}
    def costs(self,q):return dict(q,sol_usd='150',sol_observed_at=self.clock())


class FictionalGrok:
    def __init__(self,book,clock):
        if not book.demo:raise ValueError('Fictional model requires an isolated demo account')
        self.book,self.clock,self.calls=book,clock,0
    def gate(self):return {'enabled':True,'demo':True,'blockers':[]}
    def ask(self,evidence):
        self.calls+=1
        raw={'risk':7 if evidence['quote']['mint']==address(3) else 3,'reasons':[
            'liquidity='+evidence['quote']['liquidity'],
            'max_single_holder='+evidence['facts']['max_single_holder'],
            'top10_share='+evidence['facts']['top10_share']],'missing_data':[]}
        return dict(validate(encode(raw),evidence),account_id=self.book.identity,config_hash=digest(self.book.cfg),
            evidence_hash=digest(evidence),judged_at=self.clock(),demo=True)


@contextmanager
def offline():
    with patch.object(socket.socket,'connect',side_effect=AssertionError('Demo forbids network')), patch.object(socket.socket,'connect_ex',side_effect=AssertionError('Demo forbids network')):
        yield


@offline()
def demo(root):
    folder=Path(root)/'runtime/krypton-acceptance'/('demo-'+uuid.uuid4().hex)
    now=[int(time.time())];clock=lambda:now[0];cfg=load(Path(root)/'krypton_bot')
    book=Paper(folder,cfg,initialize=True,demo=True,clock=clock);providers=Fictional(clock);model=FictionalGrok(book,clock)
    try:
        for _ in range(3):scan(root,book,providers,model,clock);now[0]+=60
        providers.price='2';watch(root,book,providers,clock)
        from .view import snapshot
        out=snapshot(book.db,clock())
        if out['summary']['fill_count']!=2 or out['summary']['completed_trades']!=1 or model.calls!=2:
            raise AssertionError('Three-token demo did not meet lifecycle assertions')
        report={'path':str(folder),'account_id':book.identity,'demo':True,'excluded_from_strategy_performance':True,
            'fictional_tokens':3,'mock_model_calls':model.calls,'external_calls':0,'fills':2,'completed_trades':1,
            'decisions':out['decision_counts']}
        atomic(folder/'verification.json',encode(report));return report
    finally:book.close()
