"""Exact historical-state fallback using isolated synthetic RPC providers."""
from pathlib import Path
import tempfile
import unittest

from sniper.adapters.evm import CHAIN_ID, RpcClient, RpcError, HistoricalStateUnavailable, ReorgError
from sniper.adapters.evm_store import EvmStore
from sniper.exploratory_source import ExploratorySource
from test_engine import quote
from test_evm import NOW, bh
from test_same_interval_followup import Chain


ERROR = {'code': -32000, 'message': 'historical state ' + 'ab' * 32 + ' is not available'}


class HistoricalFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EvmStore(Path(self.temp.name) / 'events.sqlite3')
        self.primary, self.secondary = Chain(), Chain()
        self.secondary_calls = []
        self.error = ERROR
        self.mismatch = None
        self.rpc = RpcClient(transport=self.primary_call, max_retries=0)
        self.fallback = RpcClient(transport=self.secondary_call, max_retries=0)
        market = type('Market', (), {'quote': lambda _, *a, **kw: quote(now=kw['now'])})()
        self.source = ExploratorySource(self.rpc, self.store, market, clock=lambda: NOW)
        self.source.collect()
        self.sample = next(row for row in self.source.pending()
                           if row['tx_hash'] == self.primary.first['transactionHash'])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def primary_call(self, payload):
        if isinstance(payload, list):
            return [self.primary_call(item) for item in payload]
        if payload['method'] == 'eth_call':
            return {'jsonrpc': '2.0', 'id': payload['id'], 'error': self.error}
        return self.primary(payload)

    def secondary_call(self, payload):
        if isinstance(payload, list):
            return [self.secondary_call(item) for item in payload]
        method, params = payload['method'], payload['params']
        self.secondary_calls.append((method, params))
        result = self.secondary(payload)
        if self.mismatch == 'chain' and method == 'eth_chainId':
            result['result'] = hex(CHAIN_ID + 1)
        if method == 'eth_getBlockByNumber':
            if self.mismatch == 'genesis' and params[0] == '0x0':
                result['result']['hash'] = bh(0, True)
            if self.mismatch == 'block' and params[0] == hex(self.sample['block_number']):
                result['result']['hash'] = bh(self.sample['block_number'], True)
        return result

    def test_fallback_preserves_block_and_source_receipts_with_proven_binding(self):
        proof = self.source.prove(self.sample, fallback_rpc=self.fallback)
        self.assertTrue(proof['verified_swap'])
        self.assertEqual(proof['historical_state_provider'], 'quote_rpc')
        self.assertEqual(proof['historical_state_binding'], {'chain_id': CHAIN_ID,
            'genesis_hash': bh(0), 'block_number': self.sample['block_number'], 'block_hash': self.sample['block_hash']})
        calls = [params for method, params in self.secondary_calls if method == 'eth_call']
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(params[1] == hex(self.sample['block_number']) for params in calls))
        self.assertTrue(all(method in {'eth_chainId', 'eth_getBlockByNumber', 'eth_call'} for method, _ in self.secondary_calls))
        self.assertIn('eth_getTransactionReceipt', [method for method, _ in self.primary.calls])
        self.assertIn('eth_getTransactionByHash', [method for method, _ in self.primary.calls])

    def test_no_fallback_remains_unavailable(self):
        with self.assertRaises(HistoricalStateUnavailable):
            self.source.prove(self.sample)
        self.assertEqual(self.secondary_calls, [])

    def test_chain_genesis_and_block_mismatch_fail_before_fallback_metadata(self):
        for mismatch in ('chain', 'genesis', 'block'):
            with self.subTest(mismatch=mismatch):
                self.mismatch = mismatch
                self.secondary_calls.clear()
                with self.assertRaises((RpcError, ReorgError)):
                    self.source.prove(self.sample, fallback_rpc=self.fallback)
                self.assertNotIn('eth_call', [method for method, _ in self.secondary_calls])

    def test_unrelated_rpc_errors_never_use_fallback(self):
        for error in ({'code': -32000, 'message': 'execution reverted'},
                      {'code': -32005, 'message': ERROR['message']},
                      {'code': -32000, 'message': 'historical state unknown is not available'}):
            with self.subTest(error=error):
                self.error = error
                with self.assertRaises(RpcError) as raised:
                    self.source.prove(self.sample, fallback_rpc=self.fallback)
                self.assertNotIsInstance(raised.exception, HistoricalStateUnavailable)
                self.assertEqual(self.secondary_calls, [])

    def test_mixed_batch_error_does_not_hide_other_failure(self):
        original = self.primary_call
        def mixed(payload):
            if isinstance(payload, list):
                responses = [original(item) for item in payload]
                responses[-1]['error'] = {'code': -32000, 'message': 'execution reverted'}
                return responses
            return original(payload)
        self.rpc.transport = mixed
        with self.assertRaises(RpcError) as raised:
            self.source.prove(self.sample, fallback_rpc=self.fallback)
        self.assertNotIsInstance(raised.exception, HistoricalStateUnavailable)
        self.assertEqual(self.secondary_calls, [])

    def test_late_fallback_block_change_rejects_proof(self):
        original = self.secondary_call
        def changed(payload):
            result = original(payload)
            if isinstance(payload, dict) and payload['method'] == 'eth_call':
                self.mismatch = 'block'
            return result
        self.fallback.transport = changed
        with self.assertRaises(ReorgError):
            self.source.prove(self.sample, fallback_rpc=self.fallback)


class HistoricalErrorClassificationTests(unittest.TestCase):
    def test_only_concrete_historical_eth_call_is_typed(self):
        def fail(payload):
            if isinstance(payload, list):
                return [fail(item) for item in payload]
            return {'jsonrpc': '2.0', 'id': payload['id'], 'error': ERROR}
        rpc = RpcClient(transport=fail, max_retries=0)
        for method, params, typed in (
            ('eth_call', [{}, '0x64'], True),
            ('eth_call', [{}, 'latest'], False),
            ('eth_call', [{}, 'pending'], False),
            ('eth_getLogs', [{'fromBlock': '0x64', 'toBlock': '0x65'}], False),
            ('eth_getTransactionReceipt', ['0x' + 'ab' * 32], False),
        ):
            for batched in (False, True):
                with self.subTest(method=method, params=params, batched=batched):
                    with self.assertRaises(RpcError) as raised:
                        rpc.call_many([(method, params)]) if batched else rpc.call(method, params)
                    self.assertEqual(isinstance(raised.exception, HistoricalStateUnavailable), typed)
                    self.assertNotIn(ERROR['message'], str(raised.exception))


if __name__ == '__main__':
    unittest.main()
