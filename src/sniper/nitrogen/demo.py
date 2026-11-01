"""Explicit synthetic fixtures in a separate account; never a real provider."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid
from .book import Book
from .config import load,encode,digest
from .judge import MODEL,LEVELS
from . import dossier


def address(n):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';v=n;out=''
    while v:v,r=divmod(v,58);out=alphabet[r]+out
    return '1'*(32-len(n.to_bytes((n.bit_length()+7)//8,'big')))+out


def purchase(now,mint=None,pool=None,wallet=None):
    return {'wallet':wallet or address(1),'chain':'solana','action':'BUY','mint':mint or address(20),'pool':pool or address(21),
        'source':'solana_finalized_cp_swap','transaction_signature':'synthetic-'+str(now),'source_ref':'fixture:purchase',
        'occurred_at':now-5,'observed_at':now,'slot':123,'input_raw':'10000000','output_raw':'100000000',
        'input_mint':'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v','output_mint':mint or address(20)}


def evidence(book,lead,now,price='1'):
    mint,pool=lead['mint'],lead['pool']
    q={'mint':mint,'pool':pool,'symbol':'SYNTHETIC','price':price,'liquidity':'1000000','observed_at':now,'created_at_ms':(now-7200)*1000,'sol_usd':'150','sol_observed_at':now}
    ticket=book.ticket(q,now)
    ref='fixture:'+str(now);values={'age_s':7200,'market_cap_usd':1000000,'liquidity_usd':1000000,'holders_now':1200,'holders_then':1000,
        'price_now':float(price),'price_then':float(price)*.95,'buys_previous_5m':50,'sells_previous_5m':20,'buys_latest_5m':70,'sells_latest_5m':15,
        'top10_share':.2,'dev_share':.01,'social_mentions_now':100,'social_mentions_then':50,'independent_buyers':30,'largest_buyer_share_pct':8,
        'exit_capacity_usd':100000,'exit_impact_pct':.1,'exit_ticket_usd':float(ticket)}
    observations={k:{'value':v,'observed_at':now-3600 if k in ('holders_then','price_then') else now,'source_ref':ref} for k,v in values.items()}
    proof={'verified':True,'observed_at':now,'source_ref':ref,'mint':mint,'pool':pool}
    supplement={'account_id':book.identity,'mint':mint,'pool':pool,'demo':True,'snapshot_at':now,'observations':observations,
        'source_refs':{ref:{'provider':'synthetic_fixture','reference':ref,'observed_at':now,'payload_sha256':digest(values)}},
        'window':{'start':now-3600,'end':now},'trade_windows':{'previous':{'start':now-600,'end':now-300},'latest':{'start':now-300,'end':now}},
        'social_windows':{'then':{'start':now-7200,'end':now-3600},'now':{'start':now-3600,'end':now}},
        'independence_review':proof,'exit_route':proof}
    return {'quote':q,'facts':{'mint':mint,'observed_at':now,'source':'solana_finalized_rpc','complete_owner_coverage':True,'max_single_holder':'.03','top10_share':'.2','mint_disabled':True,'freeze_disabled':True,'owners':1200},
        'lp':{'mint':mint,'pool':pool,'observed_at':now,'source':'rugcheck','state':'PASS','reported_locked_pct':'100'},
        'purchases':[purchase(now,mint,pool,book.cfg['tracked_wallets'][0])],'supplement':supplement,'demo':True}


def response(action='buy',probability=.8,organic=.8,late=.4,score=2):
    dist={str(i):0. for i in range(5)};lo=int(score);dist[str(lo)]=1-(score-lo)
    if score>lo:dist[str(lo+1)]=score-lo
    probs={k:(probability if k==action else (1-probability)/2) for k in ('buy','watch','pass')}
    return {'model':MODEL,'answers':{'organic':{'type':'noul','noul':organic},'late':{'type':'noul','noul':late},
        'action':{'type':'choice','choice':action,'confidence':.11,'probabilities':probs},
        'exit_risk':{'type':'score','score':score,'confidence':.22,'legend':{str(i):x for i,x in enumerate(LEVELS)},'probabilities':dist}},'usage':{'input_tokens':100,'output_tokens':20}}


class SyntheticModel:
    def __init__(self,book,answers=None):
        if not book.demo:raise ValueError('Synthetic model requires demo account')
        self.book=book;self.answers=answers or [response()];self.calls=0
    def gate(self):return {'enabled':False,'scope':'synthetic_no_api'}
    def ask(self,id,state):
        self.calls+=1
        return {'request_id':'synthetic:'+id,'response':deepcopy(self.answers[min(self.calls-1,len(self.answers)-1)])}


def demo(root):
    base=Path(root);root=base/'runtime/nitrogen-demo'/uuid.uuid4().hex
    (root/'nitrogen_bot').mkdir(parents=True);cfg=load(base);cfg['tracked_wallets']=[address(1)]
    (root/'nitrogen_bot/config.json').write_text(encode(cfg));now=int(time.time())
    with closing(Book(root,cfg,initialize=True,demo=True,clock=lambda:now)) as b:
        id=b.discover_purchase(purchase(now),now);lead=b.lead(id);e=evidence(b,lead,now)
        b.assess(id,e,SyntheticModel(b),now);order=b.pending()[0]
        # The synthetic scenario explicitly invokes operator approval; production scan never does.
        result=b.approve(order['id'],e,now)
        duplicate=b.approve(order['id'],e,now);q=dict(e['quote'],price='2.2')
        b.watch(q,now);b.watch(dict(q,price='1.4'),now)
        from .service import head
        report_value=head(b,providers=object());b.publish()
        return {'scope':'synthetic_offline_acceptance','root':str(root),'approval':result,'duplicate':duplicate,'report':report_value}
