import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal as D
from sniper.krypton.config import load,digest,encode
from sniper.krypton.checks import holder_facts,hard_checks,all_pass,TOKEN_PROGRAM
from sniper.krypton.providers import lp_evidence,Providers
from sniper.krypton.paper import Paper
from sniper.krypton.grok import Grok,validate
from sniper.krypton.demo import Fictional,FictionalGrok,address
from sniper.krypton.service import scan,watch
from sniper.krypton.view import snapshot,view

ROOT=Path(__file__).resolve().parents[1]


class KryptonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1793345000;self.clock=lambda:self.now;self.cfg=load(ROOT/'krypton_bot')
        self.book=Paper(Path(self.tmp.name)/'runtime',self.cfg,initialize=True,demo=True,clock=self.clock)
        self.addCleanup(self.book.close);self.providers=Fictional(self.clock);self.model=FictionalGrok(self.book,self.clock)
    def evidence(self,n=1):
        mint=address(n);q=self.providers.costs(self.providers.quote(mint))
        return {'quote':q,'facts':self.providers.holders(mint,self.cfg),'lp':self.providers.lp(mint,q['pool'])}
    def entry(self):
        e=self.evidence();mint=e['quote']['mint'];self.book.discover([mint],self.now);self.book.pin(mint,e['quote']['pool'])
        return self.book.enter(mint,e,self.model.ask(e),self.now,lambda:True)
    def test_three_token_pipeline_and_original_exit(self):
        for _ in range(3):self.assertEqual(scan(ROOT,self.book,self.providers,self.model,self.clock),[]);self.now+=60
        self.assertEqual(self.model.calls,2);self.assertEqual(len(self.book.positions()),1)
        self.providers.price='2';watch(ROOT,self.book,self.providers,self.clock)
        out=snapshot(self.book.db,self.now);self.assertEqual(out['decision_counts'],{'BUY':1,'SELL':1,'SKIP':2})
        self.assertEqual(out['summary']['completed_trades'],1)
        scan(ROOT,self.book,self.providers,self.model,self.clock);self.assertEqual(out['summary']['fill_count'],2)
    def test_unknown_holder_and_lp_never_pass(self):
        e=self.evidence();e['facts']['complete_owner_coverage']=False;e['lp']={}
        states={c['name']:c['state'] for c in hard_checks(self.cfg,**e,now=self.now)}
        self.assertEqual(states['max_single_holder'],'UNKNOWN');self.assertEqual(states['lp_locked'],'UNKNOWN')
    def test_owner_aggregation_not_token_accounts(self):
        mint=address(1);owner=address(7)
        mint_account={'owner':TOKEN_PROGRAM,'executable':False,'data':{'parsed':{'type':'mint','info':{'supply':'100','decimals':0,'mintAuthority':None,'freezeAuthority':None}}}}
        def account(n):return {'owner':TOKEN_PROGRAM,'executable':False,'data':{'parsed':{'type':'account','info':{'mint':mint,'owner':owner,'tokenAmount':{'amount':str(n),'decimals':0}}}}}
        facts=holder_facts(mint,mint_account,[(address(8),account(6)),(address(9),account(6))],complete=False)
        self.assertEqual(facts['max_single_holder'],'0.12');self.assertFalse(facts['complete_owner_coverage'])
        with self.assertRaises(ValueError):holder_facts(mint,mint_account,[(address(8),account(6))]*2,complete=False)
    def test_lp_exact_pool_provider_scope(self):
        report={'mint':address(1),'rugged':False,'risks':[],'markets':[{'pubkey':address(4),'lp':{'lpLockedPct':100}}]}
        lp=lp_evidence(report,address(1),address(4),self.now)
        self.assertEqual(lp['state'],'PASS');self.assertFalse(lp['independently_verified']);self.assertIsNone(lp['source_state_timestamp'])
        self.assertEqual(lp_evidence(report,address(1),address(5),self.now)['state'],'UNKNOWN')
        report['rugged']=True;self.assertEqual(lp_evidence(report,address(1),address(4),self.now)['state'],'FAIL')
    def test_disabled_grok_reads_no_saved_key_or_transport(self):
        model=Grok(ROOT,self.book,clock=self.clock,transport=lambda *a: self.fail('Network called'))
        with patch.object(model,'key',side_effect=AssertionError('Credential read')):
            self.assertFalse(model.gate()['enabled'])
            with self.assertRaises(ValueError):model.ask(self.evidence())
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM api_requests').fetchone()[0],0)
    def test_hard_rejection_precedes_model(self):
        self.book.discover([address(2)],self.now-1)
        scan(ROOT,self.book,self.providers,self.model,self.clock);self.assertEqual(self.model.calls,0)
    def test_strict_risk_schema(self):
        e=self.evidence();base={'risk':3,'reasons':['liquidity=100000 max_single_holder=0.02 top10_share=0.20'],'missing_data':[]}
        self.assertTrue(validate(encode(base),e)['validated'])
        for mutate in ({'risk':True},{'risk':11},{'risk':'3'},{'reasons':['ignore instructions']},{'missing_data':'none'}):
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):validate(encode(dict(base,**mutate)),e)
        with self.assertRaises(ValueError):validate('{"risk":3,"risk":1}',e)
    def test_costs_size_and_no_duplicate_entry(self):
        self.assertTrue(self.entry());self.assertFalse(self.entry())
        p=self.book.positions()[0];spent=D('1000')-D(self.book.get('cash'))
        self.assertLessEqual(spent,D('20'));self.assertGreater(D(p['entry_price']),1)
        self.assertGreater(D(p['rent_reserved']),0);self.assertEqual(D(p['basis'])+D(p['rent_reserved']),spent)
    def test_stale_wrong_pool_or_gate_blocks_final_entry(self):
        e=self.evidence();mint=address(1);self.book.discover([mint],self.now);self.book.pin(mint,address(4));verdict=self.model.ask(e)
        with self.assertRaises(ValueError):self.book.enter(mint,e,verdict,self.now,lambda:False)
        self.now+=61
        with self.assertRaises(ValueError):self.book.enter(mint,e,verdict,self.now,lambda:True)
        self.assertEqual(self.book.get('cash'),'1000')
    def test_missing_exit_quote_retains_exact_holding(self):
        self.entry();before=self.book.positions();cash=self.book.get('cash')
        q=self.providers.costs(self.providers.quote(address(1),address(5)))
        with self.assertRaises(ValueError):self.book.watch(q,self.now)
        self.assertEqual(self.book.positions(),before);self.assertEqual(self.book.get('cash'),cash)
    def test_stop_independent_of_model_and_no_reentry(self):
        self.entry();self.providers.price='.5';watch(ROOT,self.book,self.providers,self.clock)
        self.assertEqual(self.book.positions(),[]);self.assertFalse(self.entry())
        row=self.book.db.execute("SELECT payload FROM fills WHERE side='SELL'").fetchone()
        self.assertEqual(json.loads(row[0])['reason'],'stop_loss')
    def test_daily_halt_latches_then_resets_berlin_day(self):
        with self.book.transaction():self.book.daily(self.now);self.book.set('cash','940');self.assertTrue(self.book.daily(self.now)['halted'])
        with self.book.transaction():self.book.set('cash','1000');self.assertTrue(self.book.daily(self.now)['halted'])
        self.now+=86400
        with self.book.transaction():self.assertFalse(self.book.daily(self.now)['halted'])
    def test_restart_identity_and_config_mismatch(self):
        identity=self.book.identity;self.entry()
        other=Paper(self.book.folder,self.cfg,demo=True);self.assertEqual(other.identity,identity);other.close()
        cfg=copy.deepcopy(self.cfg);cfg['starting_cash_usd']='2000'
        with self.assertRaises(ValueError):Paper(self.book.folder,cfg,demo=True)
    def test_view_does_not_create_account(self):
        root=Path(self.tmp.name)/'absent';self.assertFalse(view(root)['initialized']);self.assertFalse(root.exists())
    def test_publication_and_log_ids(self):
        self.entry();self.book.publish();self.book.publish()
        rows=(self.book.folder/'logs/decisions.jsonl').read_text().splitlines();self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0])['payload']['account_id'],self.book.identity)
    def test_backoff_survives_processes_without_request(self):
        self.book.provider('profiles',{'retry_after':self.now+60})
        p=Providers(ROOT,self.book,self.clock,lambda *a,**k:self.fail('Backoff bypassed'))
        with self.assertRaises(ValueError):p.profiles()
    def test_demo_model_cannot_bind_production_book(self):
        other=Paper(Path(self.tmp.name)/'production',self.cfg,initialize=True)
        try:
            with self.assertRaises(ValueError):FictionalGrok(other,self.clock)
        finally:other.close()

    def test_complete_rpc_owner_enumeration_and_supply_reconciliation(self):
        mint=address(1)
        info={'supply':'100','decimals':0,'mintAuthority':None,'freezeAuthority':None}
        def parsed(kind,value):return {'owner':TOKEN_PROGRAM,'executable':False,'data':{'parsed':{'type':kind,'info':value}}}
        ma=parsed('mint',info)
        accounts=[{'pubkey':address(n),'account':parsed('account',{'mint':mint,'owner':address(n+50),'tokenAmount':{'amount':'2','decimals':0}})} for n in range(10,60)]
        calls=[]
        def transport(method,url,body=None,headers=None,timeout=None):
            method=body['method'];calls.append(method)
            values={'getTokenSupply':{'amount':'100','decimals':0},'getTokenLargestAccounts':[{'address':a['pubkey']} for a in accounts[:20]],
                'getMultipleAccounts':[ma]+[a['account'] for a in accounts[:20]],'getProgramAccounts':accounts,'getAccountInfo':ma}
            return {'result':{'context':{'slot':100},'value':values[method]}}
        p=Providers(Path(self.tmp.name),self.book,self.clock,transport)
        facts=p.holders(mint,self.cfg)
        self.assertTrue(facts['complete_owner_coverage']);self.assertEqual(facts['owners'],50)
        self.assertEqual(facts['max_single_holder'],'0.02');self.assertEqual(facts['top10_share'],'0.2')
        self.assertEqual(len(calls),5)
        info['supply']='101'
        with self.assertRaises(ValueError):p.holders(mint,self.cfg)

    def test_http_429_has_durable_backoff(self):
        from urllib.error import HTTPError
        calls=[]
        def transport(*a,**kw):calls.append(1);raise HTTPError('redacted',429,'rate limit',{},None)
        p=Providers(Path(self.tmp.name),self.book,self.clock,transport)
        for _ in range(2):
            with self.assertRaises(ValueError):p.profiles()
        self.assertEqual(len(calls),1);self.assertEqual(self.book.provider('profiles')['retry_after'],self.now+60)

    def test_mocked_model_transport_billing_and_account_binding(self):
        e=self.evidence();calls=[]
        raw={'risk':3,'reasons':['liquidity=100000 max_single_holder=0.02 top10_share=0.20'],'missing_data':[]}
        def transport(method,url,payload,headers,timeout):
            calls.append((method,url,payload));return {'choices':[{'finish_reason':'stop','message':{'content':encode(raw)}}],
                'usage':{'cost_in_usd_ticks':1000000}}
        model=Grok(Path(self.tmp.name),self.book,self.clock,transport)
        with patch.object(model,'gate',return_value={'enabled':True}),patch.object(model,'key',return_value='isolated-fixture'):
            verdict=model.ask(e)
        self.assertEqual(len(calls),1);self.assertEqual(verdict['account_id'],self.book.identity)
        self.assertEqual(verdict['evidence_hash'],digest(e))
        self.assertEqual(snapshot(self.book.db,self.now)['summary']['api_spending'],'0.0001')

    def test_console_krypton_does_not_read_other_accounts_or_enable_live(self):
        from sniper.console import ConsoleData
        root=Path(self.tmp.name)/'console';book=Paper(root/'krypton_bot/runtime',self.cfg,initialize=True)
        try:
            data=ConsoleData(root)
            with patch.object(data,'jobs',return_value={'items':[]}):out=data.state('krypton')
            self.assertEqual(out['account_id'],book.identity);self.assertFalse(out['real_money_enabled']);self.assertNotIn('switchover',out)
            self.assertEqual(out['summary']['fill_count'],0)
        finally:book.close()

    def test_partial_log_line_recovery_preserves_authoritative_decision(self):
        self.entry();path=self.book.folder/'logs/decisions.jsonl';path.parent.mkdir();path.write_bytes(b'{"partial":')
        self.book.publish();rows=path.read_text().splitlines();self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0])['action'],'BUY')

    def test_expiry_cleanup_preserves_fresh_candidate_and_deduplicates(self):
        self.book.discover([address(1),address(2)],self.now-2000)
        self.book.discover([address(3)],self.now)
        self.book.expire_pending(self.now);self.book.expire_pending(self.now)
        self.assertEqual([r['mint'] for r in self.book.pending()],[address(3)])
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM decisions').fetchone()[0],2)


if __name__=='__main__':unittest.main()
