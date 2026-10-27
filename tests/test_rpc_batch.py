import unittest
from unittest.mock import patch
from sniper.adapters.evm import RpcClient, RpcError, EvmCollector
from test_evm import FakeChain


class RpcBatchTests(unittest.TestCase):
    def test_batch_bursts_are_bounded_and_paced(self):
        waits = []
        def transport(requests):
            return [{"jsonrpc": "2.0", "id": r["id"], "result": "0x1"} for r in requests]
        rpc = RpcClient(transport=transport, sleeper=waits.append)
        rpc.batch_enabled = True
        with patch('sniper.adapters.evm.time.monotonic', return_value=100):
            rpc.call_many([('eth_chainId', [])])
            rpc.call_many([('eth_chainId', [])])
        self.assertEqual(waits, [0.5])
        with self.assertRaises(ValueError):
            rpc.call_many([('eth_chainId', [])] * 11)

    def test_reordered_results_preserve_request_order(self):
        def transport(requests):
            return [{"jsonrpc": "2.0", "id": r["id"], "result": r["method"]}
                    for r in reversed(requests)]
        rpc = RpcClient(transport=transport)
        self.assertEqual(rpc.call_many([("eth_chainId", []), ("eth_blockNumber", [])]),
                         ["eth_chainId", "eth_blockNumber"])

    def test_missing_duplicate_foreign_and_error_results_fail_closed(self):
        for failure in ("missing", "duplicate", "foreign", "error", "boolean_id"):
            def transport(requests):
                rows = [{"jsonrpc": "2.0", "id": r["id"], "result": "0x1"} for r in requests]
                if failure == "missing": rows.pop()
                if failure == "duplicate": rows[1]["id"] = rows[0]["id"]
                if failure == "foreign": rows[1]["id"] = 999
                if failure == "error": rows[1]["error"] = {"code": -1}
                if failure == "boolean_id": rows[0]["id"] = True
                return rows
            with self.subTest(failure=failure), self.assertRaises(RpcError):
                RpcClient(transport=transport).call_many([("eth_chainId", []), ("eth_blockNumber", [])])

    def test_write_method_rejected_before_transport(self):
        def transport(_):
            self.fail("write request reached transport")
        with self.assertRaises(RpcError):
            RpcClient(transport=transport).call_many([("eth_sendRawTransaction", ["0x00"])])

    def test_correlated_contract_revert_is_reported_without_partial_results_or_provider_message(self):
        def transport(requests):
            return [{'jsonrpc':'2.0','id':requests[0]['id'],'error':{'code':3,'message':'private provider URL'}},
                    {'jsonrpc':'2.0','id':requests[1]['id'],'result':'0x1'}]
        with self.assertRaisesRegex(RpcError, '^RPC eth_call returned error 3; observation unavailable$'):
            RpcClient(transport=transport).call_many([('eth_call',[]),('eth_blockNumber',[])])

    def test_error_cannot_mask_another_response_with_wrong_identity(self):
        def transport(requests):
            return [{'jsonrpc':'2.0','id':requests[0]['id'],'error':{'code':3,'message':'reverted'}},
                    {'jsonrpc':'2.0','id':999,'result':'0x1'}]
        with self.assertRaisesRegex(RpcError,'response identity is invalid'):
            RpcClient(transport=transport).call_many([('eth_call',[]),('eth_blockNumber',[])])

    def test_batched_collector_matches_serial_canonical_events(self):
        chain = FakeChain()
        def transport(request):
            return [chain(r) for r in reversed(request)] if isinstance(request, list) else chain(request)
        rpc = RpcClient(transport=transport)
        rpc.batch_enabled = True
        collector = EvmCollector(rpc)
        try:
            result = collector.scan(100, 105)
            self.assertEqual(len(result["new_pools"]), 1)
            self.assertEqual(len(result["swaps"]), 1)
            self.assertEqual(result["cursor"]["block_number"], 105)
        finally:
            collector.store.close()
