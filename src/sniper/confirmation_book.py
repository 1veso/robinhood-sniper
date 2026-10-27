"""Persistent, canonical, pool-scoped observations for a distinct-buyer quorum."""
import json
from decimal import Decimal

from .adapters.evm import CHAIN_ID, ReorgError, address, hash32, quantity
from .models import WalletConfirmation


class ConfirmationBook:
    def __init__(self, store):
        self.store = store
        store.db.execute('''CREATE TABLE IF NOT EXISTS paper_confirmations (
            event_id TEXT PRIMARY KEY, token TEXT NOT NULL, pool TEXT NOT NULL,
            observed_at INTEGER NOT NULL, tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
            block_hash TEXT NOT NULL, payload TEXT NOT NULL)''')
        store.db.execute('CREATE INDEX IF NOT EXISTS paper_confirmations_recent ON paper_confirmations(token,pool,observed_at)')

    def watched_pools(self, now, age, limit=8):
        return [r['pool'] for r in self.store.db.execute('''SELECT p.pool,MAX(p.observed_at) AS latest
            FROM paper_confirmations p JOIN evm_events e ON e.chain_id=? AND e.tx_hash=p.tx_hash
            AND e.log_index=p.log_index AND e.block_hash=p.block_hash AND e.kind='sampled_swap' AND e.canonical=1
            WHERE p.observed_at BETWEEN ? AND ? GROUP BY p.pool ORDER BY latest DESC,p.pool LIMIT ?''',
            (CHAIN_ID, now-age, now, limit))]

    def record(self, event_id, proof):
        if proof.get('verified_swap') is not True or proof.get('factory_verified') is not True:
            raise ValueError('Quorum requires a verified funded acquisition in the configured factory')
        # The proof must refer to an event in this account's canonical journal.
        existing = self.store.db.execute("SELECT 1 FROM evm_events WHERE chain_id=? AND tx_hash=? AND log_index=? AND block_hash=? AND kind='sampled_swap' AND canonical=1",
            (CHAIN_ID, proof['tx_hash'], proof['log_index'], proof['block_hash'])).fetchone()
        if existing is None:
            raise ValueError('Confirmation has no canonical source event')
        self.store.db.execute('INSERT OR IGNORE INTO paper_confirmations VALUES(?,?,?,?,?,?,?,?)',
            (event_id, address(proof['token_address']), address(proof['pool']), proof['observed_at'],
             hash32(proof['tx_hash']), proof['log_index'], hash32(proof['block_hash']), json.dumps(proof)))

    def recent(self, token, pool, *, now, age, minimum_spend):
        rows = self.store.db.execute('''SELECT p.payload FROM paper_confirmations p
            JOIN evm_events e ON e.chain_id=? AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index
             AND e.block_hash=p.block_hash AND e.kind='sampled_swap' AND e.canonical=1
            WHERE p.token=? AND p.pool=? AND p.observed_at BETWEEN ? AND ?
            ORDER BY p.observed_at DESC,p.event_id''', (CHAIN_ID, address(token), address(pool), now-age, now)).fetchall()
        choices = {}
        for row in rows:
            proof = json.loads(row['payload'])
            wallet, tx = address(proof['wallet']), hash32(proof['tx_hash'])
            if Decimal(proof['spent_usd']) < minimum_spend:
                continue
            choices.setdefault(wallet, {})[tx] = proof
        # Match wallets to distinct transactions. Greedy deduplication can miss
        # a valid quorum when A buys in tx1 and tx2, while B also buys in tx2.
        owners, selected = {}, {}
        def assign(wallet, seen):
            for tx, proof in choices[wallet].items():
                if tx in seen:
                    continue
                seen.add(tx)
                owner = owners.get(tx)
                if owner is None or assign(owner, seen):
                    owners[tx], selected[wallet] = wallet, proof
                    return True
            return False
        for wallet in choices:
            assign(wallet, set())
        return sorted(selected.values(), key=lambda p: (-p['observed_at'], p['wallet']))

    @staticmethod
    def confirm(proofs, rpc):
        # Recheck every counted block immediately before the decision, including
        # observations recovered after a process restart.
        for number, expected in {(p['block_number'], p['block_hash']) for p in proofs}:
            header = rpc.call('eth_getBlockByNumber', [hex(number), False])
            if quantity(header['number']) != number or hash32(header['hash']) != expected:
                raise ReorgError('A counted wallet acquisition is no longer canonical')
        return [WalletConfirmation(p['wallet'], 'unreviewed:'+p['wallet'].lower(), p['tx_hash'],
            p['observed_at'], Decimal(p['spent_usd']), True, False) for p in proofs]
