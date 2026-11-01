"""Isolated, deterministic experimental paper portfolios. No live execution path."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import time
import uuid
from .krypton.config import digest, encode, number
from .execution_costs import DEFAULT_POLICY, simulate

VERSION='observable_proxies_v1'
CATALOG={
 'xenon':('Flow proxy','Provider hourly buys >=20, buys/sells >=1.05 and nonnegative hourly momentum'),
 'argon':('Depth proxy','Liquidity >=100k USD, hourly activity >=10 and volume/liquidity >=0.02'),
 'neon':('Activity proxy','Hourly activity >=40, buys >= sells and hourly volume >=10k USD; no skilled-leader claim'),
 'krypton':('Distribution proxy','Observed non-vault owner lower bounds <=10% single /40% top ten; unobserved holders remain unknown'),
 'helium':('Confluence proxy','Flow and activity proxies agree with liquidity >=50k USD; no model approval claim'),
 'nitrogen':('Large-flow proxy','Hourly average trade notional >=100 USD across >=10 trades; not identified whale buying'),
 'radon':('Turnover proxy','Hourly volume/liquidity between 0.1 and 5 across >=20 trades; no copied-trader return claim'),
 'oxygen':('Pulse proxy','Five-minute buys >=5, buys/sells >=1.1 and nonnegative five-minute momentum; not X sentiment'),
}
UNKNOWN=['buyer controller independence','complete holder coverage','historical trader skill and inventory',
         'LP lock and program audit','Grok/JEV approval','X sentiment','future execution and liquidity']

def policy(bot):
 if bot not in CATALOG:raise ValueError('Unknown experimental variant')
 return dict(version=VERSION,bot=bot,mode='experimental_paper',real_orders_enabled=False,
   starting_cash='500' if bot=='nitrogen' else '1000',ticket_pct='0.01',max_open=3,
   max_daily_loss_pct='0.05',min_liquidity='25000',min_age_seconds=600,max_quote_age=60,
   stop_pct='0.20',take_pct='0.30',max_hold_seconds=21600,
   max_observed_owner='0.20',max_observed_top10='0.60',rule=CATALOG[bot][1],costs=deepcopy(DEFAULT_POLICY))

def folder(root,bot):
 if bot not in CATALOG:raise ValueError('Unknown experimental variant')
 return Path(root)/'runtime/experiments'/bot

def validate(e,now):
 q=e['quote'];chain=e['chain'];a=e['activity']
 from .adapters.solana import validate_address
 validate_address(q['mint']);validate_address(q['pool'])
 for k in ('observed_at','sol_observed_at'):
  if type(q.get(k))is not int or not 0<=now-q[k]<=60:raise ValueError('Stale or future quote')
 if not 0<=now-q['sol_observed_at']<=30:raise ValueError('Stale native cost mark')
 for k in ('price','liquidity','sol_usd'):number(q[k],True)
 if (chain.get('genesis_verified')is not True or chain.get('commitment')!='finalized'
     or chain.get('mint')!=q['mint'] or chain.get('pool')!=q['pool'] or type(chain.get('slot'))is not int
     or chain['slot']<=0 or type(chain.get('block_time'))is not int or not 0<=now-chain['block_time']<=120
     or chain.get('mint_disabled')is not True or chain.get('freeze_disabled')is not True
     or chain.get('pool_structure_verified')is not True):raise ValueError('Finalized pool/mint evidence unavailable')
 if e.get('source_mode') not in ('live','fixture'):raise ValueError('Evidence provenance missing')
 for k in ('buys_h1','sells_h1','buys_m5','sells_m5'):
  if type(a.get(k))is not int or a[k]<0:raise ValueError('Missing activity counts')
 for k in ('volume_h1','observed_owner_share','observed_top10_share'):number(a[k])
 for k in ('change_h1','change_m5'):
  if not D(str(a[k])).is_finite():raise ValueError('Invalid momentum')
 if not 0<=number(a['observed_owner_share'])<=number(a['observed_top10_share'])<=1:raise ValueError('Invalid observed holder shares')
 if type(q.get('created_at_ms'))is not int or q['created_at_ms']<=0:raise ValueError('Pool age unavailable')
 if number(e['fee_bps'])>=10000:raise ValueError('Invalid pool fees')
 return q,a

def evaluate(bot,e,now):
 cfg=policy(bot);q,a=validate(e,now)
 checks={
  'liquidity':number(q['liquidity'])>=number(cfg['min_liquidity']),
  'age':now-q['created_at_ms']/1000>=cfg['min_age_seconds'],
  'observed_concentration':number(a['observed_owner_share'])<=number(cfg['max_observed_owner']) and number(a['observed_top10_share'])<=number(cfg['max_observed_top10']),
 }
 buys,sells=a['buys_h1'],a['sells_h1'];count=buys+sells;volume=number(a['volume_h1']);liq=number(q['liquidity'],True)
 flow=buys>=20 and D(buys)/max(1,sells)>=D('1.05') and D(str(a['change_h1']))>=0
 activity=count>=40 and buys>=sells and volume>=10000
 rules={
 'xenon':flow,'argon':liq>=100000 and count>=10 and volume/liq>=D('.02'),
 'neon':activity,'krypton':number(a['observed_owner_share'])<=D('.10') and number(a['observed_top10_share'])<=D('.40'),
 'helium':flow and activity and liq>=50000,
 'nitrogen':count>=10 and volume/max(1,count)>=100,
 'radon':count>=20 and D('.1')<=volume/liq<=5,
 'oxygen':a['buys_m5']>=5 and D(a['buys_m5'])/max(1,a['sells_m5'])>=D('1.1') and D(str(a['change_m5']))>=0}
 checks['variant_rule']=rules[bot]
 return checks

class Account:
 def __init__(self,root,bot,*,initialize=False,reference=None,now=None,fixture=False):
  self.bot=bot;self.cfg=policy(bot);self.path=folder(root,bot)/'book.sqlite3';now=int(time.time()) if now is None else now
  if not self.path.exists() and not initialize:raise ValueError('Explicit experimental enrollment required')
  if initialize:
   if not reference:raise ValueError('Authorization reference required')
   self.path.parent.mkdir(parents=True,exist_ok=True)
  self.db=sqlite3.connect(self.path,timeout=3,isolation_level=None);self.db.row_factory=sqlite3.Row
  self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
  if initialize:
   self.db.executescript("""CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS evidence(hash TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,mint TEXT NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,evidence_hash TEXT);
    CREATE TABLE IF NOT EXISTS positions(mint TEXT PRIMARY KEY,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,mint TEXT NOT NULL,side TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS traded(mint TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY,baseline TEXT NOT NULL,halted INTEGER NOT NULL);""")
   with self.tx():
    if self.get('account_id') is None:
     for k,v in dict(account_id=str(uuid.uuid4()),policy=self.cfg,policy_hash=digest(self.cfg),cash=self.cfg['starting_cash'],rent_reserved='0',
       started_at=now,ends_at=now+604800,reference=reference,mode='experimental_paper',real_orders_enabled=False,fixture=fixture).items():self.set(k,v)
  if (self.get('policy_hash')!=digest(self.cfg) or self.get('mode')!='experimental_paper' or self.get('real_orders_enabled')is not False or self.get('fixture')is not fixture):
   self.db.close();raise ValueError('Experimental account identity/policy mismatch')
  self.identity=self.get('account_id')
 @contextmanager
 def tx(self):
  self.db.execute('BEGIN IMMEDIATE')
  try:yield;self.db.commit()
  except BaseException:self.db.rollback();raise
 def get(self,k):
  r=self.db.execute('SELECT value FROM meta WHERE key=?',(k,)).fetchone();return json.loads(r[0]) if r else None
 def set(self,k,v):self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,encode(v)))
 def close(self):self.db.close()
 def positions(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM positions ORDER BY mint')]
 def equity(self):return number(self.get('cash'))+sum((number(p['mark_value']) for p in self.positions()),D(0))
 def loss_halt(self,now):
  day=time.strftime('%Y-%m-%d',time.gmtime(now));equity=self.equity()
  self.db.execute('INSERT OR IGNORE INTO days VALUES(?,?,0)',(day,str(equity)))
  r=self.db.execute('SELECT * FROM days WHERE day=?',(day,)).fetchone()
  halt=bool(r['halted']) or equity<=number(r['baseline'])*(1-number(self.cfg['max_daily_loss_pct']))
  if halt:self.db.execute('UPDATE days SET halted=1 WHERE day=?',(day,))
  return halt
 def costs(self,e):
  c=deepcopy(self.cfg['costs']);c['dex_fee_bps']=max(c['dex_fee_bps'],int(D(str(e['fee_bps'])).to_integral_value(rounding='ROUND_CEILING')));return c
 def observe(self,e,now):
  q,a=validate(e,now)
  if (e['source_mode']=='fixture')!=self.get('fixture'):raise ValueError('Fixture/live evidence separation')
  h=digest(e);mint=q['mint']
  with self.tx():
   self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?)',(h,now,encode(e)))
   if self.db.execute('SELECT 1 FROM traded WHERE mint=?',(mint,)).fetchone():return False
   if not self.get('started_at')<=now<self.get('ends_at'):return False
   checks=evaluate(self.bot,e,now)
   reason=','.join(k for k,v in checks.items() if not v)
   positions=self.positions()
   if len(positions)>=self.cfg['max_open']:reason=reason or 'position_limit'
   if any(not 0<=now-p['marked_at']<=120 for p in positions):reason=reason or 'stale_existing_position'
   if self.loss_halt(now):reason=reason or 'daily_loss_halt'
   if reason:
    self.db.execute('INSERT INTO decisions(at,mint,action,reason,evidence_hash) VALUES(?,?,?,?,?)',(now,mint,'SKIP',reason,h));return False
   cash=number(self.get('cash'));cap=min(cash,self.equity()*number(self.cfg['ticket_pct']));costs=self.costs(e)
   initial=simulate(costs,'buy',q,cap,now);amount=cap-number(initial['explicit_fee_usd'])-number(initial['rent_capital_reserved_usd'])
   if amount<=0:raise ValueError('Insufficient paper cash')
   fill=simulate(costs,'buy',q,amount,now);basis=amount+number(fill['explicit_fee_usd']);rent=number(fill['rent_capital_reserved_usd'])
   mark=simulate(costs,'sell',q,number(fill['quantity']),now);net=max(D(0),number(mark['quantity'])*number(mark['fill_price'])-number(mark['explicit_fee_usd']))
   p=dict(mint=mint,pool=q['pool'],symbol=q['symbol'],opened_at=now,quantity=fill['quantity'],basis=str(basis),entry_price=fill['fill_price'],
     rent_reserved=str(rent),marked_at=now,mark_value=str(net),evidence_hash=h)
   self.db.execute('INSERT INTO positions VALUES(?,?)',(mint,encode(p)));self.db.execute('INSERT INTO traded VALUES(?)',(mint,))
   self.set('cash',str(cash-basis-rent));self.set('rent_reserved',str(number(self.get('rent_reserved'))+rent))
   self.fill(now,mint,'BUY',dict(fill,pool=q['pool'],symbol=q['symbol'],basis=str(basis),reason='experimental_proxy_passed',evidence_hash=h))
   self.db.execute('INSERT INTO decisions(at,mint,action,reason,evidence_hash) VALUES(?,?,?,?,?)',(now,mint,'PAPER_BUY','experimental_proxy_passed',h))
   return True
 def fill(self,now,mint,side,payload):
  self.db.execute('INSERT INTO fills(at,mint,side,payload) VALUES(?,?,?,?)',(now,mint,side,encode(dict(payload,account_id=self.identity,mode='experimental_paper',simulated=True,source_mode='fixture' if self.get('fixture') else 'live',real_orders_enabled=False))))
 def watch(self,e,now):
  q,_=validate(e,now)
  if (e['source_mode']=='fixture')!=self.get('fixture'):raise ValueError('Fixture/live evidence separation')
  with self.tx():
   row=self.db.execute('SELECT payload FROM positions WHERE mint=?',(q['mint'],)).fetchone()
   if not row:return False
   p=json.loads(row[0])
   if p['pool']!=q['pool']:raise ValueError('Position pool mismatch')
   h=digest(e);self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?)',(h,now,encode(e)))
   fill=simulate(self.costs(e),'sell',q,number(p['quantity']),now);net=max(D(0),number(fill['quantity'])*number(fill['fill_price'])-number(fill['explicit_fee_usd']))
   p.update(marked_at=now,mark_value=str(net));price=number(fill['fill_price']);entry=number(p['entry_price'])
   reason=('stop_loss' if price<=entry*(1-number(self.cfg['stop_pct'])) else 'take_profit' if price>=entry*(1+number(self.cfg['take_pct']))
     else 'time_stop' if now-p['opened_at']>=self.cfg['max_hold_seconds'] else 'trial_end' if now>=self.get('ends_at') else None)
   if reason:
    self.set('cash',str(number(self.get('cash'))+net));self.db.execute('DELETE FROM positions WHERE mint=?',(q['mint'],))
    self.fill(now,q['mint'],'SELL',dict(fill,pool=q['pool'],symbol=q['symbol'],basis=p['basis'],realized_pnl=str(net-number(p['basis'])),reason=reason,evidence_hash=h))
   else:self.db.execute('UPDATE positions SET payload=? WHERE mint=?',(encode(p),q['mint']))
   self.loss_halt(now)
   return bool(reason)

def snapshot(root,now=None):
 now=int(time.time()) if now is None else now;result=[]
 for bot in CATALOG:
  path=folder(root,bot)/'book.sqlite3'
  if not path.exists():continue
  db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
  try:
   db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
   m={r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM meta')};positions=[json.loads(r[0]) for r in db.execute('SELECT payload FROM positions')]
   fills=[dict(json.loads(r['payload']),id=r['id'],at=r['at'],mint=r['mint'],side=r['side']) for r in db.execute('SELECT * FROM fills ORDER BY id DESC LIMIT 50')]
   decisions=[dict(r) for r in db.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 10')]
   equity=number(m['cash'])+sum((number(p['mark_value']) for p in positions),D(0))
   result.append(dict(bot=bot,title=bot.title()+' Experimental',proxy=CATALOG[bot][0],rule=m['policy']['rule'],account_id=m['account_id'],policy_hash=m['policy_hash'],
    cash=m['cash'],equity=str(equity),net_pnl=str(equity-number(m['policy']['starting_cash'])),rent_reserved=m['rent_reserved'],
    started_at=m['started_at'],ends_at=m['ends_at'],window_open=m['started_at']<=now<m['ends_at'],positions=positions,fills=fills,
    fill_count=db.execute('SELECT count(*) FROM fills').fetchone()[0],decisions=decisions,fixture=m['fixture'],
    stale_positions=sum(not 0<=now-p['marked_at']<=120 for p in positions)))
  finally:db.close()
 return dict(mode='EXPERIMENTAL PAPER',real_orders_enabled=False,generated_at=now,accounts=result,unknowns=UNKNOWN,
  disclaimer='Separate experimental results. Aggregate activity and sampled holdings are proxies; fills and costs are simulated. Original accounts and live gates are unchanged.')
