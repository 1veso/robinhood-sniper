"""Persistent canonical purchases with explicit pool or token quorum scope."""
import json
from decimal import Decimal

from .adapters.evm import CHAIN_ID, ReorgError, address, hash32, quantity
from .models import WalletConfirmation


class ConfirmationBook:
    def __init__(self, store, *, read_only=False):
        self.store = store
        if read_only:
            # Comparison consumers may only inspect an already initialized source.
            store.db.execute('SELECT event_id FROM paper_confirmations LIMIT 0')
            return
        store.db.execute('''CREATE TABLE IF NOT EXISTS paper_confirmations (
            event_id TEXT PRIMARY KEY, token TEXT NOT NULL, pool TEXT NOT NULL,
            observed_at INTEGER NOT NULL, tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
            block_hash TEXT NOT NULL, payload TEXT NOT NULL)''')
        store.db.execute('CREATE INDEX IF NOT EXISTS paper_confirmations_recent ON paper_confirmations(token,pool,observed_at)')
        store.db.execute('CREATE INDEX IF NOT EXISTS paper_confirmations_token_recent ON paper_confirmations(token,observed_at)')

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
        existing = self.store.db.execute("SELECT chain_id,block_number,observed_at,payload AS source_payload FROM evm_events WHERE chain_id=? AND tx_hash=? AND log_index=? AND block_hash=? AND kind='sampled_swap' AND canonical=1",
            (CHAIN_ID, proof['tx_hash'], proof['log_index'], proof['block_hash'])).fetchone()
        if existing is None:
            raise ValueError('Confirmation has no canonical source event')
        self._check_source_binding(proof, existing)
        self.store.db.execute('INSERT OR IGNORE INTO paper_confirmations VALUES(?,?,?,?,?,?,?,?)',
            (event_id, address(proof['token_address']), address(proof['pool']), proof['observed_at'],
             hash32(proof['tx_hash']), proof['log_index'], hash32(proof['block_hash']), json.dumps(proof)))

    def recent(self, token, pool, *, now, age, minimum_spend, observed_after=None):
        """Legacy pool-scoped selection; callers must opt into token scope."""
        rows = self.store.db.execute('''SELECT p.payload FROM paper_confirmations p
            JOIN evm_events e ON e.chain_id=? AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index
             AND e.block_hash=p.block_hash AND e.kind='sampled_swap' AND e.canonical=1
            WHERE p.token=? AND p.pool=? AND p.observed_at BETWEEN ? AND ?
            ORDER BY p.observed_at DESC,p.event_id''', (CHAIN_ID, address(token), address(pool), now-age, now)).fetchall()
        return self._select(rows, minimum_spend=minimum_spend, observed_after=observed_after)

    def recent_token(self, token, *, now, age, minimum_spend, observed_after=None,
                     eligible_wallets=None, chain_id=CHAIN_ID):
        """Select distinct buyers/transactions across verified pools of one token.

        This journal supports only the configured Robinhood chain. Each returned
        proof retains its own pool and original receipt/factory evidence; none of
        that pool's liquidity or route capacity transfers to another pool. Wallet
        eligibility belongs to the strategy; an explicit allowlist is applied
        before matching so an ineligible wallet cannot consume another's tx.
        """
        proofs = self.recent_token_proofs(token, now=now, age=age,
            minimum_spend=minimum_spend, observed_after=observed_after,
            eligible_wallets=eligible_wallets, chain_id=chain_id)
        return self._match(proofs)

    def recent_token_proofs(self, token, *, now, age, minimum_spend, observed_after=None,
                            eligible_wallets=None, chain_id=CHAIN_ID):
        """All bound purchases, before per-strategy wallet/transaction matching.

        Independent paths must filter their own eligibility and re-entry rules
        before choosing one proof per wallet. Otherwise a newer ineligible buy
        can hide an older eligible purchase by the same wallet.
        """
        if type(chain_id) is not int or chain_id != CHAIN_ID:
            raise ValueError('Unsupported confirmation chain')
        rows = self.store.db.execute('''SELECT p.payload,p.token,p.pool,
            p.tx_hash,p.log_index,p.block_hash,p.observed_at AS confirmation_at,
            e.chain_id,e.block_number,e.observed_at,e.payload AS source_payload
            FROM paper_confirmations p JOIN evm_events e ON e.chain_id=?
             AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index
             AND e.block_hash=p.block_hash AND e.kind='sampled_swap' AND e.canonical=1
            WHERE p.token=? AND p.observed_at BETWEEN ? AND ?
            ORDER BY p.observed_at DESC,p.event_id''',
            (chain_id, address(token), now-age, now)).fetchall()
        for row in rows:
            proof = json.loads(row['payload'])
            self._check_source_binding(proof, row)
            if (address(proof['token_address']) != row['token']
                    or address(proof['pool']) != row['pool']
                    or hash32(proof['tx_hash']) != row['tx_hash']
                    or proof['log_index'] != row['log_index']
                    or hash32(proof['block_hash']) != row['block_hash']
                    or proof['observed_at'] != row['confirmation_at']):
                raise ValueError('Confirmation payload does not match its recorded identity')
        return self._eligible_proofs(rows, minimum_spend=minimum_spend,
            observed_after=observed_after, eligible_wallets=eligible_wallets)

    @staticmethod
    def _check_source_binding(proof, row):
        source = json.loads(row['source_payload'])
        if (proof.get('verified_swap') is not True or proof.get('factory_verified') is not True
                or proof.get('chain_id', CHAIN_ID) != row['chain_id']
                or source.get('chain_id', CHAIN_ID) != row['chain_id']
                or proof['block_number'] != row['block_number']
                or proof['observed_at'] != row['observed_at']):
            raise ValueError('Confirmation proof does not match its canonical source')
        pools = [source[k] for k in ('address', 'pool') if k in source]
        if not pools or any(address(p) != address(proof['pool']) for p in pools):
            raise ValueError('Confirmation pool does not match its canonical source')
        # Raw V3 logs name the pool, not token0/token1. The token binding comes
        # from ExploratorySource.prove's factory reads and receipt verification.
        # When the journal also contains an explicit token, it must agree.
        if 'token_address' in source and address(source['token_address']) != address(proof['token_address']):
            raise ValueError('Confirmation token does not match its canonical source')

    @staticmethod
    def _select(rows, *, minimum_spend, observed_after=None, eligible_wallets=None):
        return ConfirmationBook._match(ConfirmationBook._eligible_proofs(rows,
            minimum_spend=minimum_spend, observed_after=observed_after, eligible_wallets=eligible_wallets))

    @staticmethod
    def _eligible_proofs(rows, *, minimum_spend, observed_after=None, eligible_wallets=None):
        eligible = None if eligible_wallets is None else {address(w) for w in eligible_wallets}
        proofs = []
        for row in rows:
            proof = json.loads(row['payload'])
            if observed_after is not None and proof['observed_at'] <= observed_after:
                continue
            wallet = address(proof['wallet'])
            if eligible is not None and wallet not in eligible:
                continue
            if Decimal(proof['spent_usd']) < minimum_spend:
                continue
            proofs.append(proof)
        return proofs

    @staticmethod
    def _match(proofs):
        choices = {}
        for proof in proofs:
            wallet, tx = address(proof['wallet']), hash32(proof['tx_hash'])
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
