"""Explicit enrollment of existing paper accounts. Never resets balances or positions."""
from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from rug_desk.book import atomic, encode, process_lock

SPECS = {
    'radeon': ('runtime/exploration/ledger.sqlite3', 'metadata', 'trial_manifest', 'runtime/exploration/trial.json'),
    'xenon': ('rug_desk/runtime/book.sqlite3', 'metadata', 'trial', 'rug_desk/runtime/trial.json'),
    'neon': ('neon_bot/runtime/book.sqlite3', 'metadata', 'trial', 'neon_bot/runtime/trial.json'),
    'krypton': ('krypton_bot/runtime/book.sqlite3', 'meta', 'paper_trial', 'krypton_bot/runtime/trial.json'),
    'helium': ('helium_bot/runtime/book.sqlite3', 'meta', 'paper_trial', 'helium_bot/runtime/trial.json'),
    'nitrogen': ('nitrogen_bot/runtime/book.sqlite3', 'meta', 'paper_trial', 'nitrogen_bot/runtime/trial.json'),
    'radon': ('radon_bot/runtime/book.sqlite3', 'meta', 'paper_trial', 'radon_bot/runtime/trial.json'),
}

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

@contextmanager
def connect(path, mode='ro'):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode='+mode, uri=True, timeout=20)) as db:
        with db:
            yield db

def enrollment(root, reference, now=None):
    root=Path(root); now=int(time.time()) if now is None else now
    if not reference.strip(): raise ValueError('Explicit authorization reference required')
    folder=root/'runtime/desktop'; folder.mkdir(parents=True,exist_ok=True)
    manifest=folder/'paper-autopilot.json'
    with process_lock(folder/'enroll.lock'):
        if manifest.exists():
            value=read(manifest)
            if value['state']=='ACTIVE': return value
        else:
            value={'version':1,'state':'PREPARED','mode':'paper','real_orders_enabled':False,
                   'started_at':now,'ends_at':now+604800,'authorization_reference':reference,'accounts':{}}
            # Validate all identities before mutating any account.
            for bot,(dbpath,table,key,view) in SPECS.items():
                with connect(root/dbpath) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError(bot+' ledger integrity failure')
                    rows=dict(db.execute('SELECT key,value FROM '+table))
                    old=json.loads(rows[key]) if key in rows else None
                    if bot=='radeon':
                        if not old or old['rules'].get('real_money_enabled') is not False: raise ValueError('Radeon paper binding required')
                        identity=old['account_id']
                    elif bot in ('xenon','neon'):
                        account=read((root/view).parent/'account.json')
                        if account['mode']!='paper': raise ValueError(bot+' must be paper')
                        identity=account['account_id']
                    else:
                        meta={k:json.loads(v) for k,v in rows.items()}
                        if meta.get('execution_mode',meta.get('mode'))!='paper' or meta.get('demo') is not False:
                            raise ValueError(bot+' production-paper identity required')
                        identity=meta['account_id']
                    trial=dict(old or {},account_id=identity,mode='paper',started_at=now,ends_at=now+604800,
                               authorization_reference=reference)
                    value['accounts'][bot]={'account_id':identity,'previous_trial':old,'trial':trial}
            verified=subprocess.run([sys.executable,'-X','utf8','verify_article.py'],cwd=root/'dev_watch',capture_output=True)
            if verified.returncode: raise ValueError('Oxygen article/paper verification failed')
            value['accounts']['oxygen']={'account_id':'oxygen:article-paper','trial':{
                'account_id':'oxygen:article-paper','mode':'paper','started_at':now,'ends_at':now+604800,
                'authorization_reference':reference}}
            value['aliases']={'argon':'xenon'}
            atomic(manifest,encode(value))
        for bot,(dbpath,table,key,view) in SPECS.items():
            item=value['accounts'][bot]; trial=item['trial']
            backup=folder/'trial-backup'/str(value['started_at'])/(bot+'.sqlite3')
            backup.parent.mkdir(parents=True,exist_ok=True)
            with connect(root/dbpath,'rw') as db:
                if not backup.exists():
                    with closing(sqlite3.connect(backup)) as target: db.backup(target)
                current=db.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
                current=json.loads(current[0]) if current else None
                if current not in (item['previous_trial'],trial): raise ValueError(bot+' trial changed during enrollment')
                db.execute('INSERT OR REPLACE INTO '+table+' (key,value) VALUES(?,?)',
                           (key,json.dumps(trial,sort_keys=True)))
            atomic(root/view,encode(trial))
        atomic(root/'dev_watch/runtime/trial.json',encode(value['accounts']['oxygen']['trial']))
        value['state']='ACTIVE'; atomic(manifest,encode(value))
        return value


def approved_renewal(source_root, previous, current):
    """Recognize explicit enrollment without extending a historical comparison."""
    try:
        source_root=Path(source_root).resolve()
        if source_root.name!='rug_desk':return False
        value=read(source_root.parent/'runtime/desktop/paper-autopilot.json')
        item=value['accounts']['xenon']
        return (value['state']=='ACTIVE' and value['mode']=='paper'
                and value['real_orders_enabled'] is False and item['previous_trial']==previous
                and item['trial']==current and current['account_id']==item['account_id']
                and current['ends_at']-current['started_at']==604800)
    except (OSError,ValueError,KeyError,TypeError):return False
