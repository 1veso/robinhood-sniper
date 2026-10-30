"""Offline fixed-entry replay of a read-only Radeon review snapshot.

No prices are fetched, interpolated, or extended beyond the actual holding's
recorded quotes. Output is research, not an alternative live portfolio.
"""
import argparse
from collections import defaultdict
from decimal import Decimal as D
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sniper.engine import PaperEngine
from sniper.ledger import Ledger, json_text
from sniper.models import MarketQuote
from sniper.policy import RiskPolicy
from sniper.radeon_exits import BoundRadeonExits, RUNNER, activate


class ReplayEngine(BoundRadeonExits, PaperEngine):
    """Isolated engine only. Entries are fixed to the original audited fills."""


def replay(entry, decision, quotes, *, revised):
    policy=RiskPolicy(**decision['policy'])
    ledger=Ledger(':memory:',policy.starting_cash_usd)
    engine=ReplayEngine(ledger,policy)
    at=entry['observed_at']
    try:
        if revised:
            account=ledger._get('account_id')
            manifest={'account_id':account,'source_id':'offline-review','entry_revision':'offline-review',
                      'policy_version':policy.version,'started_at':at,'ends_at':at+604800}
            ledger._set('trial_manifest',json.dumps(manifest,sort_keys=True));ledger._set('entry_revision','offline-review')
            activate(ledger,policy,RUNNER,now=at,expected_account=account,expected_source='offline-review',
                     expected_entry_revision='offline-review',review_reference='Isolated historical replay; excluded from strategy')
        q=MarketQuote.from_dict(decision['quote'])
        qty=D(entry['quantity']);basis=-D(entry['cash_delta'])
        # Reconstruct the exact entry, including costs. Do not re-run admission or sizing.
        p={'position_id':entry['position_id'],'asset_id':entry['asset_id'],'symbol':entry['symbol'],
           'strategy':entry['strategy'],'event_id':entry['event_id'],'opened_at':at,'pool_id':q.pool_id,
           'entry_price':D(entry['fill_price']),'reference_entry_price':D(entry['reference_price']),
           'qty0':qty,'qty':qty,'total_entry_cost':basis,'remaining_basis':basis,'realized_pnl':D(0),
           'net_exit_proceeds':D(0),'total_costs':D(entry['friction_usd'])+D(entry['gas_usd']),
           'liq0':q.liquidity_usd,'peak_price':q.price_usd,'mark_price':q.price_usd,
           'mark_liquidity':q.liquidity_usd,'mark_sell_bps':q.sell_cost_bps,'mark_gas':q.gas_usd,
           'mark_at':q.observed_at,'mark_status':'ok','reserved_exit_cost':engine._entry_calculation(D(entry['gross_usd']),q)['exit_cost'],
           'tp_done':0}
        with ledger.transaction():
            keys=list(p)
            ledger.conn.execute(f"INSERT INTO positions({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                                tuple(str(x) if isinstance(x,D) else x for x in p.values()))
            ledger._set('cash',policy.starting_cash_usd-basis)
            ledger._set('fees_paid',p['total_costs'])
            engine._position_opened(p,q,at)
        uncovered=[]
        for now,raw in quotes:
            q=MarketQuote.from_dict(raw)
            result=engine.watch({q.asset_id:q},now)
            uncovered.extend({'at':now,'reason':r['reason']} for r in result if r['verdict']=='UNRESOLVED')
            if not ledger._positions():break
        remaining=ledger._positions()
        return {'complete':not remaining,'realized':ledger._get('realized_pnl'),
                'remaining_basis':str(remaining[0]['remaining_basis']) if remaining else '0',
                'uncovered':uncovered,'sales':[dict(x) for x in ledger.conn.execute('SELECT * FROM fills ORDER BY id')]}
    finally:
        ledger.close()


def study(snapshot):
    marks=defaultdict(list);fills=defaultdict(list)
    for e in snapshot['events']:
        if e['kind']=='market_mark':
            marks[e['event_id'].rsplit(':',2)[0][5:]].append((e['observed_at'],json.loads(e['payload'])))
    for f in snapshot['fills']:fills[f['position_id']].append(f)
    decisions={x['event_id']:json.loads(x['payload']) for x in snapshot['decisions']}
    trades=[]
    for pid,fs in fills.items():
        entry=fs[0];actual=sum((D(f['pnl']) for f in fs),D(0));closed=fs[-1]['action']=='CLOSE'
        original=replay(entry,decisions[entry['event_id']],marks[pid],revised=False)
        revised=replay(entry,decisions[entry['event_id']],marks[pid],revised=True)
        if abs(D(original['realized'])-actual)>D('1e-18') or original['complete']!=closed:
            raise ValueError('Original engine replay does not reconcile: '+pid)
        trades.append({'position_id':pid,'symbol':entry['symbol'],'opened_at':entry['observed_at'],
                       'entry_policy':decisions[entry['event_id']]['result']['policy_version'],
                       'entry_revision':decisions[entry['event_id']]['result'].get('entry_revision'),
                       'actual_closed':closed,'actual_realized':actual,'revised':revised,
                       'included_pair':closed and revised['complete'] and not revised['uncovered']})
    paired=[x for x in trades if x['included_pair']]
    return {'snapshot_at':snapshot['at'],'excluded_from_strategy':True,'policy':RUNNER,
            'scope':'Fixed actual entries; recorded holding quotes only. No continuous highs, future prices, or portfolio/reentry counterfactual.',
            'selection_bias':'Ladder chosen after examining these trades. This is in-sample research, not out-of-sample validation.',
            'entries':len(trades),'paired_count':len(paired),
            'paired_actual':sum((x['actual_realized'] for x in paired),D(0)),
            'paired_replay':sum((D(x['revised']['realized']) for x in paired),D(0)),
            'trades':trades}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    report=study(json.loads(args.snapshot.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json_text(report),encoding='utf-8')
    print(json_text({k:v for k,v in report.items() if k!='trades'}))


if __name__=='__main__':main()
