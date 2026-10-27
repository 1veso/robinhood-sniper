from __future__ import annotations

import tempfile
import sqlite3
import uuid
import unittest
import urllib.error
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from sniper.adapters.evm import (
    CHAIN_ID, FACTORY, USDG, WETH, POOL_CREATED_TOPIC, SWAP_TOPIC, TRANSFER_TOPIC,
    DecodeError, EvmCollector, EvmRegistry, ReorgError, RpcClient, RpcError, RpcBudgetExceeded,
    decode_pool_created, decode_swap, decode_transfer, token_units, verify_purchase,
)
from sniper.adapters.evm_store import EvmStore

TOKEN = "0x" + "11" * 20
POOL = "0x" + "aa" * 20
WALLET = "0x" + "22" * 20
OTHER = "0x" + "33" * 20
TX_POOL = "0x" + "01" * 32
TX_SWAP = "0x" + "02" * 32
NOW = 1_800_000_000


def word(value):
    if isinstance(value, str):
        return value[2:].rjust(64, "0")
    return f"{value % (1 << 256):064x}"


def bh(number, fork=False):
    return "0x" + word(number + (10_000 if fork else 0))


def common(contract, tx, block, index, fork=False):
    return {"address": contract, "transactionHash": tx, "blockNumber": hex(block),
            "blockHash": bh(block, fork), "logIndex": hex(index), "removed": False}


def pool_log(fork=False):
    return {**common(FACTORY, TX_POOL, 101, 0, fork),
            "topics": [POOL_CREATED_TOPIC, "0x" + word(TOKEN), "0x" + word(USDG), "0x" + word(3000)],
            "data": "0x" + word(60) + word(POOL)}


def swap_log(fork=False):
    return {**common(POOL, TX_SWAP, 102, 2, fork),
            "topics": [SWAP_TOPIC, "0x" + word(OTHER), "0x" + word(WALLET)],
            "data": "0x" + word(-2_000_000_000) + word(10_000_000) + word(2 ** 96) + word(123456) + word(-1)}


def transfer(contract, source, dest, amount, index, fork=False):
    return {**common(contract, TX_SWAP, 102, index, fork),
            "topics": [TRANSFER_TOPIC, "0x" + word(source), "0x" + word(dest)],
            "data": "0x" + word(amount)}


def receipt(fork=False):
    return {"transactionHash": TX_SWAP, "status": "0x1", "blockNumber": "0x66", "blockHash": bh(102, fork),
            "logs": [transfer(USDG, WALLET, POOL, 10_000_000, 0, fork),
                     transfer(TOKEN, POOL, WALLET, 2_000_000_000, 1, fork), swap_log(fork)]}


class FakeChain:
    def __init__(self):
        self.chain_id = CHAIN_ID
        self.fork_from = None
        self.head = 150
        self.calls = []
        self.fail_receipt = False
        self.change_end_on_repeat = False
        self.end_reads = 0

    def fork(self, number):
        return self.fork_from is not None and number >= self.fork_from

    def __call__(self, payload):
        method, params = payload["method"], payload["params"]
        self.calls.append((method, params))
        result = None
        if method == "eth_chainId":
            result = hex(self.chain_id)
        elif method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            if number == 105:
                self.end_reads += 1
                if self.change_end_on_repeat and self.end_reads > 1:
                    self.fork_from = 101
            result = {"number": hex(number), "hash": bh(number, self.fork(number)),
                      "parentHash": bh(max(0, number - 1), self.fork(max(0, number - 1))),
                      "timestamp": hex(NOW - (self.head - number))}
        elif method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            candidates = [pool_log(self.fork(101))] if f["topics"][0] == POOL_CREATED_TOPIC else [swap_log(self.fork(102))]
            result = [l for l in candidates if lo <= int(l["blockNumber"], 16) <= hi]
        elif method == "eth_getTransactionByHash":
            result = {"hash": TX_SWAP, "from": WALLET, "to": OTHER}
        elif method == "eth_getTransactionReceipt":
            if self.fail_receipt:
                return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "unavailable"}}
            result = receipt(self.fork(102))
        elif method == "eth_call":
            call = params[0]
            if call["data"] == "0x313ce567":
                result = "0x" + word(6 if call["to"] == USDG else 9)
            elif call["data"] in {"0x95d89b41", "0x06fdde03"}:
                result = "0x" + b"TEST".hex().ljust(64, "0")
            elif call["data"] == "0x8da5cb5b":
                result = "0x" + word(0)
            else:
                raise AssertionError(f"unexpected selector {call['data']}")
        elif method == "eth_getCode":
            result = "0x6000"
        else:
            raise AssertionError(f"unexpected method {method}")
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


