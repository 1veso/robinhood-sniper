"""NITROGEN — slot-seven Jev Judgment Desk. Explicitly approved paper fills only."""
import argparse
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from rug_desk.book import atomic,process_lock
from sniper.nitrogen.config import load,encode
from sniper.nitrogen.book import Book,bound_config,folder_for


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',nargs='?',default='status',choices=['init','status','scan','watch','report','head','pending','approve','reject','import','pause','resume','model-status','model-binding','reconcile','demo'])
    p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--id');p.add_argument('--source',type=Path)
    p.add_argument('--mode',choices=['paper'],default='paper');p.add_argument('--scheduled',action='store_true')
    a=p.parse_args(argv);root=a.root.resolve()
    if a.command=='status':
        from sniper.nitrogen.view import view
        print(encode(view(root)));return 0
    if a.command=='demo':
        from sniper.nitrogen.demo import demo
        print(encode(demo(root)));return 0
    if a.command=='init':
        with closing(Book(root,load(root),initialize=True)) as b:
            auth=b.folder/'paid-jev.json'
            if not auth.exists():atomic(auth,encode({'enabled':False,'operator_authorized':False,'approval_reference':'','binding':None,'daily_budget_usd':'0'}))
            b.publish();print(encode({'bot':'nitrogen','slot':7,'account_id':b.identity,'starting_cash':b.cfg['starting_cash_usd'],'paid_calls_enabled':False}))
        return 0
    if a.command in ('approve','reject') and not a.id:p.error('--id <order-id> is required; see pending')
    with process_lock(folder_for(root)/(('scan' if a.command=='scan' else a.command)+'.lock')):
        with closing(Book(root,bound_config(root))) as b:
            from sniper.nitrogen import service
            if a.command=='pending':
                with b.transaction():b.expire_orders(int(time.time()))
                result={'instructions':'APPROVE #id → python nitrogen_bot.py approve --id <order-id>','orders':b.pending()}
            elif a.command=='approve':result=service.approve(b,a.id)
            elif a.command=='reject':result=b.reject_order(a.id,int(time.time()));b.publish()
            elif a.command=='import':
                if not a.source:p.error('--source is required')
                if a.source.stat().st_size>1000000:p.error('Factual supplement exceeds 1 MB')
                value=json.loads(a.source.read_text(encoding='utf-8-sig'));b.import_evidence(value);result={'imported':True,'scope':'account-bound factual observations; no judgment or fill'}
            elif a.command in ('pause','resume'):
                b.set('entries_paused',a.command=='pause');result={'entries_paused':a.command=='pause','exits_independent':True};b.publish()
            elif a.command=='reconcile':
                if not a.source:p.error('--source is required for the reviewed provider invoice')
                from sniper.nitrogen.model import reconcile
                result=reconcile(b,json.loads(a.source.read_text(encoding='utf-8-sig')));b.publish()
            elif a.command in ('model-status','model-binding'):
                from sniper.nitrogen.model import Model
                m=Model(root,b);result=m.gate() if a.command=='model-status' else m.binding()
            elif a.command in ('report','head'):result=service.head(b,scheduled=a.scheduled)
            else:result=getattr(service,a.command)(b)
            print(encode(result));return 0 if not isinstance(result,list) or not result else 2


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,sqlite3.Error,RuntimeError) as exc:
        print('NITROGEN stopped safely: '+(str(exc)[:250] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__),file=sys.stderr);sys.exit(2)
