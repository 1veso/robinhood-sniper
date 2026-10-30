"""Independent transactional paper account. SQLite is authoritative; views are disposable."""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal as D
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo
from rug_desk.book import atomic, process_lock
from sniper.execution_costs import simulate
from .config import encode, digest, number, cost_policy
from .checks import hard_checks, all_pass


def day(now): return datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin')).date().isoformat()


def bound_config(folder):
    """Exit workers retain approved exits even when the editable config drifts."""
    path=Path(folder)/'book.sqlite3'
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    try:
        rows=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('config','config_hash')"))
        cfg=json.loads(rows['config'])
        if digest(cfg)!=json.loads(rows['config_hash']):raise ValueError('Stored account policy binding invalid')
        return cfg
    finally:db.close()


class Paper:
    account_kind = 'krypton'

    def __init__(self,folder,cfg,*,initialize=False,demo=False,clock=time.time):
        self.folder,self.cfg,self.clock=Path(folder),cfg,clock
        path=self.folder/'book.sqlite3'
        if not path.exists() and not initialize: raise ValueError('Initialize Krypton explicitly before running workers')
        if path.exists():
            probe=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
            try:
                row=probe.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()
                kind=json.loads(row[0]) if row else 'krypton'
                if kind!=self.account_kind:raise ValueError('Account belongs to another bot')
            finally:probe.close()
        self.folder.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=3,isolation_level=None); self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA foreign_keys=ON')
        if initialize:
            self.db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(mint TEXT PRIMARY KEY,first_seen INTEGER NOT NULL,pool TEXT,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,mint TEXT,action TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(mint TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,mint TEXT NOT NULL,side TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS providers(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,started_at INTEGER NOT NULL,finished_at INTEGER,state TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY,baseline TEXT NOT NULL,halted INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,mint TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_requests(id TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
            with self.transaction():
                if self.get('account_id') is None:
                    for k,v in {'account_id':str(uuid.uuid4()),'bot_id':self.account_kind,'config':cfg,'config_hash':digest(cfg),
                        'cost_policy':cost_policy(),'cash':cfg['starting_cash_usd'],'rent_reserved':'0',
                        'created_at':int(clock()),'demo':demo,'execution_mode':'paper','log_cursor':0}.items():self.set(k,v)
        if self.get('config_hash')!=digest(cfg) or self.get('execution_mode')!='paper' or self.get('demo') is not demo:
            self.db.close(); raise ValueError('Account/configuration/mode mismatch; migration required, never reset')
        self.identity=self.get('account_id');self.demo=demo;self.costs=self.get('cost_policy')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try: yield; self.db.commit()
        except BaseException: self.db.rollback(); raise

    def get(self,key):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self,key,value): self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,encode(value)))
    def close(self): self.db.close()
    def provider(self,name,value=None):
        if value is not None:self.db.execute('INSERT OR REPLACE INTO providers VALUES(?,?)',(name,encode(value)))
        row=self.db.execute('SELECT payload FROM providers WHERE name=?',(name,)).fetchone()
        return json.loads(row[0]) if row else None

    def discover(self,mints,now):
        with self.transaction():
            for mint in mints:self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,NULL,'PENDING')",(mint,now))

    def pending(self):return [dict(r) for r in self.db.execute("SELECT * FROM candidates WHERE state='PENDING' ORDER BY first_seen,mint LIMIT ?",(self.cfg['candidates_per_scan'],))]
    def expire_pending(self,now):
        with self.transaction():
            expired=self.db.execute("SELECT mint FROM candidates WHERE state='PENDING' AND first_seen<? ORDER BY first_seen,mint LIMIT 100",(now-self.cfg['candidate_expiry_seconds'],)).fetchall()
            for row in expired:self.decision(row[0],'SKIP',{'reason':'Candidate expired without admission'},now,terminal=True)
    def pin(self,mint,pool):
        with self.transaction():
            row=self.db.execute('SELECT pool FROM candidates WHERE mint=?',(mint,)).fetchone()
            if not row or row[0] not in (None,pool):raise ValueError('Pinned pool mismatch')
            self.db.execute('UPDATE candidates SET pool=? WHERE mint=?',(pool,mint))

    def decision(self,mint,action,payload,now,*,terminal=False):
        self.db.execute('INSERT INTO decisions(at,mint,action,payload) VALUES(?,?,?,?)',
            (now,mint,action,encode(dict(payload,account_id=self.identity,config_hash=digest(self.cfg),demo=self.demo))))
        if terminal:self.db.execute('UPDATE candidates SET state=? WHERE mint=?',(action,mint))

    def reject(self,mint,payload,now,action='SKIP'):
        with self.transaction():self.decision(mint,action,payload,now,terminal=True)

    def positions(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM positions ORDER BY mint')]
    def valid_quote(self,q,now,mint=None,pool=None):
        from sniper.adapters.solana import validate_address
        validate_address(q['mint']);validate_address(q['pool']);number(q['price'],True);number(q['liquidity'],True)
        if mint is not None and (q['mint']!=mint or q['pool']!=pool):raise ValueError('Position pool mismatch')
        if type(q.get('observed_at')) is not int or not 0<=now-q['observed_at']<=self.cfg['quote_max_age_seconds']:raise ValueError('Stale/future pool mark')

    def equity(self,now):
        total=number(self.get('cash')); unresolved=0
        for row in self.db.execute('SELECT payload FROM api_requests'):
            usage=json.loads(row[0]).get('usage',{})
            if usage.get('billed_usd') is not None:total-=number(usage['billed_usd'])
        for p in self.positions():
            mark=p.get('mark')
            if mark and 0<=now-mark['at']<=self.cfg['quote_max_age_seconds']:total+=number(mark['net_value'])
            else: unresolved+=1
        return total,unresolved

    def daily(self,now):
        equity,_=self.equity(now);date=day(now)
        row=self.db.execute('SELECT * FROM days WHERE day=?',(date,)).fetchone()
        if row is None:
            prior=self.get('last_equity')
            baseline=number(prior['equity']) if prior else number(self.cfg['starting_cash_usd'])
            self.db.execute('INSERT INTO days VALUES(?,?,0)',(date,str(baseline)))
            row=self.db.execute('SELECT * FROM days WHERE day=?',(date,)).fetchone()
        baseline=number(row['baseline']);halted=bool(row['halted']) or (baseline>0 and equity<=baseline*(1-number(self.cfg['max_daily_loss_pct'])))
        if halted:self.db.execute('UPDATE days SET halted=1 WHERE day=?',(date,))
        self.set('last_equity',{'at':now,'equity':str(equity)})
        return {'day':date,'baseline':str(baseline),'equity':str(equity),'halted':halted}

    def _validate_admission(self,mint,evidence,verdict,now):
        """Optional strategy-specific evidence guard, inside the entry transaction."""

    def enter(self,mint,evidence,verdict,now,gate):
        q=evidence['quote']
        if verdict.get('demo') is True and not self.demo:raise ValueError('Demo evidence cannot enter a production account')
        with self.transaction():
            row=self.db.execute('SELECT * FROM candidates WHERE mint=?',(mint,)).fetchone()
            if not row or row['state']!='PENDING':return False
            self.valid_quote(q,now,mint,row['pool'])
            if now-row['first_seen']>self.cfg['candidate_expiry_seconds']:raise ValueError('Candidate expired')
            if not all_pass(hard_checks(self.cfg,q,evidence['facts'],evidence['lp'],now)):raise ValueError('Final hard checks did not pass')
            if (not gate() or verdict.get('validated') is not True or verdict.get('account_id')!=self.identity
                or verdict.get('config_hash')!=digest(self.cfg) or verdict.get('evidence_hash')!=digest(evidence)
                or type(verdict.get('judged_at')) is not int or not 0<=now-verdict['judged_at']<=60
                or number(verdict.get('risk'))>number(self.cfg['grok_max_risk']) or verdict.get('missing_data')!=[]):
                raise ValueError('Current bound model approval unavailable')
            if self.daily(now)['halted']:raise ValueError('Daily loss halt')
            cash=number(self.get('cash'));equity,_=self.equity(now)
            cap=min(cash,equity*number(self.cfg['max_position_pct']))
            initial=simulate(self.costs,'buy',q,cap,now)
            amount=cap-number(initial['explicit_fee_usd'])-number(initial['rent_capital_reserved_usd'])
            if amount<=0:raise ValueError('Insufficient paper cash after costs')
            fill=simulate(self.costs,'buy',q,amount,now)
            basis=amount+number(fill['explicit_fee_usd']);rent=number(fill['rent_capital_reserved_usd'])
            if basis+rent>cash or basis+rent>cap:raise ValueError('Final cash/size limit')
            self._validate_admission(mint,evidence,verdict,now)
            p={'mint':mint,'pool':q['pool'],'symbol':q['symbol'],'opened_at':now,'quantity':fill['quantity'],
                'basis':str(basis),'entry_price':fill['fill_price'],'rent_reserved':str(rent),
                'entry_costs':fill,'entry_evidence_hash':digest(evidence),'mark':None}
            mark=simulate(self.costs,'sell',q,number(p['quantity']),now)
            p['mark']={'at':now,'net_value':str(max(D(0),number(mark['quantity'])*number(mark['fill_price'])-number(mark['explicit_fee_usd']))),'quote':q}
            self.db.execute('INSERT INTO positions VALUES(?,?)',(mint,encode(p)))
            self.set('cash',str(cash-basis-rent));self.set('rent_reserved',str(number(self.get('rent_reserved'))+rent))
            self.db.execute('INSERT INTO fills(at,mint,side,payload) VALUES(?,?,?,?)',(now,mint,'BUY',encode(dict(fill,pool=q['pool'],basis=str(basis),symbol=q['symbol']))))
            self.decision(mint,'BUY',{'reason':'All hard checks and bound Grok risk passed','checks':hard_checks(self.cfg,q,evidence['facts'],evidence['lp'],now),'evidence':evidence,'grok':verdict},now,terminal=True)
            self.daily(now)
        return True

    def watch(self,q,now):
        with self.transaction():
            row=self.db.execute('SELECT payload FROM positions WHERE mint=?',(q['mint'],)).fetchone()
            if not row:return False
            p=json.loads(row[0]);self.valid_quote(q,now,p['mint'],p['pool'])
            fill=simulate(self.costs,'sell',q,number(p['quantity']),now)
            net=number(fill['quantity'])*number(fill['fill_price'])-number(fill['explicit_fee_usd'])
            if net<=0:raise ValueError('Nonpositive modeled proceeds; holding remains unresolved')
            p['mark']={'at':now,'net_value':str(net),'quote':q}
            self.db.execute('INSERT INTO marks(at,mint,payload) VALUES(?,?,?)',(now,p['mint'],encode(q)))
            self.db.execute('UPDATE positions SET payload=? WHERE mint=?',(encode(p),p['mint']))
            price=number(fill['fill_price']);entry=number(p['entry_price'])
            reason='stop_loss' if price<=entry*(1-number(self.cfg['stop_loss_pct'])) else 'take_profit' if price>=entry*(1+number(self.cfg['take_profit_pct'])) else None
            if reason:
                result=dict(fill,pool=p['pool'],symbol=p['symbol'],basis=p['basis'],realized_pnl=str(net-number(p['basis'])),reason=reason,opened_at=p['opened_at'])
                self.set('cash',str(number(self.get('cash'))+net))
                self.db.execute('INSERT INTO fills(at,mint,side,payload) VALUES(?,?,?,?)',(now,p['mint'],'SELL',encode(result)))
                self.db.execute('DELETE FROM positions WHERE mint=?',(p['mint'],))
                self.decision(p['mint'],'SELL',{'reason':reason,'fill':result},now)
            self.daily(now)
        return bool(reason)

    def run_start(self,kind,now):
        prior=self.db.execute('SELECT started_at,finished_at FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1',(kind,)).fetchone()
        gap={'previous_unfinished':bool(prior and prior['finished_at'] is None),
            'seconds_since_previous_start':now-prior['started_at'] if prior else None}
        return self.db.execute("INSERT INTO runs(kind,started_at,state,payload) VALUES(?,?,'RUNNING',?)",(kind,now,encode(gap))).lastrowid

    def run_finish(self,rid,now,errors):
        row=self.db.execute('SELECT payload FROM runs WHERE id=?',(rid,)).fetchone();payload=json.loads(row[0]);payload['errors']=errors
        self.db.execute('UPDATE runs SET finished_at=?,state=?,payload=? WHERE id=?',(now,'ERROR' if errors else 'OK',encode(payload),rid))

    def snapshot(self,now):
        from .view import snapshot
        return snapshot(self.db,now)

    def publish(self):
        with process_lock(self.folder/'publish.lock'):
            self.db.execute('BEGIN')
            try:data=self.snapshot(int(self.clock()))
            finally:self.db.rollback()
            atomic(self.folder/'account.json',encode({k:data[k] for k in ('account_id','config_hash','created_at','demo')}))
            atomic(self.folder/'paper_state.json',encode(data))
            atomic(self.folder/'status.json',encode(data['status']))
            # At-least-once export, stable decision IDs allow crash deduplication.
            cursor=self.get('log_cursor') or 0
            rows=self.db.execute('SELECT * FROM decisions WHERE id>? ORDER BY id LIMIT 1000',(cursor,)).fetchall()
            if rows:
                log=self.folder/'logs/decisions.jsonl';log.parent.mkdir(exist_ok=True)
                # Repair only an interrupted derived export line. SQLite retains
                # the complete authoritative decision and stable sequence ID.
                if log.exists() and log.stat().st_size:
                    with log.open('r+b') as f:
                        size=f.seek(0,2);start=max(0,size-1024*1024);f.seek(start);tail=f.read()
                        if not tail.endswith(b'\n'):
                            end=tail.rfind(b'\n')
                            if end<0 and start:raise ValueError('Derived decision log tail exceeds bound')
                            f.truncate(start+end+1 if end>=0 else 0)
                with log.open('a',encoding='utf-8') as f:
                    for row in rows:f.write(encode(dict(row,payload=json.loads(row['payload'])))+'\n')
                    f.flush();os.fsync(f.fileno())
                self.set('log_cursor',rows[-1]['id'])