def collector(chain, store=None):
    return EvmCollector(RpcClient(transport=chain), store=store,
                        watchlist={WALLET: {"qualified": True, "cluster_id": "independent-a", "qualification_evidence": "review-2026-10"}},
                        quote_usd={USDG: {"price_usd": "1", "observed_at": NOW}}, clock=lambda: NOW)


class RpcTests(unittest.TestCase):
    def test_expired_budget_prevents_retry_and_scope_restores_access(self):
        tick, calls = [0.0], []
        def transport(payload):
            calls.append(payload)
            tick[0] = 2.0
            raise urllib.error.URLError("down")
        rpc = RpcClient(transport=transport, sleeper=lambda _: self.fail("Expired budget must not sleep"))
        with patch("sniper.adapters.evm.time.monotonic", side_effect=lambda: tick[0]):
            with self.assertRaises(RpcBudgetExceeded), rpc.budget(1):
                rpc.verify_chain()
            self.assertEqual(len(calls), 1)
            rpc.transport = FakeChain()
            self.assertEqual(rpc.verify_chain(), CHAIN_ID)

    def test_method_allowlist_refuses_transactions(self):
        rpc = RpcClient(transport=lambda p: self.fail("should never call transport"))
        for method in ["eth_sendRawTransaction", "eth_sendTransaction", "personal_sign", "eth_sign", "wallet_addEthereumChain"]:
            with self.assertRaisesRegex(RpcError, "read-only"):
                rpc.call(method, [])

    def test_rpc_error_never_becomes_absent_function(self):
        rpc = RpcClient(transport=lambda p: {"jsonrpc": "2.0", "id": p["id"], "error": {"code": 3, "message": "execution reverted"}})
        with self.assertRaisesRegex(RpcError, "error 3"):
            rpc.call("eth_call", [])

    def test_wrong_chain(self):
        chain = FakeChain(); chain.chain_id = 1
        with self.assertRaisesRegex(RpcError, "wrong chain"):
            collector(chain).poll(max_blocks=5)

    def test_mismatched_id(self):
        rpc = RpcClient(transport=lambda p: {"jsonrpc": "2.0", "id": 999, "result": "0x1237"})
        with self.assertRaises(RpcError):
            rpc.verify_chain()

    def test_bounded_transport_retries(self):
        calls = []
        def transport(p):
            calls.append(p)
            raise urllib.error.URLError("down")
        rpc = RpcClient(transport=transport, max_retries=2, sleeper=lambda t: None)
        with self.assertRaises(RpcError):
            rpc.call("eth_chainId")
        self.assertEqual(len(calls), 3)

    def test_https_required(self):
        with self.assertRaises(ValueError):
            RpcClient("http://remote.example/rpc")
        RpcClient("http://127.0.0.1:1234")


class AbiTests(unittest.TestCase):
    def test_pool_uses_second_data_word_not_tickspacing(self):
        event = decode_pool_created(pool_log())
        self.assertEqual(event["pool"], POOL)
        self.assertEqual(event["tick_spacing"], 60)
        self.assertNotEqual(event["pool"], "0x" + "0" * 38 + "3c")

    def test_signed_amounts_and_tick(self):
        event = decode_swap(swap_log())
        self.assertEqual(event["amount0_raw"], "-2000000000")
        self.assertEqual(event["amount1_raw"], "10000000")
        self.assertEqual(event["tick"], -1)

    def test_truncated_data_rejected(self):
        log = pool_log(); log["data"] = log["data"][:66]
        with self.assertRaises(DecodeError):
            decode_pool_created(log)

    def test_removed_log_rejected(self):
        log = swap_log(); log["removed"] = True
        with self.assertRaises(DecodeError):
            decode_swap(log)

    def test_decimals_are_not_assumed_eighteen(self):
        self.assertEqual(token_units(1234567, 6), Decimal("1.234567"))
        with self.assertRaises(DecodeError):
            token_units(10, None)
        with self.assertRaises(DecodeError):
            token_units(10, 255)
        self.assertEqual(token_units(2 ** 256 - 1, 0), Decimal(2 ** 256 - 1))

    def test_canonical_weth(self):
        self.assertEqual(WETH, "0x0bd7d308f8e1639fab988df18a8011f41eacad73")
        self.assertNotIn("0x4200000000000000000000000000000000000006", EvmRegistry().bases)


