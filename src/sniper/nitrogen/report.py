"""Recorded cohorts and hypothetical 24-hour outcomes, never calibration claims."""
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from zoneinfo import ZoneInfo
from .view import snapshot, decoded


def report(book,now):
    local=datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin'));start=int(local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
    week=int((local-timedelta(days=local.weekday())).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
    v=snapshot(book.db,now);all_a=decoded(book.db,'assessments');orders=decoded(book.db,'orders');closed=decoded(book.db,'closed_trades')
    today=[a for a in all_a if start<=a['started']<=now]
    decisions=decoded(book.db,'decisions');daily=[d for d in decisions if start<=d['at']<=now]
    counts=lambda action:len({d['payload'].get('lead') for d in daily if d['action']==action})
    buckets=[]
    for label,low,high in [('0.70–0.79',.7,.8),('0.80–0.89',.8,.9),('0.90–1.00',.9,1.000001)]:
        cohort=[a for a in all_a if week<=a['started']<=now and a['status']=='COMPLETE' and a['response']['answers']['action']['choice']=='buy' and low<=a['response']['answers']['action']['probabilities']['buy']<high]
        ids={a['id'] for a in cohort};os=[o for o in orders if o['payload']['assessment'] in ids];oids={o['id'] for o in os};cs=[c for c in closed if c['order_id'] in oids]
        buckets.append({'buy_probability':label,'assessed':len(cohort),'approved':sum(o['approved_at'] is not None for o in os),
            'executed':sum(o['filled_at'] is not None for o in os),'closed':len(cs),'wins':sum(D(c['payload']['realized_pnl'])>0 for c in cs),
            'net_trading_pnl':str(sum((D(c['payload']['realized_pnl']) for c in cs),D(0))),'unresolved_outcomes':len(cohort)-len(cs)})
    outcomes=decoded(book.db,'outcomes');known=[o for o in outcomes if o['status']=='OBSERVED']
    completed=[a for a in today if a['status']=='COMPLETE']
    eligible_rates=[(b['buy_probability'],b['wins']/b['closed']) for b in buckets if b['closed']]
    comparison='Insufficient closed outcomes to compare probability buckets.'
    if buckets[0]['closed'] and buckets[2]['closed']:
        comparison='The 0.90–1.00 bucket did not win more often than 0.70–0.79.' if buckets[2]['wins']/buckets[2]['closed']<=buckets[0]['wins']/buckets[0]['closed'] else 'The 0.90–1.00 bucket had a higher observed win rate; sample counts remain visible.'
    return {'scope':'synthetic' if book.demo else 'prospective_paper','account_id':book.identity,'date':local.date().isoformat(),'timezone':'Europe/Berlin','at':now,
        'source_file':str(book.folder/'book.sqlite3'),'leads_found':counts('SEARCH'),'risk_rejected':counts('RISK_REJECTED'),'whale_confirmed':counts('WHALE_CONFIRMED'),
        'judge':{k:sum(a['result']==k for a in completed) for k in ('BUY','WATCH','PASS')},
        'no_answer':sum(a['status']!='COMPLETE' for a in today),'risk_clean_judge_rejections':sum(a['result']=='PASS' for a in completed),
        'orders':{'pending_now':sum(o['status']=='PENDING' for o in orders),'approved_today':sum(start<=(o['approved_at'] or -1)<=now for o in orders),
            'filled_today':sum(start<=(o['filled_at'] or -1)<=now for o in orders),'expired_total':sum(o['status']=='EXPIRED' for o in orders),'rejected_total':sum(o['status']=='REJECTED' for o in orders)},
        'account':v['summary'],'buy_probability_outcomes':{'period_start':week,'scheduled_sunday':local.weekday()==6,'buckets':buckets,'observation':comparison,
            'label':'Observed outcomes by buy-label probability; not a calibration of profit probability. Net trading P&L includes modeled fill costs; model billed/reserved costs are separate account totals.'},
        'rejected_24h':{'scope':'hypothetical fixed decision-time ticket, same cost model, fresh pinned-pool mark at 24h ±5 minutes',
            'total':len(outcomes),'observed':len(known),'hypothetical_green':sum(D(o['payload']['net_pnl'])>0 for o in known),
            'unknown':sum(o['status']=='UNKNOWN' for o in outcomes),'pending':sum(o['status']=='PENDING' for o in outcomes),
            'confirmed_rugs':None,'note':'Price loss is not a verified rug. Missing outcomes remain unknown.'},
        'observations':[f"Recorded {sum(o['filled_at'] is not None for o in orders)} explicitly approved paper entries.",f"{v['summary']['unknown_model_bills']} provider bills remain unverified."],
        'proposed_improvement':'Review the most frequent recorded evidence blocker and source coverage before proposing threshold changes. No configuration was changed.'}
