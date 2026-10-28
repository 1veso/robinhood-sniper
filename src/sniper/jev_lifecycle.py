"""Explicit promotions. Time, returns and model confidence grant no authority."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import time

from .jev_review import Journal, reader
from .jev_dossier import encode, digest

STAGES=('SHADOW','ACTIVE_PAPER','LIVE_READY','LIVE')
PAPER_CHECKS=('prospective_entry','duplicate_no_extra_order','failure_closed','hard_rejection',
    'final_transaction_recheck','watch_restart','original_exits','disabled_baseline','paper_live_separation')
CODE_FILES=('src/sniper/jev.py','src/sniper/jev_dossier.py','src/sniper/jev_review.py',
    'src/sniper/jev_lifecycle.py','src/sniper/jev_admission.py','rug_desk/check.py',
    'src/sniper/jev_readiness.py','src/sniper/jev_deployment.py','rug_desk/book.py','rug_desk/report.py','solana_executor.py','src/sniper/execution_solana_service.py',
    'src/sniper/execution_solana.py','src/sniper/solana_cpmm.py','src/sniper/solana_wire.py','src/sniper/live_readiness.py')


def code_binding(root):
    return {p:hashlib.sha256((Path(root)/p).read_bytes()).hexdigest() for p in CODE_FILES}


def current(root):
    path=Path(root)/'runtime/jev-review/assessments.sqlite3'
    default={'stage':'SHADOW','paper_account_id':None,'live_account_id':None,'transition_id':None}
    if not path.exists():return default
    with closing(reader(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='promotion_events'").fetchone():return default
        row=db.execute('SELECT id,payload FROM promotion_events ORDER BY id DESC LIMIT 1').fetchone()
    return {**json.loads(row['payload']),'transition_id':row['id']} if row else default


def record(root,target,evidence,reference,*,paper_account_id=None,live_account_id=None,now=None):
    if target not in STAGES or not isinstance(reference,str) or not reference.strip():
        raise ValueError('An explicit transition and authorization reference are required')
    now=int(time.time()) if now is None else now
    with closing(Journal(root)) as j:
        j.db.execute('CREATE TABLE IF NOT EXISTS promotion_events(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL)')
        j.db.execute('BEGIN IMMEDIATE')
        try:
            row=j.db.execute('SELECT payload FROM promotion_events ORDER BY id DESC LIMIT 1').fetchone()
            prior=json.loads(row[0]) if row else {'stage':'SHADOW','paper_account_id':None,'live_account_id':None}
            if STAGES.index(target)!=STAGES.index(prior['stage'])+1:
                raise ValueError('Promotion must advance exactly one explicit stage')
            paper=paper_account_id or prior['paper_account_id'];live=live_account_id or prior['live_account_id']
            if not paper or (live is not None and live==paper):raise ValueError('Paper and live identities must be separate')
            from .jev_review import sources
            accounts=[identity['account_id'] for source,identity,_,_ in sources(root,include_checks=False) if source=='rug_desk']
            if accounts!=[paper]:raise ValueError('Existing RUG DESK paper identity required')
            if target=='ACTIVE_PAPER':
                if (evidence.get('scope')!='JEV_PAPER_INTEGRATION_ACCEPTANCE' or evidence.get('code_binding')!=code_binding(root)
                    or evidence.get('account_id')!=paper or set(evidence.get('checks',{}))!=set(PAPER_CHECKS)
                    or any(v!='PASS' for v in evidence['checks'].values())
                    or type(evidence.get('tested_at'))is not int or not 0<=now-evidence['tested_at']<=86400):
                    raise ValueError('Current bound paper integration acceptance required')
            else:
                requirement='LIVE_READY' if target=='LIVE_READY' else 'LIVE'
                from .jev_readiness import report
                latest=report(root,now=now)
                rows=[x for x in latest['requirements'] if x['required_for']==requirement]
                if not rows or any(x['status']!='PASS' for x in rows):
                    raise ValueError('Promotion requirements are not all PASS')
                from .jev_readiness import read
                draft=read(Path(root)/'runtime/deployment-review/jev-solana-live-draft.json')
                if not live or live!=draft.get('account_id'):raise ValueError('Separate reviewed live identity required')
            payload={'stage':target,'previous_stage':prior['stage'],'paper_account_id':paper,'live_account_id':live,
                'authorization_reference':reference,'at':now,'evidence_sha256':digest(evidence),'evidence':evidence,
                'automatic_promotion':False,'paid_authorization_unchanged':True}
            if target in ('LIVE_READY','LIVE'):
                from .jev_deployment import operating_digest
                payload['reviewed_operating_sha256']=operating_digest(draft)
            j.db.execute('INSERT INTO promotion_events(at,payload) VALUES(?,?)',(now,encode(payload)))
            j.db.commit()
        except Exception:j.db.rollback();raise
    return current(root)


def enabled_for(root,account_id):
    state=current(root)
    return state['stage'] in ('ACTIVE_PAPER','LIVE_READY','LIVE') and state['paper_account_id']==account_id
