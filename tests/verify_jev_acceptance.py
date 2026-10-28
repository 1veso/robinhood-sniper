"""Persist an isolated mock-provider acceptance trace; never a strategy account."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT),str(ROOT/'tests')]
from test_jev import Book, check, normalize, raw_report, quote, MINT, BUY, fixture, response
from sniper.jev_dossier import build, encode
from sniper.jev_review import Journal


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);args=p.parse_args()
    root=Path(args.root).resolve()
    allowed=(ROOT/'runtime/jev-acceptance').resolve()
    if not root.is_relative_to(allowed) or root==allowed or root.exists():
        raise ValueError('A new isolated runtime/jev-acceptance child is required')
    root.mkdir(parents=True)
    now=int(time.time());book=Book(root/'rug_desk',initialize=True);journal=Journal(root)
    try:
        book.enable_costs(now)
        market=Mock();market.quote.return_value={**quote(now=now),'sol_usd':'200','sol_observed_at':now}
        entry=check(book,MINT,market=market,rugcheck=Mock(report=Mock(return_value=normalize(MINT,raw_report(),now))),judge=Mock(ask=Mock(return_value=BUY)),clock=lambda:now)
        assert entry['entry']
        row=dict(book.db.execute('select * from checks').fetchone())
        original,baseline=build('rug_desk',book.identity,row)
        dossier,_=fixture(now)
        dossier.update(candidate_id=original['candidate_id'],account_id=original['account_id'])
        dossier['limitations'].append('SYNTHETIC ACCEPTANCE: history, route and independence observations are fixtures, not real market evidence.')
        client=Mock(ask=Mock(return_value=response()))
        before=book.fills()
        first=journal.assess(dossier,baseline,now=now,client=client)
        again=journal.assess(dossier,baseline,now=now+1,client=client)
        assert first['status']=='COMPLETE' and first['id']==again['id'] and client.ask.call_count==1
        assert book.fills()==before and len(before)==1
        assert check(book,MINT,market=market,clock=lambda:now+1)['stage']=='seen_once'
        assert book.fills()==before
        result={'scope':'ISOLATED_SYNTHETIC_ACCEPTANCE_EXCLUDED_FROM_ALL_STRATEGIES',
            'at':now,'account_id':book.identity['account_id'],'position_id':entry['position_id'],
            'assessment_id':first['id'],'mock_provider_calls':client.ask.call_count,
            'real_provider_calls':0,'telegram_calls':0,'signatures':0,'broadcasts':0,
            'baseline_fills_before_review':len(before),'baseline_fills_after_review':len(book.fills()),
            'extra_orders':0,'duplicate_assessment_same_id':True,'seen_once_preserved':True,
            'baseline_entry':entry,'assessment':first,'baseline_fills':before}
        (root/'verification.json').write_text(encode(result),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in ('baseline_entry','assessment','baseline_fills')}))
    finally:journal.close();book.close()


if __name__=='__main__':main()