class PurchaseTests(unittest.TestCase):
    def verify(self, r=None, tx=None):
        return verify_purchase(decode_swap(swap_log()), decode_pool_created(pool_log()),
                               r or receipt(), tx or {"from": WALLET, "hash": TX_SWAP}, WALLET, {USDG: "USDG"})

    def test_direct_swap_has_payment_and_receipt(self):
        buy = self.verify()
        self.assertTrue(buy["verified_swap"])
        self.assertEqual(buy["spent_raw"], "10000000")
        self.assertEqual(buy["amount_raw"], "2000000000")

    def test_airdrop_without_base_payment_is_not_buy(self):
        r = receipt(); r["logs"] = r["logs"][1:]
        self.assertIsNone(self.verify(r))

    def test_transfer_without_swap_is_not_buy(self):
        r = receipt(); r["logs"] = r["logs"][:2]
        self.assertIsNone(self.verify(r))

    def test_unrelated_payment_is_not_buy(self):
        r = receipt(); r["logs"][0] = transfer(USDG, WALLET, OTHER, 10_000_000, 0)
        self.assertIsNone(self.verify(r))

    def test_reverted_tx_is_not_buy(self):
        r = receipt(); r["status"] = "0x0"
        self.assertIsNone(self.verify(r))

    def test_other_transaction_sender_not_attributed(self):
        self.assertIsNone(self.verify(tx={"from": OTHER, "hash": TX_SWAP}))

    def test_balance_inflating_airdrop_in_same_tx_rejected(self):
        r = receipt(); r["logs"].append(transfer(TOKEN, OTHER, WALLET, 100, 3))
        self.assertIsNone(self.verify(r))


class StoreIdentityTests(unittest.TestCase):
    def test_identity_survives_restart_and_backup_but_differs_for_new_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            store = EvmStore(path)
            identity = store.identity
            self.assertEqual(str(uuid.UUID(identity)), identity)
            store.close()
            store = EvmStore(path)
            self.assertEqual(store.identity, identity)
            backup_path = Path(directory) / "backup.sqlite3"
            destination = sqlite3.connect(backup_path)
            store.db.backup(destination)
            destination.close()
            store.close()
            backup = EvmStore(backup_path)
            fresh = EvmStore(Path(directory) / "new.sqlite3")
            try:
                self.assertEqual(backup.identity, identity)
                self.assertNotEqual(fresh.identity, identity)
            finally:
                backup.close()
                fresh.close()


