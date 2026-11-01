"""Independent bounded search/judge, exit and report responsibilities."""
from datetime import datetime
from decimal import Decimal as D
import json
from zoneinfo import ZoneInfo
from rug_desk.book import atomic
from sniper.execution_costs import simulate
from .config import load, digest, encode, number
from .model import Model
from .providers import Providers


def scan(book,providers=None,model=None):
    providers=providers or Providers(book.root,book,book.clock);model=model or Model(book.root,book,book.clock)
    now=int(book.clock());run=book.run_start('scan',now);errors=[]
    try:
        book.recover(now)
        if digest(load(book.root))!=digest(book.cfg):raise ValueError('Edited policy differs from account binding')
        book.set('model_gate',model.gate())
        if book.get('entries_paused') is True:return []
        try:
            for purchase in providers.search():book.discover_purchase(purchase,int(book.clock()))
        except Exception as exc:errors.append('search: '+str(exc)[:180])
        rows=book.db.execute("SELECT id FROM leads WHERE state IN ('SEARCH','WATCH','EVIDENCE_BLOCKED','WHALE_BLOCKED') AND (due_at IS NULL OR due_at<=?) ORDER BY COALESCE((SELECT max(at) FROM decisions WHERE json_extract(payload,'$.lead')=leads.id),first_seen),first_seen LIMIT ?",(int(book.clock()),book.cfg['candidates_per_scan'])).fetchall()
        for row in rows:
            try:book.assess(row['id'],providers.evidence(book.lead(row['id'])),model,int(book.clock()))
            except Exception as exc:
                errors.append('candidate '+row['id'][:12]+': '+type(exc).__name__)
                book.decision(book.lead(row['id'])['mint'],'EVIDENCE_ERROR',{'lead':row['id'],'reason':str(exc)[:180]},int(book.clock()))
    except Exception as exc:errors.append(str(exc)[:200])
    finally:book.run_finish(run,int(book.clock()),errors);book.publish()
    return errors


def approve(book,id,providers=None):
    order=book.order(id)
    if order['status']!='PENDING':return order['status']
    now=int(book.clock())
    if now>=order['expires']:
        with book.transaction():book.expire_orders(now)
        return 'EXPIRED'
    if digest(load(book.root))!=digest(book.cfg):raise ValueError('Edited policy differs from account binding')
    providers=providers or Providers(book.root,book,book.clock)
    # Network I/O precedes the atomic order/limits/fill transaction.
    evidence=providers.evidence(book.lead(order['lead']))
    result=book.approve(id,evidence,int(book.clock()));book.publish();return result


def watch(book,providers=None):
    providers=providers or Providers(book.root,book,book.clock);run=book.run_start('watch',int(book.clock()));errors=[]
    try:
        for p in book.positions():
            try:book.watch(providers.mark(p['mint'],p['pool']),int(book.clock()))
            except Exception as exc:errors.append(p['mint']+': '+type(exc).__name__)
    finally:book.run_finish(run,int(book.clock()),errors);book.publish()
    return errors


def follow_outcomes(book,providers):
    now=int(book.clock())
    for row in book.db.execute("SELECT * FROM outcomes WHERE status='PENDING' AND due_at<=? LIMIT 20",(now,)).fetchall():
        p=json.loads(row['payload']);status='UNKNOWN'
        try:
            if now-row['due_at']>300:raise ValueError('24-hour observation window missed')
            q=providers.mark(p['mint'],p['pool']);now=int(book.clock());book.valid_quote(q,now,p['mint'],p['pool'])
            if abs(q['observed_at']-row['due_at'])>300:raise ValueError('Outcome timestamp outside fixed window')
            sale=simulate(book.costs,'sell',q,number(p['entry']['quantity']),now)
            p.update(outcome_quote=q,exit=sale,net_pnl=str(number(sale['quantity'])*number(sale['fill_price'])-number(sale['explicit_fee_usd'])-number(p['ticket_usd'])-number(p['entry']['explicit_fee_usd'])))
            status='OBSERVED'
        except Exception as exc:
            if now-row['due_at']<=300:continue
            p['unknown_reason']='Missing fresh pinned-pool 24-hour mark'
        with book.transaction():book.db.execute('UPDATE outcomes SET status=?,payload=? WHERE assessment=?',(status,encode(p),row['assessment']))


def head(book,providers=None,scheduled=False):
    now=int(book.clock());run=book.run_start('head',now);errors=[]
    try:
        providers=providers or Providers(book.root,book,book.clock)
        follow_outcomes(book,providers)
        with book.transaction():book.expire_orders(now)
        local=datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin'));key='evening:'+local.date().isoformat()
        due=local.hour>=21 and not book.db.execute('SELECT 1 FROM reports WHERE id=?',(key,)).fetchone()
        if not scheduled or due:
            from .report import report
            value=report(book,now)
            with book.transaction():book.db.execute('INSERT OR IGNORE INTO reports VALUES(?,?,?)',(key if scheduled else 'manual:'+str(now),now,encode(value)))
            atomic(book.folder/'reports'/(local.date().isoformat()+'.json'),encode(value))
            return value
        return {'state':'NOT_DUE','next_schedule':'21:00 Europe/Berlin; Sunday probability cohorts included'}
    except Exception as exc:errors.append(type(exc).__name__);raise
    finally:book.run_finish(run,int(book.clock()),errors);book.publish()
