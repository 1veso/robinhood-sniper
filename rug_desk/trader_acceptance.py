"""Isolated replay acceptance: no network, paid models, real fills or alert delivery."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk.book import Book, atomic, encode
from rug_desk.traders import Selections, validate_rank
from rug_desk.leader_signals import process_event
from rug_desk.rugcheck import normalize
from rug_desk.watch import watch

def key(n):
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    value=int.from_bytes(bytes([n])*32,'big');out=''
    while value:
        value,digit=divmod(value,58);out=alphabet[digit]+out
    return out

def trader(now, name='replay_leader', wallet=None):
    wallet=wallet or key(77);records=[]
    for n in range(20):
        opened=now-5*86400+n*10800
        for side,at,value in (('BUY',opened,'100'),('SELL',opened+3600,'110')):
            records.append({'id':f'fixture-{n}-{side}','source_ref':f'replay:transaction-{n}-{side}',
                'timestamp':at,'block_number':100000+len(records),'transaction_index':0,
                'kind':side,'token':key(100+n%10),'quantity':'10','value_usd':value,
                'gas_usd':'.01','verified':True})
    return {'handle':name,'wallet':wallet,'chain':'solana','wallet_source_ref':'replay:fictional-owner-map',
        'source':{'kind':'replay','reference':'replay:generated-fixture-not-real-performance'},'followers':120,
        'history':{'wallet':wallet,'complete':True,'cutoff':now,'window_start':now-30*86400,
            'cost_basis':'net_token_flows_plus_network_fees','records':records,'gaps':[],'opening_inventory':[],
            'source':'synthetic replay fixture'}}

def event(now, mint=None, name='replay_leader', wallet=None, action='BUY'):
    return {'chain':'solana','handle':name,'wallet':wallet or key(77),'mint':mint or key(1),
        'transaction_signature':'2'*88,'action':action,'occurred_at':now,'observed_at':now,
        'source':'replay','source_ref':'replay:fictional-alert-not-an-on-chain-fill'}

class ReplayJudge:
    def rank_traders(self, candidates):
        return validate_rank({'status':'completed','output_text':encode({'verdict':'SELECT','confidence':.8,
            'leaders':[{'handle':candidates[0]['handle'],'rank':1,'evidence_fields':['win_rate','top_token_profit_share']}]})},candidates)
    def ask(self,*args,**kwargs):
        return {'verdict':'BUY','confidence':'.8','validated':True,
            'why':'Synthetic acceptance verdict, not a real model or X assessment',
            'x_read':'No network in this replay','main_risk':'Fixture only'}

class ReplayMarket:
    def __init__(self, clock):self.clock=clock;self.price='1'
    def quote(self,mint,pool):
        now=int(self.clock())
        return {'mint':mint,'pool':pool,'price':self.price,'liquidity':'50000',
            'observed_at':now,'created_at_ms':(now-3600)*1000,'h1':{'buys':30,'sells':10},
            'sol_usd':'150','sol_observed_at':now}

class ReplayRugcheck:
    def __init__(self, clock):self.clock=clock;self.missing=False
    def report(self,mint):
        raw={'mint':mint,'rugged':False,'token':{'mintAuthority':None,'freezeAuthority':None,'supply':1000000000,'decimals':6},
            'score_normalised':10,'risks':[],'markets':[{'pubkey':key(2),'lp':{'baseUSD':25000,'quoteUSD':25000,'lpLockedPct':99}}],
            'knownAccounts':{},'topHolders':[{'address':key(n),'owner':key(n+30),'amount':10000000,'pct':1,'insider':False} for n in range(10,22)],
            'totalHolders':100,'creatorBalance':0,'graphInsidersDetected':0}
        if self.missing:del raw['token']['mintAuthority']
        return normalize(mint,raw,int(self.clock()))

def run_acceptance(output, now=None):
    output=Path(output)
    if output.exists():raise ValueError('Acceptance requires a fresh isolated directory')
    moment=[int(time.time()) if now is None else now];clock=lambda:moment[0]
    book=Book(output,initialize=True)
    try:
        with book.transaction():book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
        book.enable_costs(moment[0])
        store=Selections(book);judge=ReplayJudge();market=ReplayMarket(clock);rug=ReplayRugcheck(clock)
        records=[trader(moment[0])]
        atomic(output/'traders-replay.json',encode(records))
        selected=store.select(records,judge=judge,clock=clock)
        assert selected['entry_enabled']
        signal=event(moment[0])
        atomic(output/'events-replay.json',encode([signal]))
        dependencies={'clock':clock,'judge':judge,'market':market,'rugcheck':rug}
        first=process_event(book,signal,**dependencies)
        assert first['entry'] and len(book.fills())==1 and book.fills()[0]['execution_costs']
        duplicate=process_event(book,signal,**dependencies)
        assert not duplicate['entry'] and len(book.fills())==1
        unapproved=process_event(book,event(moment[0],key(3),name='unapproved'),**dependencies)
        assert unapproved['verdict']=='SKIP'
        rug.missing=True
        missing=process_event(book,event(moment[0],key(4)),**dependencies)
        assert missing['verdict']=='SKIP' and not missing['entry']
        rug.missing=False
        mirror=process_event(book,event(moment[0],action='SELL'),**dependencies)
        assert not mirror['entry'] and len(book.fills())==1
        empty=store.select([],judge=judge,clock=clock)
        assert empty['empty_selection'] and empty['leaders']==[]
        blocked=process_event(book,event(moment[0],key(5)),**dependencies)
        assert blocked['verdict']=='SKIP' and len(book.positions())==1
        identity=book.identity
        book.close();book=Book(output)
        assert book.identity==identity and Selections(book).status(moment[0])['empty_selection']
        market.price='2.2';moment[0]+=10
        partial=watch(book,market=market,clock=clock)
        assert partial[0]['result']=='take_profit_1'
        market.price='1.4';moment[0]+=10
        closed=watch(book,market=market,clock=clock)
        assert closed[0]['result']=='trailing_exit' and not book.positions()
        decisions=[dict(r) for r in book.db.execute('SELECT event_id,at,verdict,stage,reason FROM leader_decisions ORDER BY id')]
        from rug_desk.report import write_report
        write_report(book,moment[0])
        result={'passed':True,'mode':'SYNTHETIC_REPLAY','excluded_from_strategy_performance':True,
            'network_calls':0,'paid_model_calls':0,'real_fills':0,'telegram_sends':0,
            'checks':{'qualified_buy_reached_existing_pipeline':first['entry'],
                'unapproved_and_missing_risk_skip':unapproved['verdict']==missing['verdict']=='SKIP',
                'duplicate_no_second_entry':not duplicate['entry'],
                'empty_selection_blocks_entries':not blocked['entry'],
                'decisions_and_simulated_fills_separate':len(decisions)>len(book.fills()),
                'positions_managed_after_leaders_removed':not book.positions()},
            'selection':empty,'decisions':decisions,'simulated_fills':book.fills(),
            'exit_results':[partial,closed],
            'connection':'Manual/replayed events only; no supported CopyFomo external execution connection verified'}
        atomic(output/'result.json',encode(result))
        return result
    finally:book.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or Path(__file__).resolve().parent/'runtime/trader-acceptance'/uuid.uuid4().hex
    out=run_acceptance(output)
    print(encode({k:v for k,v in out.items() if k not in ('selection','decisions','simulated_fills')}))

if __name__=='__main__':main()
