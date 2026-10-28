"""Synthetic Neon plumbing acceptance, always in a fresh isolated account."""
import time
from pathlib import Path
from rug_desk.book import atomic,encode
from rug_desk.trader_acceptance import trader,event,key,ReplayJudge,ReplayMarket,ReplayRugcheck
from rug_desk.traders import Selections
from rug_desk.leader_signals import process_event
from rug_desk.watch import watch
from .neon import NeonBook,POLICY,paper_snapshot


class TestBook(NeonBook):
    def _event(self,event_id,now,message):
        super()._event(event_id,now,'TEST — SYNTHETIC NEON ACCEPTANCE; EXCLUDED FROM STRATEGY\n'+message)


def run(output,now=None):
    output=Path(output)
    if output.exists():raise ValueError('Acceptance requires a fresh isolated directory')
    tick=[int(time.time()) if now is None else now];clock=lambda:tick[0]
    book=TestBook(output,initialize=True)
    try:
        with book.transaction():
            book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
            book.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode({'started_at':tick[0],'ends_at':tick[0]+POLICY['trial_seconds'],'synthetic':True}),))
        book.enable_costs(tick[0]);store=Selections(book);judge=ReplayJudge();market=ReplayMarket(clock);rug=ReplayRugcheck(clock)
        store.select([trader(tick[0])],judge=judge,clock=clock)
        deps={'clock':clock,'judge':judge,'market':market,'rugcheck':rug}
        first=process_event(book,event(tick[0]),**deps)
        duplicate=process_event(book,event(tick[0]),**deps)
        unapproved=process_event(book,event(tick[0],key(3),name='unapproved'),**deps)
        rug.missing=True;missing=process_event(book,event(tick[0],key(4)),**deps);rug.missing=False
        store.select([],judge=judge,clock=clock)
        empty=process_event(book,event(tick[0],key(5)),**deps)
        identity=book.identity;book.close();book=TestBook(output)
        persistence=book.identity==identity and len(book.positions())==1
        tick[0]+=10;market.price='2.2';partial=watch(book,market=market,clock=clock)
        tick[0]+=10;market.price='1.4';closed=watch(book,market=market,clock=clock)
        checks={'qualified_buy_reaches_pipeline':bool(first['entry']),
            'unapproved_and_missing_risk_skip':unapproved['verdict']==missing['verdict']=='SKIP',
            'duplicate_does_not_enter':not duplicate['entry'],
            'empty_selection_blocks':not empty['entry'],
            'decisions_separate_from_fills':book.db.execute('SELECT count(*) FROM leader_decisions').fetchone()[0]>len(book.fills()),
            'restart_preserves_position':persistence,
            'independent_original_exits':partial[0]['result']=='take_profit_1' and closed[0]['result']=='trailing_exit' and not book.positions()}
        snapshot=paper_snapshot(book.fills(),book.positions(all_positions=True),tick[0])
        checks['net_accounting_reconciles']=abs(__import__('decimal').Decimal(snapshot['net_pnl'])-__import__('decimal').Decimal(snapshot['realized_pnl']))<__import__('decimal').Decimal('1e-18')
        result={'passed':all(checks.values()),'checks':checks,'account_id':identity['account_id'],
            'mode':'SYNTHETIC_REPLAY','excluded_from_strategy_performance':True,
            'network_calls':0,'paid_calls':0,'signing_calls':0,'broadcast_calls':0,
            'fills':book.fills(),'snapshot':snapshot}
        book.export();atomic(output/'result.json',encode(result))
        if not result['passed']:raise AssertionError(checks)
        return result
    finally:book.close()
