"""Explicit token quorum scope, with only isolated canonical-journal fixtures."""
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from sniper.adapters.evm import CHAIN_ID, ReorgError
from sniper.adapters.evm_store import EvmStore
from sniper.confirmation_book import ConfirmationBook


NOW = 1_700_000_000
TOKEN = '0x' + '11' * 20
OTHER_TOKEN = '0x' + '12' * 20
POOL = '0x' + '21' * 20
OTHER_POOL = '0x' + '22' * 20
WALLET = '0x' + '31' * 20
OTHER_WALLET = '0x' + '32' * 20
THIRD_WALLET = '0x' + '33' * 20


def block_hash(number):
    return '0x' + f'{number:064x}'


def purchase(index, *, wallet=WALLET, pool=POOL, token=TOKEN, at=NOW, tx=None, spend='100'):
    return {'wallet': wallet, 'tx_hash': tx or '0x' + f'{index:064x}',
        'log_index': index, 'block_number': 100 + index, 'block_hash': block_hash(100 + index),
        'token_address': token, 'pool': pool, 'observed_at': at, 'spent_usd': spend,
        'verified_swap': True, 'factory_verified': True, 'chain_id': CHAIN_ID,
        'receipt': {'fixture': index}, 'pool_evidence': {'pool': pool, 'fixture': index}}


class CrossPoolConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'source.sqlite3'
        self.store = EvmStore(self.path)
        self.book = ConfirmationBook(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def persist(self, proof, *, chain=CHAIN_ID, raw_log=False):
        event = {**proof, 'kind': 'sampled_swap'}
        if raw_log:
            event = {k: proof[k] for k in ('tx_hash', 'log_index', 'block_number', 'block_hash', 'observed_at')}
            event.update(kind='sampled_swap', address=proof['pool'])
        cursor = self.store.cursor(chain)
        number = max(proof['block_number'], cursor['block_number'] if cursor else 0)
        self.store.commit_chunk(chain, [event],
            [{'number': proof['block_number'], 'hash': proof['block_hash']}],
            {'number': number, 'hash': block_hash(number)}, expected_cursor=cursor)

    def add(self, index, *, raw_log=False, **kwargs):
        proof = purchase(index, **kwargs)
        self.persist(proof, raw_log=raw_log)
        self.book.record(str(index), proof)
        return proof

    def recent(self, **kwargs):
        return self.book.recent_token(TOKEN, now=NOW, age=900, minimum_spend=D(10), **kwargs)

    def test_two_wallets_same_pool_qualify(self):
        self.add(1)
        self.add(2, wallet=OTHER_WALLET)
        self.assertEqual(len(self.recent()), 2)

    def test_two_wallets_across_pools_retain_each_proof_without_widening_legacy_query(self):
        first = self.add(1, raw_log=True)
        second = self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL, raw_log=True)
        selected = self.recent()
        self.assertEqual({p['wallet']: p for p in selected}, {WALLET: first, OTHER_WALLET: second})
        self.assertEqual({p['pool'] for p in selected}, {POOL, OTHER_POOL})
        self.assertEqual(len(self.book.recent(TOKEN, POOL, now=NOW, age=900, minimum_spend=D(10))), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_confirmations').fetchone()[0], 2)

    def test_one_wallet_buying_through_two_pools_counts_once(self):
        self.add(1)
        self.add(2, pool=OTHER_POOL)
        selected = self.recent()
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['wallet'], WALLET)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_confirmations').fetchone()[0], 2)

    def test_all_purchase_api_preserves_alternatives_for_independent_path_filtering(self):
        older = self.add(1, at=NOW-10)
        newer = self.add(2, pool=OTHER_POOL)
        third = self.add(3, wallet=OTHER_WALLET, pool=OTHER_POOL, at=NOW-5)
        proofs = self.book.recent_token_proofs(TOKEN, now=NOW, age=900, minimum_spend=D(10))
        self.assertEqual(proofs, [newer, third, older])
        self.assertEqual(len(self.recent()), 2)
        fresh = self.book.recent_token_proofs(TOKEN, now=NOW, age=900,
            minimum_spend=D(10), observed_after=NOW-5, eligible_wallets={WALLET})
        self.assertEqual(fresh, [newer])

    def test_different_tokens_do_not_combine(self):
        self.add(1)
        self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL, token=OTHER_TOKEN)
        self.assertEqual(len(self.recent()), 1)

    def test_two_wallets_in_one_transaction_do_not_combine_across_pools(self):
        first = self.add(1)
        self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL, tx=first['tx_hash'])
        self.assertEqual(len(self.recent()), 1)

    def test_matching_across_pools_preserves_valid_distinct_transaction_assignment(self):
        self.add(1, at=NOW-1)
        second = self.add(2, pool=OTHER_POOL)
        self.add(3, wallet=OTHER_WALLET, pool=OTHER_POOL, tx=second['tx_hash'], at=NOW-1)
        selected = self.recent()
        self.assertEqual(len({p['wallet'] for p in selected}), 2)
        self.assertEqual(len({p['tx_hash'] for p in selected}), 2)

    def test_strategy_wallet_filter_is_applied_before_transaction_matching(self):
        first = self.add(1, at=NOW-2)
        second = self.add(2, at=NOW-1)
        self.add(3, wallet=OTHER_WALLET, pool=OTHER_POOL, tx=first['tx_hash'])
        self.add(4, wallet=THIRD_WALLET, pool=OTHER_POOL, tx=second['tx_hash'], at=NOW-1)
        selected = self.recent(eligible_wallets={WALLET.upper().replace('0X', '0x'), THIRD_WALLET})
        self.assertEqual({p['wallet'] for p in selected}, {WALLET, THIRD_WALLET})
        self.assertEqual(len({p['tx_hash'] for p in selected}), 2)
        self.assertEqual(self.recent(eligible_wallets=set()), [])

    def test_window_spend_and_strict_post_close_boundary_are_preserved(self):
        self.add(1, at=NOW-900)
        self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL, at=NOW-901)
        self.add(3, wallet=OTHER_WALLET, pool=OTHER_POOL, at=NOW+1)
        self.add(4, wallet=OTHER_WALLET, pool=OTHER_POOL, at=NOW-899, spend='9.99')
        self.assertEqual(len(self.recent()), 1)
        self.assertEqual(self.recent(observed_after=NOW-900), [])
        self.add(5, wallet=OTHER_WALLET, pool=OTHER_POOL, at=NOW-899, spend='10')
        self.assertEqual(len(self.recent()), 2)
        self.assertEqual([p['wallet'] for p in self.recent(observed_after=NOW-900)], [OTHER_WALLET])

    def test_restart_keeps_cross_pool_identity_and_reorg_removes_only_orphaned_purchase(self):
        first = self.add(1)
        self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL)
        identity = self.store.identity
        self.store.close()
        self.store = EvmStore(self.path)
        self.book = ConfirmationBook(self.store)
        self.assertEqual(self.store.identity, identity)
        self.assertEqual(len(self.recent()), 2)
        self.store.rewind(CHAIN_ID, {'number': 101, 'hash': block_hash(101)}, NOW)
        self.assertEqual(self.recent(), [first])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_confirmations').fetchone()[0], 2)

    def test_other_chain_cannot_supply_a_confirmation(self):
        proof = purchase(1)
        self.persist(proof, chain=CHAIN_ID+1)
        with self.assertRaisesRegex(ValueError, 'canonical source'):
            self.book.record('other-chain', proof)
        for chain in (CHAIN_ID+1, str(CHAIN_ID), True):
            with self.subTest(chain=chain), self.assertRaisesRegex(ValueError, 'confirmation chain'):
                self.recent(chain_id=chain)

    def test_record_rejects_contradictory_pool_token_chain_time_or_block_binding(self):
        proof = purchase(1)
        self.persist(proof)
        alterations = ({'pool': OTHER_POOL}, {'token_address': OTHER_TOKEN},
            {'chain_id': CHAIN_ID+1}, {'observed_at': NOW+1}, {'block_number': 999})
        for change in alterations:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'canonical source'):
                self.book.record('contradiction', {**proof, **change})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_confirmations').fetchone()[0], 0)

    def test_token_scope_revalidates_persisted_source_and_proof_identity(self):
        proof = self.add(1, raw_log=True)
        for change in ({'token_address': OTHER_TOKEN}, {'pool': OTHER_POOL},
                       {'factory_verified': False}, {'tx_hash': '0x'+'ff'*32}):
            self.store.db.execute('UPDATE paper_confirmations SET payload=? WHERE event_id=?',
                (json.dumps({**proof, **change}), '1'))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.recent()
        self.store.db.execute('UPDATE paper_confirmations SET payload=? WHERE event_id=?', (json.dumps(proof), '1'))
        self.assertEqual(self.recent(), [proof])

    def test_cross_pool_canonical_rpc_recheck_remains_mandatory(self):
        first = self.add(1)
        second = self.add(2, wallet=OTHER_WALLET, pool=OTHER_POOL)
        calls = []
        class RPC:
            def call(self, method, params):
                number = int(params[0], 16)
                calls.append(number)
                return {'number': hex(number), 'hash': block_hash(number)}
        confirmations = self.book.confirm(self.recent(), RPC())
        self.assertEqual(set(calls), {first['block_number'], second['block_number']})
        self.assertEqual(len(confirmations), 2)
        self.assertTrue(all(not c.qualified for c in confirmations))
        class Fork:
            def call(self, method, params):
                return {'number': params[0], 'hash': '0x'+'ff'*32}
        with self.assertRaises(ReorgError):
            self.book.confirm(self.recent(), Fork())


if __name__ == '__main__':
    unittest.main()
