import copy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
from sniper.neon import NeonBook,POLICY,initialize,cycle,publish,maybe_start,paper_snapshot,meta,usage
from sniper.neon_sources import Research,decode_swap,leaderboard
from sniper.neon_acceptance import run
from sniper.neon_console import view
from sniper.solana_wire import b58encode
from sniper import solana_cpmm as cp
from sniper.adapters.solana_execution import USDC
from rug_desk.traders import Selections
from rug_desk.leader_signals import process_event
from rug_desk.trader_acceptance import trader,event,key,ReplayJudge,ReplayMarket,ReplayRugcheck
from rug_desk.book import encode
from rug_desk.grok import Judge

NOW=1793200000
SIG='2'*88

def receipt(signature=SIG):
    accounts=[key(77),key(90),key(91),key(2),key(92),key(93),key(94),key(95),cp.TOKEN,cp.TOKEN,USDC,key(1),key(96)]
    keys=list(dict.fromkeys(accounts+[cp.PROGRAM]))
    def bal(account,mint,amount):return {'accountIndex':keys.index(account),'mint':mint,'owner':key(77),'programId':cp.TOKEN,'uiTokenAmount':{'amount':str(amount),'decimals':6}}
    return {'slot':100,'blockTime':NOW,'meta':{'err':None,'fee':5000,'preTokenBalances':[bal(key(92),USDC,1000)],
        'postTokenBalances':[bal(key(92),USDC,900),bal(key(93),key(1),200)]},
        'transaction':{'signatures':[signature],'message':{'header':{'numRequiredSignatures':1},'accountKeys':keys,
            'instructions':[{'programIdIndex':keys.index(cp.PROGRAM),'accounts':[keys.index(k) for k in accounts],
                'data':b58encode(cp.SWAP+struct.pack('<QQ',100,190))}]}}}

class RPC:
    def __init__(self):self.page=[];self.missing=False;self.block=True
    def verify_chain(self):return True
    def rpc(self,method,args):
        if method=='getSignaturesForAddress':return self.page
        if method=='getTransaction':return None if self.missing else receipt(args[0])
        if method=='getBlock':return {'blockhash':key(44),'signatures':[SIG]} if self.block else None
        raise AssertionError(method)


class NeonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Path(self.tmp.name)
        initialize(self.app);self.book=NeonBook(self.app/'neon_bot');self.rpc=RPC()
        self.source=Research(self.book,self.app,rpc=self.rpc,clock=lambda:NOW)
    def tearDown(self):self.book.close();self.tmp.cleanup()
    def fixture_trial(self):
        with self.book.transaction():
            self.book.db.execute("INSERT INTO metadata VALUES('trader_replay_account','1')")
            self.book.db.execute("INSERT INTO metadata VALUES('trial',?)",(encode({'started_at':NOW,'ends_at':NOW+100}),))
        Selections(self.book).select([trader(NOW)],judge=ReplayJudge(),clock=lambda:NOW)
    def send(self,signal=None,**kw):
        return process_event(self.book,signal or event(NOW),clock=lambda:NOW,judge=ReplayJudge(),
            market=ReplayMarket(lambda:NOW),rugcheck=ReplayRugcheck(lambda:NOW),**kw)
    def test_acceptance_pipeline_costs_and_restart(self):
        out=run(self.app/'acceptance',NOW)
        self.assertTrue(out['passed']);self.assertEqual(len(out['fills']),3)
        self.assertEqual(self.book.fills(),[])
    def test_initialize_idempotent_separate_identity_and_trial_blocked(self):
        initial=self.book.identity;initialize(self.app)
        self.assertEqual(initial,self.book.identity);self.assertIsNone(maybe_start(self.book,self.app,NOW))
        self.assertEqual(publish(self.book,self.app,NOW)['trial_state'],'SETUP_BLOCKED')
        other=NeonBook(self.app/'other',initialize=True)
        try:self.assertNotEqual(initial,other.identity)
        finally:other.close()
    def test_persistence_in_another_process(self):
        code="import sys;from sniper.neon import NeonBook;from pathlib import Path;b=NeonBook(Path(sys.argv[1]));print(b.identity['account_id']);b.close()"
        out=subprocess.check_output([sys.executable,'-c',code,str(self.book.root)],text=True,cwd=Path(__file__).parents[1])
        self.assertEqual(out.strip(),self.book.identity['account_id'])
    def test_disabled_model_never_reads_key_or_calls_network(self):
        transport=Mock(side_effect=AssertionError('Paid network forbidden'))
        judge=Judge(self.book.root,transport=transport,budget_root=self.app/'rug_desk',billing_account_id=self.book.identity['account_id'])
        self.assertFalse(judge.recurring_enabled())
        self.assertEqual(judge.ask(key(1),{}, {})['verdict'],'SKIP')
        with self.assertRaises(ValueError):judge.rank_traders([{}])
        transport.assert_not_called();self.assertFalse((self.app/'rug_desk/runtime/ai-budget.sqlite3').exists())
    def test_deadline_blocks_new_entries_but_preserves_exit(self):
        self.fixture_trial();self.assertTrue(self.send()['entry'])
        self.assertIn('ended',self.book.limits(key(3),NOW+100))
        from rug_desk.watch import watch
        market=ReplayMarket(lambda:NOW+101);market.price='.5'
        out=watch(self.book,market=market,clock=lambda:NOW+101)
        self.assertEqual(out[0]['result'],'stop');self.assertFalse(self.book.positions())
    def test_proof_required_and_pool_mismatch_skips(self):
        self.fixture_trial();signal=decode_swap(receipt(),SIG,key(77),'replay_leader',NOW)
        self.assertEqual(self.send(signal)['verdict'],'SKIP')
        signal['transaction_signature']='3'*88;signal['pool']=key(9)
        out=self.send(signal,event_verifier=lambda e,n:True)
        self.assertFalse(out['entry']);self.assertIn('pool',out['reason'].lower());self.assertEqual(self.book.fills(),[])
    def test_admission_disabled_skips_before_judgment(self):
        self.fixture_trial()
        def deny(_):raise ValueError('Grok gate disabled')
        self.assertFalse(self.send(admission_guard=deny)['entry'])
    def test_parser_rejects_gifts_wrong_owner_missing_cost_and_unsupported_program(self):
        parsed=decode_swap(receipt(),SIG,key(77),'replay_leader',NOW)
        self.assertEqual(parsed['action'],'BUY');self.assertIsNone(parsed['fee_usd'])
        variants=[]
        r=receipt();r['meta']['postTokenBalances'][0]['uiTokenAmount']['amount']='1000';variants.append(r)
        r=receipt();r['meta']['postTokenBalances'][1]['owner']=key(78);variants.append(r)
        r=receipt();r['meta']['fee']=None;variants.append(r)
        r=receipt();r['meta']['err']={'fail':1};variants.append(r)
        r=receipt();r['transaction']['message']['header']['numRequiredSignatures']=0;variants.append(r)
        r=receipt();r['transaction']['message']['instructions'][0]['programIdIndex']=1;variants.append(r)
        for r in variants:
            with self.subTest(r=r),self.assertRaises(ValueError):decode_swap(r,SIG,key(77),'replay_leader',NOW)
    def test_checkpoint_retains_failed_receipt_then_records_exactly_once(self):
        old='3'*88;self.rpc.page=[{'signature':old,'slot':99,'confirmationStatus':'finalized'}]
        self.assertEqual(self.source._wallet(key(77),'replay_leader',NOW),[])
        self.rpc.page=[{'signature':SIG,'slot':100,'confirmationStatus':'finalized'},self.rpc.page[0]]
        self.rpc.missing=True
        with self.assertRaises(ValueError):self.source._wallet(key(77),'replay_leader',NOW)
        self.assertTrue(json.loads(self.book.db.execute('SELECT pending FROM neon_cursors').fetchone()[0]))
        self.book.close();self.book=NeonBook(self.app/'neon_bot');self.source=Research(self.book,self.app,rpc=self.rpc,clock=lambda:NOW)
        self.rpc.missing=False;events=self.source._wallet(key(77),'replay_leader',NOW)
        self.assertEqual(len(events),1);self.assertTrue(self.source.verify(events[0],NOW))
        self.assertFalse(self.source.verify(events[0],NOW+301))
        self.assertEqual(self.source._wallet(key(77),'replay_leader',NOW),[])
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM neon_observations').fetchone()[0],1)
    def test_missing_checkpoint_records_gap_without_trading_backlog(self):
        self.rpc.page=[{'signature':'3'*88}];self.source._wallet(key(77),'replay_leader',NOW)
        self.rpc.page=[{'signature':SIG,'slot':100,'confirmationStatus':'finalized'}]
        self.assertEqual(self.source._wallet(key(77),'replay_leader',NOW),[])
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM neon_source_gaps').fetchone()[0],1)
    def test_unverified_canonical_block_retains_pending(self):
        self.rpc.page=[{'signature':'3'*88}];self.source._wallet(key(77),'replay_leader',NOW)
        self.rpc.page=[{'signature':SIG,'slot':100,'confirmationStatus':'finalized'},self.rpc.page[0]];self.rpc.block=False
        with self.assertRaises(RuntimeError):self.source._wallet(key(77),'replay_leader',NOW)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM neon_observations').fetchone()[0],0)
    def test_headline_traders_cannot_qualify(self):
        from datetime import datetime,timezone
        board={'board':'solana','window':'30d','capturedAt':datetime.fromtimestamp(NOW,timezone.utc).isoformat(),'data':[{'address':key(77),'handle':'leader','realizedPnlUsd':99999}]}
        rows=leaderboard(board,NOW);self.assertFalse(rows[0]['qualified'])
        with self.assertRaises(ValueError):leaderboard(board,NOW+7201)
    def test_invalid_history_clears_old_selection_and_worker_error_publishes(self):
        self.fixture_trial();(self.book.runtime/'traders.json').write_text('{broken')
        source=Mock();source.refresh_board.return_value={};source.observe.return_value=([],[])
        cycle(self.app,'signals',clock=lambda:NOW,source=source,judge=ReplayJudge(),send=False)
        self.assertEqual(Selections(self.book).status(NOW)['status'],'FAILED')
        source.observe.side_effect=RuntimeError('provider unavailable')
        with self.assertRaises(RuntimeError):cycle(self.app,'signals',clock=lambda:NOW+1,source=source,judge=ReplayJudge(),send=False)
        state=json.loads((self.book.runtime/'status.json').read_text())
        self.assertTrue(any(r['status']=='error' for r in state['runs']))
    def test_read_only_console_and_cost_aware_open_mark(self):
        self.fixture_trial();self.send()
        from rug_desk.watch import watch
        watch(self.book,market=ReplayMarket(lambda:NOW),clock=lambda:NOW)
        publish(self.book,self.app,NOW)
        before=self.book.db.total_changes
        with patch('sniper.neon_console.time.time',return_value=NOW):out=view(self.app)
        self.assertEqual(self.book.db.total_changes,before)
        self.assertEqual(out['snapshot']['open_positions'],1)
        self.assertLess(D(out['snapshot']['net_pnl']),0)
        self.assertEqual(out['snapshot']['unresolved_positions'],0)
        self.assertEqual(out['fills'][0]['action'],'OPEN')
    def test_missing_marks_value_zero_without_fictional_exit(self):
        self.fixture_trial();self.send()
        out=paper_snapshot(self.book.fills(),self.book.positions(),NOW)
        self.assertEqual(out['unresolved_positions'],1);self.assertEqual(len(self.book.fills()),1)
        self.assertEqual(out['positions'][0]['liquidation_value_usd'],'0')
    def test_model_usage_attributed_to_separate_account(self):
        path=self.app/'rug_desk/runtime/ai-budget.sqlite3';path.parent.mkdir(parents=True)
        db=sqlite3.connect(path)
        db.executescript('CREATE TABLE advisory_request_accounts(request_id TEXT,account_id TEXT);CREATE TABLE advisory_usage(request_id TEXT,billed_usd TEXT);')
        db.execute('INSERT INTO advisory_request_accounts VALUES(?,?)',('n',self.book.identity['account_id']))
        db.execute('INSERT INTO advisory_request_accounts VALUES(?,?)',('other','other-account'))
        db.execute("INSERT INTO advisory_usage VALUES('other','0.5')");db.commit()
        self.assertFalse(usage(self.book,self.app)['known'])
        db.execute("INSERT INTO advisory_usage VALUES('n','0.12')");db.commit();db.close()
        self.assertEqual(usage(self.book,self.app)['billed_usd'],'0.12')

    def test_watch_completion_includes_daily_report_dependencies(self):
        with patch('zoneinfo.ZoneInfo',side_effect=RuntimeError('Missing timezone data')):
            with self.assertRaises(RuntimeError):cycle(self.app,'watch',clock=lambda:NOW,send=False)
        row=self.book.db.execute("SELECT status FROM runs WHERE job='watch'").fetchone()
        self.assertEqual(row[0],'error')
        self.assertEqual(self.book.fills(),[])

    def test_finalized_observation_admits_once_through_original_pipeline(self):
        self.fixture_trial();self.rpc.page=[{'signature':'3'*88}]
        self.source._wallet(key(77),'replay_leader',NOW)
        self.rpc.page=[{'signature':SIG,'slot':100,'confirmationStatus':'finalized'},self.rpc.page[0]]
        signal=self.source._wallet(key(77),'replay_leader',NOW)[0]
        self.assertTrue(self.send(signal,event_verifier=self.source.verify)['entry'])
        self.assertFalse(self.send(signal,event_verifier=self.source.verify)['entry'])
        self.assertEqual(len(self.book.fills()),1)

    def test_abandoned_process_is_a_monitoring_gap_not_completed_success(self):
        with self.book.transaction():self.book.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',('abandoned','watch',NOW-300,None,'running','{}'))
        cycle(self.app,'watch',clock=lambda:NOW,send=False)
        self.assertEqual(self.book.db.execute("SELECT status FROM runs WHERE id='abandoned'").fetchone()[0],'interrupted')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM neon_monitoring_gaps').fetchone()[0],1)
