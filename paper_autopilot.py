"""Persistent local paper supervisor. No signer, order execution, or Telegram delivery."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
WORKER_PYTHON=str(Path(sys.executable).with_name('python.exe')) if os.name=='nt' else sys.executable
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
import desktop
from rug_desk.book import atomic, encode, process_lock
from sniper.autopilot_trial import enrollment, read
from sniper.process_tree import launch

EXTRA={
    'experimental-paper':(60,190,['experimental_paper.py','once']),
    'crew':(60,45,['crew.py','run']),
    'oxygen-watch':(600,590,['oxygen_bot.py','watch']),
    'oxygen-scan':(7200,600,['oxygen_bot.py','scan']),
    'oxygen-pulse':(3600,300,['oxygen_bot.py','pulse']),
    'oxygen-report':(60,90,['oxygen_bot.py','report']),
}
JOBS={name:(v[1]*60,v[2]+30 if v[2] else 0,
           ['desktop.py','worker',name,'--no-send']) for name,v in desktop.JOBS.items()}
JOBS.update(EXTRA)
ENTRY_JOBS={'scan','neon-signals','krypton-scan','helium-scan','nitrogen-scan','radon-entries','oxygen-scan'}
WINDOW_JOBS=ENTRY_JOBS|{'research','crew','radon-research','jev-review','radeon-sizing','shadow'}

def window_open(manifest, now):
    return (manifest.get('state')=='ACTIVE' and manifest.get('mode')=='paper'
            and manifest.get('real_orders_enabled') is False
            and manifest['ends_at']-manifest['started_at']==604800
            and manifest['started_at']<=now<manifest['ends_at'])

def dashboard_ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/',timeout=2) as response:
            return response.status==200
    except Exception:return False

def once(name,root=ROOT):
    with process_lock(root/'runtime/desktop'/(name+'.autopilot-job.lock')):
        state=read(root/'runtime/desktop/paper-autopilot.json')
        if name in WINDOW_JOBS and not window_open(state,time.time()): return 0
        os.environ['SNIPER_WORKER_LOG']='1'
        if name in desktop.JOBS:return desktop.worker(name,root,no_send=True)
        return desktop.child(EXTRA[name][2],EXTRA[name][1],root)

def run(root=ROOT):
    folder=root/'runtime/desktop'; statepath=folder/'autopilot-status.json'
    manifest=read(folder/'paper-autopilot.json')
    if manifest['state']!='ACTIVE' or manifest['mode']!='paper' or manifest['real_orders_enabled'] is not False:
        raise ValueError('Explicit paper enrollment required')
    active={}; handles={}; trees={}
    with process_lock(folder/'autopilot.lock'):
        old=read(statepath) if statepath.exists() else {}
        records=old.get('jobs',{})
        for i,name in enumerate(JOBS):
            records.setdefault(name,{'next_at':int(time.time())+i*2,'phase':'SCHEDULED'})
            # A restart keeps paid jobs' already-reserved cadence, never rapid retries.
            if records[name].get('phase')=='RUNNING':records[name]['phase']='INTERRUPTED'
            if name=='console':records[name]['next_at']=0
        try:
            while True:
                now=int(time.time()); manifest=read(folder/'paper-autopilot.json')
                for name,process in list(active.items()):
                    record=records[name]; cap=JOBS[name][1]
                    expired=(name in ENTRY_JOBS and not window_open(manifest,now))
                    timedout=bool(cap and now-record['started_at']>cap)
                    if process.poll() is None and (expired or timedout):trees[name].close()
                    code=process.poll()
                    if code is not None:
                        record.update(phase='COMPLETE' if code==0 else 'FAILED',exit_code=code,
                                      finished_at=now,deadline_stopped=expired,timeout=timedout)
                        trees.pop(name).close();handles.pop(name).close();del active[name]
                        record['next_at']=max(record['next_at'],now+JOBS[name][0])
                launched=0
                for name in sorted(JOBS,key=lambda n:records[n].get('next_at',0)):
                    interval=JOBS[name][0]
                    if len(active)>=8 or launched>=2:break
                    if name in active or now<records[name].get('next_at',0):continue
                    record=records[name]
                    if name in WINDOW_JOBS and not window_open(manifest,now):
                        record.update(phase='TRIAL_ENDED',next_at=now+60);continue
                    if name=='oxygen-report':
                        local=datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin'))
                        if (local.hour,local.minute)!=(21,30) or record.get('report_day')==local.date().isoformat():
                            record['next_at']=now+30;continue
                        record['report_day']=local.date().isoformat()
                    if name=='console' and dashboard_ready():
                        record.update(phase='SERVING',next_at=now+15);continue
                    record.update(next_at=now+interval,started_at=now,phase='STARTING')
                    # Persist cadence BEFORE launching a process that could incur API costs.
                    atomic(statepath,encode({'mode':'paper','pid':os.getpid(),'updated_at':now,'jobs':records}))
                    log=folder/(name+'.log')
                    if log.exists() and log.stat().st_size>5_000_000:log.replace(folder/(name+'.previous.log'))
                    handle=log.open('ab');handles[name]=handle
                    try:
                        process,tree=launch([WORKER_PYTHON,'-X','utf8',str(root/'paper_autopilot.py'),'once',name],
                            cwd=root,stdout=handle,stderr=handle,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                            start_new_session=os.name!='nt')
                        trees[name]=tree;launched+=1
                        active[name]=process;record.update(phase='RUNNING',pid=process.pid)
                    except Exception as exc:
                        handle.close();handles.pop(name)
                        record.update(phase='FAILED',error=type(exc).__name__)
                atomic(statepath,encode({'mode':'paper','real_orders_enabled':False,'pid':os.getpid(),
                       'updated_at':now,'trial_ends_at':manifest['ends_at'],'jobs':records}))
                time.sleep(2)
        finally:
            for tree in trees.values():tree.close()
            for handle in handles.values():handle.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['enroll','run','once','status']);p.add_argument('job',nargs='?',choices=JOBS)
    p.add_argument('--authorization-reference')
    a=p.parse_args()
    if a.command=='enroll':
        value=enrollment(ROOT,a.authorization_reference or '')
        print(encode({'state':value['state'],'mode':value['mode'],'started_at':value['started_at'],
                      'ends_at':value['ends_at'],'accounts':list(value['accounts']),'aliases':value['aliases']}));return 0
    if a.command=='once':
        if not a.job:p.error('job required')
        return once(a.job)
    if a.command=='run':return run()
    value=read(ROOT/'runtime/desktop/autopilot-status.json');print(encode(value));return 0

if __name__=='__main__':
    if sys.stdout is None:sys.stdout=open(ROOT/'runtime/desktop/supervisor.log','a',encoding='utf-8',buffering=1)
    if sys.stderr is None:sys.stderr=sys.stdout
    try:raise SystemExit(main())
    except Exception as exc:
        import traceback
        frames=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]
        print(encode({'error_type':type(exc).__name__,'frames':frames}),file=sys.stderr)
        raise SystemExit(2)
