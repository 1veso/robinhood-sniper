"""Helium's isolated account and immutable, transaction-bound team evidence."""
from contextlib import contextmanager, nullcontext
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from sniper.krypton.paper import Paper as BaselinePaper, bound_config
from .config import TEAM_POLICY, TEAM_POLICY_HASH, digest, encode, number


def unsettled_costs(db):
    """Conservatively account for estimates/reservations without inventing invoices."""
    total = Decimal(0)
    for request_id, raw in db.execute('SELECT id,payload FROM api_requests'):
        item = json.loads(raw)
        usage = item.get('usage', {})
        if usage.get('billed_usd') is None:
            value = usage.get('estimated_usd')
            if value is None:
                value = item.get('reservation_usd', '.50' if request_id.startswith('helium:grok:') else '0')
            total += number(value)
    return total


class Paper(BaselinePaper):
    account_kind = 'helium'

    def __init__(self, folder, cfg, *, initialize=False, demo=False, clock=time.time):
        self._cockpit_ready = False
        path = Path(folder)/'book.sqlite3'
        new_account = not path.exists()
        if path.exists():
            check = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
            try:
                row = check.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()
                if not row or json.loads(row[0]) != 'helium':
                    raise ValueError('Helium requires its own account; an existing bot cannot be adopted')
            finally:
                check.close()
        super().__init__(folder, cfg, initialize=initialize, demo=demo, clock=clock)
        if initialize:
            try:
                with self.transaction():
                    self.db.execute('''CREATE TABLE IF NOT EXISTS team_tasks(
                        task_id TEXT PRIMARY KEY,mint TEXT UNIQUE NOT NULL,
                        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                        stage TEXT NOT NULL,state TEXT NOT NULL,owner TEXT NOT NULL,
                        reason TEXT NOT NULL,evidence_hash TEXT,analysis_hash TEXT,review_hash TEXT)''')
                    self.db.execute('''CREATE TABLE IF NOT EXISTS team_artifacts(
                        task_id TEXT NOT NULL,kind TEXT NOT NULL,at INTEGER NOT NULL,
                        hash TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(task_id,kind),
                        FOREIGN KEY(task_id) REFERENCES team_tasks(task_id))''')
                    self.db.execute('''CREATE TABLE IF NOT EXISTS team_events(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,
                        at INTEGER NOT NULL,payload TEXT NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES team_tasks(task_id))''')
                    if new_account:
                        self.set('team_policy', TEAM_POLICY)
                        self.set('team_policy_hash', TEAM_POLICY_HASH)
            except BaseException:
                self.close()
                raise
        # This is the writable worker/account object. Console views use their
        # own mode=ro connection and never run this additive migration.
        from .cockpit import migrate
        try:
            migrate(self,int(clock()))
            self._cockpit_ready = True
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with super().transaction():
            yield
            if self._cockpit_ready:
                from .cockpit import sync
                sync(self)

    def team_binding_current(self):
        return (self.get('bot_id') == 'helium'
                and self.get('team_policy_hash') == TEAM_POLICY_HASH
                and digest(self.get('team_policy')) == TEAM_POLICY_HASH)

    def _write(self):
        return nullcontext() if self.db.in_transaction else self.transaction()

    def discover(self, mints, now):
        from .cockpit import event
        with self._write():
            for mint in mints:
                result=self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,NULL,'PENDING')",(mint,now))
                if result.rowcount:
                    event(self,now,'candidate','scout',mint=mint,outcome='discovered',
                          reason='Candidate observed in the configured Solana discovery feed')

    def provider(self, name, value=None):
        if value is None:
            return super().provider(name)
        from .cockpit import event
        with self._write():
            result=super().provider(name,value)
            now=value.get('finished_at',value.get('attempted_at',int(self.clock())))
            event(self,now,'provider','scout',provider=name,
                  outcome=str(value.get('state','unknown')).lower(),
                  reason=value.get('error') or name+' '+str(value.get('state','unknown')).lower())
            return result

    def run_start(self, kind, now):
        from .cockpit import event
        with self._write():
            run_id=super().run_start(kind,now)
            event(self,now,'worker','guard' if kind=='watch' else 'scout',
                  outcome='running',worker=kind,run_id=run_id,reason=kind+' worker started')
            return run_id

    def run_finish(self, run_id, now, errors):
        from .cockpit import event, observe
        with self._write():
            super().run_finish(run_id,now,errors)
            kind=self.db.execute('SELECT kind FROM runs WHERE id=?',(run_id,)).fetchone()[0]
            event(self,now,'worker','guard' if kind=='watch' else 'scout',
                  outcome='error' if errors else 'idle',worker=kind,run_id=run_id,
                  reason='; '.join(errors) if errors else kind+' worker completed')
            observe(self,now,kind+'_completed')

    def begin_task(self, mint, now):
        if not self.team_binding_current():
            raise ValueError('Helium team policy changed; new entries require an explicit migration')
        with self._write():
            candidate = self.db.execute('SELECT * FROM candidates WHERE mint=?', (mint,)).fetchone()
            if not candidate or candidate['state'] != 'PENDING':
                raise ValueError('Current candidate required')
            task_id = digest({'account_id': self.identity, 'config_hash': digest(self.cfg),
                              'team_policy_hash': TEAM_POLICY_HASH, 'mint': mint,
                              'first_seen': candidate['first_seen']})
            if self.db.execute('SELECT 1 FROM team_tasks WHERE mint=?', (mint,)).fetchone():
                raise ValueError('Candidate already has a durable task; no model replay')
            self.db.execute('INSERT INTO team_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (task_id,mint,now,now,'discovery','RUNNING','Scout',
                             'Collect current pinned-pool observations',None,None,None))
            self.handoff(task_id,'discovery','Scout','Collect current pinned-pool observations',now)
        return task_id

    def handoff(self, task_id, stage, owner, reason, now, state='RUNNING'):
        with self._write():
            row = self.db.execute('SELECT state FROM team_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row or row['state'] != 'RUNNING':
                raise ValueError('Task is absent or already terminal')
            event = {'task_id':task_id,'account_id':self.identity,'stage':stage,'state':state,
                     'owner':owner,'reason':str(reason)[:500],'updated_at':now}
            self.db.execute('INSERT INTO team_events(task_id,at,payload) VALUES(?,?,?)',
                            (task_id,now,encode(event)))
            self.db.execute('UPDATE team_tasks SET updated_at=?,stage=?,state=?,owner=?,reason=? WHERE task_id=?',
                            (now,stage,state,owner,event['reason'],task_id))

    def record_artifact(self, task_id, kind, value, now):
        if kind not in ('evidence','analysis','review'):
            raise ValueError('Unsupported team artifact')
        fingerprint = digest(value)
        with self._write():
            task = self.db.execute('SELECT state FROM team_tasks WHERE task_id=?',(task_id,)).fetchone()
            if not task or task['state'] != 'RUNNING':
                raise ValueError('An active task is required for evidence')
            previous = self.db.execute('SELECT hash FROM team_artifacts WHERE task_id=? AND kind=?',
                                       (task_id,kind)).fetchone()
            if previous:
                if previous['hash'] != fingerprint:
                    raise ValueError('Completed evidence is immutable')
                return
            self.db.execute('INSERT INTO team_artifacts VALUES(?,?,?,?,?)',
                            (task_id,kind,now,fingerprint,encode(value)))
            self.db.execute('UPDATE team_tasks SET '+kind+'_hash=? WHERE task_id=?',(fingerprint,task_id))
            if kind == 'evidence':
                from .cockpit import market
                market(self,value.get('quote') or {},now,'candidate_evidence')

    def artifact(self, task_id, kind):
        row = self.db.execute('SELECT payload,hash FROM team_artifacts WHERE task_id=? AND kind=?',
                              (task_id,kind)).fetchone()
        if not row:
            raise ValueError('Required team evidence is missing')
        value = json.loads(row['payload'])
        if digest(value) != row['hash']:
            raise ValueError('Team artifact integrity mismatch')
        return value

    def recover_tasks(self, now):
        """A crash may have billed a request. Record uncertainty instead of replaying."""
        with self._write():
            for row in self.db.execute("SELECT mint FROM team_tasks WHERE state='RUNNING'").fetchall():
                self.decision(row['mint'],'ERROR',
                    {'reason':'Interrupted team task; progress retained, model request not retried'},now,terminal=True)

    def decision(self, mint, action, payload, now, *, terminal=False):
        with self._write():
            task = self.db.execute('SELECT * FROM team_tasks WHERE mint=?',(mint,)).fetchone()
            if task:
                payload = dict(payload,team_task_id=task['task_id'],team_policy_hash=self.get('team_policy_hash'))
                if terminal and task['state'] == 'RUNNING':
                    owner = 'Executor' if action == 'BUY' else task['owner']
                    self.handoff(task['task_id'],'complete',owner,payload.get('reason',action),now,state=action)
            super().decision(mint,action,payload,now,terminal=terminal)

    def _validate_admission(self, mint, evidence, verdict, now):
        from .reviewer import validate_response, decision
        if not self.team_binding_current():
            raise ValueError('Team policy binding no longer matches')
        task = self.db.execute('SELECT * FROM team_tasks WHERE mint=?',(mint,)).fetchone()
        if not task or task['state'] != 'RUNNING' or task['stage'] != 'execution':
            raise ValueError('A completed Helium team review is required')
        task_id = task['task_id']
        if (digest(self.artifact(task_id,'evidence')) != digest(evidence)
            or digest(self.artifact(task_id,'analysis')) != digest(verdict)):
            raise ValueError('Final inputs differ from the independently reviewed evidence')
        review = self.artifact(task_id,'review')
        expected = {'account_id':self.identity,'config_hash':digest(self.cfg),
                    'team_policy_hash':TEAM_POLICY_HASH,'task_id':task_id,
                    'evidence_hash':digest(evidence),'analysis_hash':digest(verdict)}
        if (any(review.get(k) != v for k,v in expected.items())
            or review.get('validated') is not True or review.get('decision') != 'BUY'
            or type(review.get('judged_at')) is not int
            or not 0 <= now-review['judged_at'] <= TEAM_POLICY['review_max_age_seconds']
            or decision(validate_response(review.get('response'))) != 'BUY'):
            raise ValueError('Fresh bound Jev acceptance is required')
        if review.get('demo') is True:
            if not self.demo:
                raise ValueError('Synthetic team evidence cannot authorize production entries')
        else:
            row = self.db.execute('SELECT payload FROM api_requests WHERE id=?',
                                  (review.get('request_id'),)).fetchone()
            audit = json.loads(row[0]) if row else {}
            if (audit.get('state') != 'RECEIVED' or audit.get('provider') != 'jev'
                or audit.get('task_id') != task_id or audit.get('evidence_hash') != digest(evidence)
                or audit.get('analysis_hash') != digest(verdict)
                or audit.get('result') != review):
                raise ValueError('Recorded Jev request does not support this entry')

    def equity(self, now):
        total, unresolved = super().equity(now)
        return total-unsettled_costs(self.db), unresolved

    def snapshot(self, now):
        from .view import snapshot
        return snapshot(self.db,now)
