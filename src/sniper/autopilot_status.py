"""Allowlisted local supervisor observations for the read-only dashboard."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}

def public_status(root,bot,now=None):
    now=int(time.time()) if now is None else now
    folder=Path(root)/'runtime/desktop';m=read(folder/'paper-autopilot.json');s=read(folder/'autopilot-status.json')
    bot={'robinhood':'radeon','solana':'xenon','jev':'xenon','devwatch':'oxygen'}.get(bot,bot)
    trial=m.get('accounts',{}).get(bot,{}).get('trial',{})
    return {'enrolled':m.get('state')=='ACTIVE' and bool(trial),'mode':'paper',
            'started_at':trial.get('started_at'),'ends_at':trial.get('ends_at'),
            'window_open':bool(trial and trial['started_at']<=now<trial['ends_at']),
            'supervisor_fresh':s.get('mode')=='paper' and 0<=now-s.get('updated_at',0)<=30,
            'real_orders_enabled':False}

def supervisor_jobs(root,now=None):
    now=int(time.time()) if now is None else now
    folder=Path(root)/'runtime/desktop';m=read(folder/'paper-autopilot.json');s=read(folder/'autopilot-status.json')
    if m.get('state')!='ACTIVE':return None
    import desktop
    fresh=s.get('mode')=='paper' and 0<=now-s.get('updated_at',0)<=30
    items=[]
    for name,record in s.get('jobs',{}).items():
        title=desktop.JOBS[name][0] if name in desktop.JOBS else name
        next_at=record.get('next_at')
        items.append({'name':title,'state':record.get('phase') if fresh else 'SUPERVISOR_STALE',
            'last_result':record.get('exit_code'),
            'last_run':datetime.fromtimestamp(record['started_at'],timezone.utc).isoformat() if record.get('started_at') else None,
            'next_run':datetime.fromtimestamp(next_at,timezone.utc).isoformat() if next_at else None,
            'account_root_bound':True})
    return {'available':fresh,'checked_at':s.get('updated_at'),'items':items,
            'source':'persistent_paper_supervisor','error':None if fresh else 'supervisor_stale'}
