"""Bounded public reads, supported direct purchases, and separate factual supplements."""
import json
import time
from sniper.krypton.providers import Providers as Base
from sniper.neon_sources import decode_swap
from .config import number, digest


class Providers(Base):
    settings_folder='nitrogen_bot'
    agent_name='Nitrogen'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.deadline=time.monotonic()+75

    def request(self,*args,**kwargs):
        if time.monotonic()>self.deadline:raise ValueError('Bounded provider worker deadline reached')
        return super().request(*args,**kwargs)

    def search(self):
        now=int(self.clock());out=[];remaining=4
        if not self.book.cfg['tracked_wallets']:raise ValueError('No NITROGEN tracked wallets configured')
        wallets=self.book.cfg['tracked_wallets']
        cursor=self.book.provider('search-rotation') or {}
        offset=cursor.get('offset',0)%len(wallets)
        wallets=wallets[offset:]+wallets[:offset]
        self.book.provider('search-rotation',{'offset':(offset+1)%len(wallets),'finished_at':now,'state':'OK'})
        for wallet in wallets:
            wallet_remaining=max(1,4//len(wallets))
            rows=self.rpc('getSignaturesForAddress',[wallet,{'limit':20,'commitment':'finalized'}])
            if not isinstance(rows,list):raise ValueError('Invalid signature list')
            for r in rows[:20]:
                sig=r.get('signature')
                if r.get('err') is not None or r.get('confirmationStatus')!='finalized' or type(r.get('blockTime')) is not int or not 0<=now-r['blockTime']<=self.book.cfg['whale_max_age_seconds']:continue
                prior=self.book.db.execute('SELECT payload FROM purchases WHERE signature=?',(sig,)).fetchone()
                if prior:out.append(json.loads(prior[0]));continue
                if remaining<=0 or wallet_remaining<=0:continue
                if self.book.provider('unsupported:'+str(sig)[:90]):continue
                remaining-=1;wallet_remaining-=1
                # Read v1 receipts to record them as unsupported and advance.
                # The shared decoder still admits only legacy/v0 swaps.
                receipt=self.rpc('getTransaction',[sig,{'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':1}])
                try:p=decode_swap(receipt,sig,wallet,None,now)
                except (ValueError,KeyError,TypeError,IndexError):
                    self.book.provider('unsupported:'+str(sig)[:90],{'state':'UNSUPPORTED','finished_at':now,'scope':'Only direct classic-SPL Raydium CP exact-input swaps decoded'})
                    continue
                if p['action']=='BUY':out.append(p)
        return out

    def quote(self,mint,pool=None):
        q=super().quote(mint,pool)
        return q

    def evidence(self,lead):
        q=self.costs(self.quote(lead['mint'],lead['pool']))
        facts=self.holders(lead['mint'],self.book.cfg);lp=self.lp(lead['mint'],lead['pool'])
        row=self.book.db.execute('SELECT payload FROM observations WHERE mint=? AND pool=? ORDER BY at DESC LIMIT 1',(lead['mint'],lead['pool'])).fetchone()
        purchases=[json.loads(r[0]) for r in self.book.db.execute('SELECT payload FROM purchases WHERE at>=?',(int(self.clock())-self.book.cfg['whale_max_age_seconds'],))]
        q=self.costs(self.quote(lead['mint'],lead['pool']))  # Refresh after slower factual reads.
        return {'quote':q,'facts':facts,'lp':lp,'purchases':purchases,'supplement':json.loads(row[0]) if row else {},'demo':False}

    def mark(self,mint,pool):return self.costs(self.quote(mint,pool))