class CollectionTests(unittest.TestCase):
    def test_budget_preserves_completed_chunk_and_retries_uncommitted_range(self):
        chain, tick, expire = FakeChain(), [0.0], [True]
        chain.head = 115
        def transport(payload):
            result = chain(payload)
            if (expire[0] and payload["method"] == "eth_getBlockByNumber"
                    and payload["params"][0] == hex(109)):
                tick[0], expire[0] = 61.0, False
            return result
        c = collector(chain)
        self.addCleanup(c.store.close)
        c.rpc.transport = transport
        c.max_blocks_per_request = 5
        with patch("sniper.adapters.evm.time.monotonic", side_effect=lambda: tick[0]):
            partial = c.poll(start_block=100, max_blocks=10, max_seconds=60)
            self.assertEqual(partial["status"], "budget_exhausted")
            self.assertEqual(partial["cursor"]["block_number"], 104)
            self.assertEqual(len(partial["verified_buys"]), 1)
            before = c.store.events(CHAIN_ID)
            resumed = c.poll(max_blocks=5, max_seconds=60)
            self.assertEqual(resumed["from_block"], 105)
            self.assertEqual(resumed["cursor"]["block_number"], 109)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(c.store.events(CHAIN_ID), before)

    def test_collect_persist_restart_and_rescan_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "collector.sqlite3"
            chain = FakeChain(); store = EvmStore(path)
            c = collector(chain, store)
            result = c.scan(100, 105)
            self.assertEqual(result["inserted_events"], 3)
            self.assertEqual(len(result["verified_buys"]), 1)
            buy = result["verified_buys"][0]
            self.assertEqual(buy["spent_usd"], "10.000000")
            self.assertEqual(buy["amount"], "2.000000000")
            self.assertTrue(buy["qualified"])
            self.assertFalse(buy["independence_verified_by_collector"])
            self.assertEqual(buy["chain_timestamp"], NOW - 48)
            store.close()
            store = EvmStore(path); c = collector(chain, store)
            self.assertEqual(c.scan(100, 105)["inserted_events"], 0)
            self.assertEqual(len(store.events(CHAIN_ID)), 3)
            self.assertEqual(store.cursor(CHAIN_ID)["block_number"], 105)
            store.close()

    def test_rpc_receipt_failure_does_not_advance_cursor(self):
        chain = FakeChain(); chain.fail_receipt = True
        c = collector(chain)
        with self.assertRaises(RpcError):
            c.scan(100, 105)
        self.assertIsNone(c.store.cursor(CHAIN_ID))
        self.assertEqual(c.store.events(CHAIN_ID), [])

    def test_renounced_owner_does_not_claim_safety(self):
        result = collector(FakeChain()).contract_safety(TOKEN, 105)
        self.assertTrue(result["checks"]["contract_present"])
        self.assertTrue(result["details"]["owner_renounced"])
        self.assertIsNone(result["checks"]["mint_disabled"])
        self.assertIsNone(result["checks"]["permissions_verified"])

    def test_missing_wallet_evidence_not_qualified(self):
        c = collector(FakeChain())
        c.watchlist[WALLET].pop("qualification_evidence")
        self.assertFalse(c.scan(100, 105)["verified_buys"][0]["qualified"])

    def test_stale_quote_does_not_invent_usd_spend(self):
        c = collector(FakeChain()); c.quote_usd[USDG]["observed_at"] = NOW - 31
        self.assertIsNone(c.scan(100, 105)["verified_buys"][0]["spent_usd"])

    def test_invalid_nested_price_is_unavailable_not_an_uncaught_source_crash(self):
        c = collector(FakeChain()); c.quote_usd[USDG] = "invalid-observation"
        result = c.scan(100, 105)
        self.assertEqual(result["verified_buys"], [])
        self.assertTrue(any("base-price observation" in e["reason"] for e in result["errors"]))

    def test_reorg_invalidates_old_records_and_replays(self):
        chain = FakeChain(); c = collector(chain)
        c.scan(100, 105)
        chain.fork_from = 101
        result = c.poll(max_blocks=6)
        self.assertEqual(result["reorg"]["rewind_to"], 99)
        self.assertEqual(result["reorg"]["invalidated_events"], 3)
        self.assertEqual(len(c.store.events(CHAIN_ID, canonical=False)), 3)
        self.assertEqual(len(c.store.events(CHAIN_ID)), 3)
        self.assertEqual(c.store.cursor(CHAIN_ID)["block_hash"], bh(105, True))

    def test_unknown_deep_reorg_halts(self):
        chain = FakeChain(); c = collector(chain); c.scan(100, 105)
        chain.fork_from = 0
        with self.assertRaises(ReorgError):
            c.poll(max_blocks=6)

    def test_reorg_during_chunk_never_commits(self):
        chain = FakeChain(); chain.change_end_on_repeat = True
        c = collector(chain)
        with self.assertRaises(ReorgError):
            c.scan(100, 105)
        self.assertIsNone(c.store.cursor(CHAIN_ID))

    def test_no_cursor_gaps(self):
        c = collector(FakeChain()); c.scan(100, 105)
        with self.assertRaisesRegex(ValueError, "skip"):
            c.scan(110, 115)

    def test_future_confirmation_boundary_rejected(self):
        with self.assertRaisesRegex(ValueError, "confirmation"):
            collector(FakeChain()).scan(145, 150)


if __name__ == "__main__":
    unittest.main()
