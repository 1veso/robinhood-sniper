"""Bounded same-interval discovery, entirely synthetic and provider-free."""
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from sniper.adapters.evm import CHAIN_ID, FACTORY, USDG, RpcClient, RpcBudgetExceeded, ReorgError, decode_swap
from sniper.adapters.evm_store import EvmStore
from sniper.confirmation_book import ConfirmationBook
from sniper.exploration import ExploratoryEngine
from sniper.exploratory_source import ExploratorySource, COLLECTION_REVISION, signal_id
from sniper.ledger import Ledger
from sniper.models import Candidate, SafetyEvidence
from sniper.policy import RiskPolicy
from test_engine import quote
from test_evm import NOW, TOKEN, POOL, WALLET, OTHER, bh, word, swap_log, transfer


class Chain:
    def __init__(self, count=40, head=150):
        self.head, self.logs, self.calls = head, [], []
        self.fail_pool_request = None
        self.pool_requests = 0
        self.fork = False
        for i in range(count):
            number = 102 + i if head == 150 else 100 + i * 600
            self.logs.append({**swap_log(), 'transactionHash': '0x' + f'{i+1:064x}',
                'blockNumber': hex(number), 'blockHash': bh(number), 'address': OTHER,
                'topics': [swap_log()['topics'][0], '0x' + word(OTHER), '0x' + word('0x' + f'{100+i:040x}')]})
        ordered = sorted(self.logs, key=lambda raw: signal_id(decode_swap(raw)))
        self.first, self.second = ordered[0], ordered[-1]
        self.first['address'] = self.second['address'] = POOL

    def __call__(self, payload):
        if isinstance(payload, list):
            return [self(p) for p in payload]
        method, params = payload['method'], payload['params']
        self.calls.append((method, params))
        if method == 'eth_chainId': result = hex(CHAIN_ID)
        elif method == 'eth_blockNumber': result = hex(self.head)
        elif method == 'eth_getBlockByNumber':
            number = int(params[0], 16)
            result = {'number': hex(number), 'hash': bh(number, self.fork),
                'parentHash': bh(max(0, number-1), self.fork), 'timestamp': hex(NOW - (self.head-number)//10)}
        elif method == 'eth_getLogs':
            request = params[0]
            if 'address' in request:
                self.pool_requests += 1
                if self.pool_requests == self.fail_pool_request:
                    raise RpcBudgetExceeded('synthetic shared entry deadline')
            low, high = int(request['fromBlock'],16), int(request['toBlock'],16)
            result = [dict(raw) for raw in self.logs if low <= int(raw['blockNumber'],16) <= high
                      and ('address' not in request or raw['address'] == request['address'])]
        elif method == 'eth_getTransactionByHash':
            raw = next(raw for raw in self.logs if raw['transactionHash'] == params[0])
            result = {'hash': raw['transactionHash'], 'from': '0x' + raw['topics'][2][-40:], 'to': OTHER}
        elif method == 'eth_getTransactionReceipt':
            raw = next(raw for raw in self.logs if raw['transactionHash'] == params[0])
            buyer = '0x' + raw['topics'][2][-40:]
            transfers = [transfer(USDG,buyer,POOL,10_000_000,0), transfer(TOKEN,POOL,buyer,2_000_000_000,1)]
            for leg in transfers:
                leg.update(transactionHash=raw['transactionHash'],blockHash=raw['blockHash'],blockNumber=raw['blockNumber'])
            result = {'transactionHash': raw['transactionHash'], 'status':'0x1',
                'blockNumber':raw['blockNumber'],'blockHash':raw['blockHash'],'logs':transfers+[raw]}
        elif method == 'eth_call':
            call=params[0]
            values={'0x0dfe1681':TOKEN,'0xd21220a7':USDG,'0xddca3f43':3000,'0x1698ee82':POOL,'0x313ce567':6}
            result='0x'+word(values[call['data'][:10]])
        elif method == 'eth_getCode': result='0x6000'
        else: raise AssertionError(method)
        return {'jsonrpc':'2.0','id':payload['id'],'result':result}


class SameIntervalFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'events.sqlite3'
        self.store = EvmStore(self.path)
        self.chain = Chain()
        self.now = NOW
        self.open_source()

    def open_source(self):
        self.rpc = RpcClient(transport=self.chain, max_retries=0)
        market = type('Market',(),{'quote':lambda _,*a,**kw:quote(now=kw['now'])})()
        self.source = ExploratorySource(self.rpc,self.store,market,clock=lambda:self.now)
        self.book = ConfirmationBook(self.store)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def first_proof(self, sample_size=32, watch_pools=()):
        result = self.source.collect(sample_size=sample_size, watch_pools=watch_pools)
        first = next(s for s in self.source.pending() if s['tx_hash'] == self.chain.first['transactionHash'])
        proof = self.source.prove(first)
        self.book.record(signal_id(first),proof)
        return result, first, proof

    def test_only_first_of_two_buyers_globally_sampled_followup_forms_quorum_once(self):
        result, first, proof = self.first_proof()
        self.assertEqual(result['global_sampled_transactions'],32)
        self.assertEqual(result['collection_revision'],COLLECTION_REVISION)
        self.assertNotIn(self.chain.second['transactionHash'],[s['tx_hash'] for s in self.source.pending()])
        self.assertEqual(len(self.book.recent(TOKEN,POOL,now=NOW,age=900,minimum_spend=D(10))),1)
        cursor = self.store.cursor(CHAIN_ID)
        followed = self.source.followup(POOL, sampled=proof)
        self.assertEqual(followed['inserted'],1)
        self.assertEqual(self.store.cursor(CHAIN_ID),cursor)
        second = next(s for s in self.source.pending() if s['tx_hash'] == self.chain.second['transactionHash'])
        second_proof = self.source.prove(second)
        self.book.record(signal_id(second),second_proof)
        cohort=self.book.recent(TOKEN,POOL,now=NOW,age=900,minimum_spend=D(10))
        confirmations=self.book.confirm(cohort,self.rpc)
        self.assertEqual(len(confirmations),2)
        self.assertEqual(len({c.wallet for c in confirmations}),2)
        self.assertEqual(len({c.tx_hash for c in confirmations}),2)
        policy=RiskPolicy.from_file(Path(__file__).resolve().parents[1]/'config/exploration-v3.toml')
        ledger=Ledger(':memory:',D(1000));engine=ExploratoryEngine(ledger,policy)
        try:
            asset='eip155:4663/erc20:'+TOKEN
            safety=SafetyEvidence(asset,NOW,{name:True for name in policy.required_safety_checks},'isolated fixture',{'report_pool_id':POOL})
            candidate=Candidate(signal_id(second),asset,'TEST',second['observed_at'],confirmations,safety,engine.strategy_name,{'decision_at':NOW})
            q=quote(asset=asset,now=NOW,pool_id=POOL,route_max_notional_usd=D(5),route_max_sell_quantity=D(5),route_impact_bps=D(1))
            self.assertEqual(engine.consider(candidate,q,NOW)['verdict'],'PAPER_BUY')
            self.assertEqual(engine.consider(candidate,q,NOW)['reason'],'duplicate_event')
            before_calls=self.chain.pool_requests
            self.assertEqual(self.source.followup(POOL,sampled=proof)['state'],'COMPLETE')
            self.assertEqual(self.chain.pool_requests,before_calls)
            self.assertEqual(ledger.conn.execute('SELECT count(*) FROM fills').fetchone()[0],1)
        finally:ledger.close()

    def test_chunk_progress_survives_deadline_restart_without_repeating_finished_chunks(self):
        self.chain=Chain(count=4,head=3005);self.open_source()
        _,_,proof=self.first_proof(sample_size=1)
        cursor=self.store.cursor(CHAIN_ID)
        self.chain.fail_pool_request=2
        with self.assertRaises(RpcBudgetExceeded):self.source.followup(POOL,sampled=proof)
        job=self.store.db.execute('SELECT * FROM exploration_followups').fetchone()
        self.assertEqual(job['next_block'],501);self.assertEqual(job['state'],'PENDING')
        self.store.close();self.store=EvmStore(self.path);self.open_source()
        self.chain.fail_pool_request=None
        result=self.source.resume_followups()
        self.assertEqual(result['jobs'][0]['state'],'COMPLETE')
        self.assertEqual(self.store.cursor(CHAIN_ID),cursor)
        requests=[p[0] for m,p in self.chain.calls if m=='eth_getLogs' and 'address' in p[0]]
        self.assertEqual(sum(int(r['fromBlock'],16)==1 for r in requests),1)
        self.assertEqual(self.store.db.execute('SELECT requests FROM exploration_followups').fetchone()[0],6)
        self.assertLessEqual(result['jobs'][0]['selected_legs'],16)

    def test_noncanonical_followup_cannot_supply_second_buyer(self):
        _,_,proof=self.first_proof()
        self.chain.second['blockHash']=bh(int(self.chain.second['blockNumber'],16),True)
        with self.assertRaises(ReorgError):self.source.followup(POOL,sampled=proof)
        self.assertEqual(len(self.book.recent(TOKEN,POOL,now=NOW,age=900,minimum_spend=D(10))),1)
        self.assertNotIn(self.chain.second['transactionHash'],[s['tx_hash'] for s in self.source.pending()])

    def test_existing_watched_slots_limit_new_pools_to_eight_per_interval(self):
        watched=['0x'+f'{500+i:040x}' for i in range(8)]
        _,_,proof=self.first_proof(watch_pools=watched)
        self.assertEqual(self.source.followup(POOL,sampled=proof)['state'],'POOL_BOUND_REACHED')
        self.assertEqual(self.chain.pool_requests,0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM exploration_followups').fetchone()[0],8)

    def test_pool_selection_never_exceeds_sixteen_legs(self):
        for raw in self.chain.logs: raw['address']=POOL
        _,_,proof=self.first_proof(sample_size=1)
        result=self.source.followup(POOL,sampled=proof)
        self.assertEqual(result['selected_legs'],16)
        self.assertEqual(result['inserted'],15)
        self.assertEqual(len(self.source.pending()),16)

    def test_changed_interval_anchor_blocks_followup_before_appending(self):
        _,_,proof=self.first_proof()
        self.chain.fork=True
        with self.assertRaises(ReorgError):self.source.followup(POOL,sampled=proof)
        self.assertEqual(self.chain.pool_requests,0)
        self.assertEqual(len(self.source.pending()),32)

    def test_concurrent_cursor_move_during_final_header_cannot_admit_followed_events(self):
        _,_,proof=self.first_proof()
        before=self.store.cursor(CHAIN_ID)
        transport=self.rpc.transport
        anchor_reads=[0]
        def concurrent(payload):
            result=transport(payload)
            if payload['method']=='eth_getBlockByNumber' and int(payload['params'][0],16)==before['block_number']:
                anchor_reads[0]+=1
                if anchor_reads[0]==2:
                    self.store.commit_chunk(CHAIN_ID,[],[],
                        {'number':before['block_number']+1,'hash':bh(before['block_number']+1)},expected_cursor=before)
            return result
        self.rpc.transport=concurrent
        with self.assertRaisesRegex(RuntimeError,'cursor changed concurrently'):
            self.source.followup(POOL,sampled=proof)
        self.assertEqual(self.store.db.execute('SELECT state FROM exploration_followups').fetchone()[0],'PENDING')
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM evm_events WHERE tx_hash=?',(self.chain.second['transactionHash'],)).fetchone())

    def test_concurrent_rewind_then_same_tip_restoration_is_rejected_by_generation(self):
        _,_,proof=self.first_proof()
        before=self.store.cursor(CHAIN_ID)
        transport=self.rpc.transport
        anchor_reads=[0]
        def concurrent(payload):
            result=transport(payload)
            if payload['method']=='eth_getBlockByNumber' and int(payload['params'][0],16)==before['block_number']:
                anchor_reads[0]+=1
                if anchor_reads[0]==2:
                    self.store.rewind(CHAIN_ID,{'number':100,'hash':bh(100)},NOW)
                    self.store.commit_chunk(CHAIN_ID,[],[],{'number':before['block_number'],'hash':before['block_hash']},
                                            expected_cursor=self.store.cursor(CHAIN_ID))
            return result
        self.rpc.transport=concurrent
        with self.assertRaisesRegex(ReorgError,'generation changed'):
            self.source.followup(POOL,sampled=proof)
        self.assertEqual(self.store.cursor(CHAIN_ID),before)
        self.assertEqual(self.store.db.execute('SELECT state FROM exploration_followups').fetchone()[0],'PENDING')
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM evm_events WHERE tx_hash=?',(self.chain.second['transactionHash'],)).fetchone())

    def test_global_sample_bound_is_enforced_before_provider_calls(self):
        for size in (0,33,True,1.5):
            with self.subTest(size=size),self.assertRaises(ValueError):self.source.collect(sample_size=size)
        self.assertFalse(self.chain.calls)

    def test_invalidated_pending_checkpoint_is_recorded_once_without_provider_work(self):
        self.chain=Chain(count=4,head=3005);self.open_source()
        _,_,proof=self.first_proof(sample_size=1)
        self.chain.fail_pool_request=2
        with self.assertRaises(RpcBudgetExceeded):self.source.followup(POOL,sampled=proof)
        self.store.rewind(CHAIN_ID,{'number':0,'hash':bh(0)},NOW)
        calls=len(self.chain.calls)
        with self.assertRaisesRegex(ReorgError,'checkpoint was invalidated'):self.source.resume_followups()
        self.assertEqual(len(self.chain.calls),calls)
        self.assertEqual(self.store.db.execute('SELECT state FROM exploration_followups').fetchone()[0],'INVALIDATED')
        self.assertEqual(self.source.resume_followups()['jobs'],[])
        self.assertEqual(len(self.chain.calls),calls)

    def test_followup_requires_canonical_verified_discovery(self):
        self.source.collect()
        self.assertEqual(self.source.followup(POOL)['state'],'NO_VERIFIED_BUYER')
        self.assertEqual(self.chain.pool_requests,0)

    def test_pending_is_oldest_first_and_expiry_preserves_original_time(self):
        self.source.collect()
        pending=self.source.pending()
        self.assertEqual([s['observed_at'] for s in pending],sorted(s['observed_at'] for s in pending))
        original={signal_id(s):s['observed_at'] for s in pending}
        self.now=NOW+901
        self.assertEqual(self.source.expire_pending(limit=5),5)
        self.assertEqual(self.source.pending_summary()['expired_unrecorded'],27)
        self.assertFalse(self.source.pending())
        for row in self.store.db.execute('SELECT * FROM exploration_outcomes'):
            result=json.loads(row['payload'])
            self.assertEqual(result['verdict'],'UNEVALUATED');self.assertEqual(result['reason'],'signal_expired')
            self.assertEqual(result['observed_at'],original[row['event_id']])

    def test_expiry_recovers_committed_entry_after_restart_without_relabeling_or_filling_again(self):
        _,_,proof=self.first_proof()
        self.source.followup(POOL,sampled=proof)
        second=next(s for s in self.source.pending() if s['tx_hash']==self.chain.second['transactionHash'])
        self.book.record(signal_id(second),self.source.prove(second))
        confirmations=self.book.confirm(self.book.recent(TOKEN,POOL,now=NOW,age=900,minimum_spend=D(10)),self.rpc)
        policy=RiskPolicy.from_file(Path(__file__).resolve().parents[1]/'config/exploration-v3.toml')
        ledger=Ledger(':memory:',D(1000));engine=ExploratoryEngine(ledger,policy)
        try:
            asset='eip155:4663/erc20:'+TOKEN
            safety=SafetyEvidence(asset,NOW,{name:True for name in policy.required_safety_checks},'isolated fixture',{'report_pool_id':POOL})
            candidate=Candidate(signal_id(second),asset,'TEST',second['observed_at'],confirmations,safety,engine.strategy_name,{'decision_at':NOW})
            q=quote(asset=asset,now=NOW,pool_id=POOL,route_max_notional_usd=D(5),route_max_sell_quantity=D(5),route_impact_bps=D(1))
            self.assertEqual(engine.consider(candidate,q,NOW)['verdict'],'PAPER_BUY')
            before=ledger.snapshot()
            # Financial commit succeeded, but source.finish never ran.
            self.store.close();self.store=EvmStore(self.path);self.open_source();self.now=NOW+901
            def recorded(event_id):
                row=ledger.conn.execute('SELECT verdict,reason,observed_at AS decision_observed_at FROM decisions WHERE event_id=?',(event_id,)).fetchone()
                return dict(row) if row else None
            self.assertEqual(self.source.expire_pending(resolve_outcome=recorded),32)
            row=self.store.db.execute('SELECT payload FROM exploration_outcomes WHERE event_id=?',(signal_id(second),)).fetchone()
            outcome=json.loads(row['payload'])
            self.assertEqual(outcome['verdict'],'PAPER_BUY');self.assertEqual(outcome['reason'],'eligible')
            self.assertEqual(outcome['observed_at'],second['observed_at'])
            self.assertEqual(outcome['decision_observed_at'],NOW)
            self.assertNotIn('expired_at',outcome)
            self.assertEqual(self.source.pending_summary()['expired_recorded'],32)
            self.assertEqual(self.source.pending_summary()['recovered_ledger_outcomes'],1)
            self.assertEqual(ledger.snapshot(),before)
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0],1)
        finally:ledger.close()

    def test_interval_progress_rolls_back_with_sample_cursor_on_commit_failure(self):
        cursor=self.store.cursor(CHAIN_ID)
        original=self.store.commit_chunk
        def fail(*args,**kwargs):
            callback=kwargs['on_commit']
            def interrupted(inserted):callback(inserted);raise RuntimeError('synthetic atomic commit interruption')
            return original(*args,**{**kwargs,'on_commit':interrupted})
        self.store.commit_chunk=fail
        with self.assertRaises(RuntimeError):self.source.collect()
        self.assertEqual(self.store.cursor(CHAIN_ID),cursor)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM exploration_intervals').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM evm_events').fetchone()[0],0)


if __name__=='__main__':unittest.main()
