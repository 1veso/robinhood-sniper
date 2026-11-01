"""Explicitly authorized isolated paper experiments on live public chain data."""
import argparse
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from rug_desk.book import atomic,encode,process_lock
from sniper.experimental_paper import Account,CATALOG,snapshot
from sniper.experimental_feed import Feed,SourceUnavailable

def run(root=ROOT,limit=12):
 folder=root/'runtime/experiments';started=int(time.time());status={'started_at':started,'mode':'experimental_paper','real_orders_enabled':False,'errors':[],'rejections':[],'buys':[],'sells':[]}
 with process_lock(folder/'worker.lock'):
  accounts={bot:Account(root,bot) for bot in CATALOG}
  try:
   cooldown=folder/'source-backoff.json'
   if cooldown.exists() and json.loads(cooldown.read_text()).get('retry_after',0)>time.time():
    status['state']='SOURCE_BACKOFF';return status
   feed=Feed(root);evidence={};held={}
   for book in accounts.values():
    for p in book.positions():
     key=(p['mint'],p['pool'])
     if key not in held or p['marked_at']<held[key]['marked_at']:held[key]=p
   # Exit/mark work comes before entry work. Failed reads never fabricate exits.
   for mint,pool in sorted(held,key=lambda key:held[key]['marked_at']):
    if time.time()-started>95:break
    try:
     e=feed.capture(mint,pool);evidence[(mint,pool)]=e
     for bot,book in accounts.items():
      if book.watch(e,int(time.time())):status['sells'].append({'bot':bot,'mint':mint,'simulated':True})
    except SourceUnavailable as exc:
     atomic(cooldown,encode({'retry_after':int(time.time())+120,'error_type':str(exc)}));raise
    except Exception as exc:status['errors'].append({'stage':'watch','mint':mint,'error_type':type(exc).__name__,'reason':str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__})
   if any(b.get('started_at')<=time.time()<b.get('ends_at') and len(b.positions())<b.cfg['max_open'] for b in accounts.values()) and time.time()-started<100:
    candidates=feed.candidates();cursor_path=folder/'discovery.json'
    prior=json.loads(cursor_path.read_text()) if cursor_path.exists() else {};offset=prior.get('offset',0)%max(1,len(candidates))
    candidates=candidates[offset:]+candidates[:offset]
    selected=candidates[:limit];atomic(cursor_path,encode({'offset':offset+len(selected),'at':int(time.time())}))
    for mint in selected:
     if time.time()-started>130:break
     try:
      e=feed.capture(mint);now=int(time.time())
      for bot,book in accounts.items():
       if book.observe(e,now):status['buys'].append({'bot':bot,'mint':mint,'pool':e['quote']['pool'],'simulated':True})
     except SourceUnavailable as exc:
      atomic(cooldown,encode({'retry_after':int(time.time())+120,'error_type':str(exc)}));raise
     except ValueError as exc:status['rejections'].append({'mint':mint,'reason':str(exc)[:160]})
     except Exception as exc:status['errors'].append({'stage':'entry','mint':mint,'error_type':type(exc).__name__,'reason':str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__})
  except Exception as exc:
   if isinstance(exc,SourceUnavailable):atomic(folder/'source-backoff.json',encode({'retry_after':int(time.time())+120,'error_type':str(exc)}))
   status['errors'].append({'stage':'source','error_type':type(exc).__name__})
  finally:
   for book in accounts.values():book.close()
   status['finished_at']=int(time.time());atomic(folder/'status.json',encode(status))
 return status

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['init','once','report']);p.add_argument('--reference');a=p.parse_args()
 if a.command=='init':
  if not a.reference:p.error('--reference required')
  now=int(time.time())
  for bot in CATALOG:Account(ROOT,bot,initialize=True,reference=a.reference,now=now).close()
  print(json.dumps({'initialized':list(CATALOG),'real_orders_enabled':False}))
 elif a.command=='once':print(json.dumps(run()))
 else:print(json.dumps(snapshot(ROOT),default=str))
if __name__=='__main__':main()
