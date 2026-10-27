"""Bounded, explicitly sampled live V3 signals. No wallet skill is inferred."""
import hashlib
import json
import time
from decimal import Decimal

from .adapters.evm import (CHAIN_ID, FACTORY, WETH, USDG, SWAP_TOPIC, EvmCollector,
    RpcError, ReorgError, address, quantity, decode_swap, verify_purchase, token_units,
    _words, _address_word, hash32, decode_transfer, TRANSFER_TOPIC, ZERO_ADDRESS)


def verify_recipient_acquisition(swap, pool, receipt, transaction):
    """Prove a funded pool buy and net receipt, NOT recipient ownership of funds.

    Smart-wallet routers often use a relayer as tx.from. This explicitly weaker
    evidence is only used by exploration. The strict direct-EOA verifier stays
    unchanged. Payer, transaction sender and recipient remain separate fields.
    """
    try:
        if quantity(receipt['status']) != 1 or hash32(transaction['hash']) != swap['tx_hash']:
            return None
        if hash32(receipt['transactionHash']) != swap['tx_hash'] or hash32(receipt['blockHash']) != swap['block_hash'] or quantity(receipt['blockNumber']) != swap['block_number']:
            return None
        t0, t1 = pool['token0'], pool['token1']
        if (t0 in (WETH, USDG)) == (t1 in (WETH, USDG)):
            return None
        base, token = (t0, t1) if t0 in (WETH, USDG) else (t1, t0)
        amount_in = int(swap['amount0_raw'] if base == t0 else swap['amount1_raw'])
        amount_out = -int(swap['amount1_raw'] if base == t0 else swap['amount0_raw'])
        wallet = address(swap['recipient'])
        if amount_in <= 0 or amount_out <= 0 or wallet in (ZERO_ADDRESS, pool['pool'], base, token):
            return None
        same_pool = [decode_swap(l) for l in receipt['logs'] if (l.get('topics') or [None])[0] == SWAP_TOPIC and address(l['address']) == pool['pool']]
        if len(same_pool) != 1 or any(same_pool[0][k] != swap[k] for k in same_pool[0]):
            return None
        incoming_base = outgoing_base = delivered = net_token = 0
        payers = set()
        for log in receipt['logs']:
            if not log.get('topics') or log['topics'][0] != TRANSFER_TOPIC or address(log['address']) not in (base, token):
                continue
            transfer = decode_transfer(log)
            if transfer['tx_hash'] != swap['tx_hash'] or transfer['block_hash'] != swap['block_hash'] or transfer['block_number'] != swap['block_number']:
                return None
            amount = int(transfer['amount_raw'])
            if transfer['address'] == base:
                if transfer['to'] == pool['pool']:
                    if transfer['from'] == ZERO_ADDRESS:
                        return None
                    incoming_base += amount
                    payers.add(transfer['from'])
                if transfer['from'] == pool['pool']:
                    outgoing_base += amount
            else:
                if transfer['to'] == wallet:
                    net_token += amount
                if transfer['from'] == wallet:
                    net_token -= amount
                if transfer['from'] == pool['pool']:
                    if transfer['to'] != wallet:
                        return None
                    delivered += amount
        if incoming_base != amount_in or outgoing_base or delivered != amount_out or net_token < amount_out:
            return None
        return {'wallet': wallet, 'transaction_sender': address(transaction['from']), 'base_payers': sorted(payers),
                'token_address': token, 'quote_address': base, 'amount_raw': str(amount_out),
                'recipient_net_token_raw': str(net_token), 'spent_raw': str(amount_in), 'verified_swap': True,
                'proof_scope': 'funded pool buy and net recipient acquisition; funding ownership unestablished',
                'spend_basis': 'pool base input, not proven personal wallet expenditure'}
    except (KeyError, TypeError, ValueError):
        return None


def signal_id(swap):
    return 'explore:' + hashlib.sha256((swap['tx_hash'] + ':' + str(swap['log_index']) + ':' + swap['block_hash']).encode()).hexdigest()


class ExploratorySource:
    def __init__(self, rpc, store, market, clock=time.time):
        self.rpc, self.store, self.market, self.clock = rpc, store, market, clock
        self.collector = EvmCollector(rpc, store=store, clock=clock)
        store.db.execute('CREATE TABLE IF NOT EXISTS exploration_outcomes (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def pending(self):
        # SQL limits memory growth during an unattended week.
        rows = self.store.db.execute("SELECT payload FROM evm_events WHERE chain_id=? AND kind='sampled_swap' AND canonical=1 AND observed_at>=? ORDER BY block_number DESC,log_index DESC", (CHAIN_ID, int(self.clock()) - 900)).fetchall()
        return [s for row in rows if not self.store.db.execute('SELECT 1 FROM exploration_outcomes WHERE event_id=?', (signal_id(s := json.loads(row['payload'])),)).fetchone()]

    def finish(self, swap, result):
        self.store.db.execute('INSERT OR IGNORE INTO exploration_outcomes VALUES(?,?)', (signal_id(swap), json.dumps(result, sort_keys=True)))

    def collect(self, sample_size=32, watch_pools=()):
        self.rpc.verify_chain()
        reorg = self.collector._recover_reorg()
        cursor = self.store.cursor(CHAIN_ID)
        tip = quantity(self.rpc.call('eth_blockNumber')) - 5
        if cursor and tip <= cursor['block_number']:
            return {'sampled': 0, 'reorg': reorg}
        # On recovery from downtime, record the skipped interval explicitly.
        start = max(0, tip - 999, cursor['block_number'] + 1 if cursor else 0)
        anchor = self.collector.block(tip)
        logs = []
        for first in range(start, tip + 1, 500):
            result = self.rpc.call('eth_getLogs', [{'topics': [SWAP_TOPIC], 'fromBlock': hex(first), 'toBlock': hex(min(tip, first + 499))}])
            if not isinstance(result, list) or len(result) > 20000:
                raise RpcError('Invalid or oversized swap response')
            logs.extend(result)
        swaps = [decode_swap(raw) for raw in logs]
        if any(not start <= s['block_number'] <= tip for s in swaps):
            raise RpcError('Swap outside requested interval')
        # Hash ordering avoids cherry-picking by eventual P&L. Select transactions,
        # then retain ALL their swap legs: a base/base routing leg must not hide
        # the actual token acquisition later in that same sampled transaction.
        selected, seen = [], set()
        for swap in sorted(swaps, key=signal_id):
            if swap['tx_hash'] not in seen:
                selected.append(swap)
                seen.add(swap['tx_hash'])
            if len(selected) >= sample_size:
                break
        selected = [swap for swap in swaps if swap['tx_hash'] in seen]
        # After finding one buyer, keep watching that candidate pool for the
        # second buyer. This bounded follow-up does not depend on future P&L.
        selected_keys = {signal_id(s) for s in selected}
        focused = 0
        for pool in list(dict.fromkeys(address(p) for p in watch_pools))[:8]:
            for swap in sorted((s for s in swaps if s['address'] == pool), key=signal_id)[:16]:
                key = signal_id(swap)
                if key not in selected_keys:
                    selected.append(swap)
                    selected_keys.add(key)
                    focused += 1
        headers = {tip: anchor}
        if self.rpc.batch_enabled:
            numbers = sorted({s['block_number'] for s in selected} - headers.keys())
            for offset in range(0, len(numbers), 10):
                group = numbers[offset:offset+10]
                raw_headers = self.rpc.call_many([('eth_getBlockByNumber', [hex(n), False]) for n in group])
                for number, raw in zip(group, raw_headers):
                    if quantity(raw['number']) != number:
                        raise RpcError('Mismatched batched block header')
                    headers[number] = {'number': number, 'hash': hash32(raw['hash']),
                        'parent_hash': hash32(raw['parentHash']), 'timestamp': quantity(raw['timestamp'])}
        for s in selected:
            if s['block_number'] not in headers:
                headers[s['block_number']] = self.collector.block(s['block_number'])
            header = headers[s['block_number']]
            if header['hash'] != s['block_hash']:
                raise ReorgError('Sampled event block changed')
            s.update(kind='sampled_swap', observed_at=header['timestamp'])
        if self.collector.block(tip)['hash'] != anchor['hash']:
            raise ReorgError('Collection anchor changed')
        self.store.commit_chunk(CHAIN_ID, selected, list(headers.values()), anchor, expected_cursor=cursor)
        return {'from_block': start, 'through_block': tip, 'logs_seen': len(logs), 'sampled': len(selected),
                'sampled_transactions': len({s['tx_hash'] for s in selected}), 'global_sampled_transactions': len(seen),
                'focused_pool_signals': focused,
                'coverage': 'bounded hash sample plus bounded candidate-pool follow-up; not all chain activity', 'reorg': reorg,
                'skipped_blocks': max(0, start - cursor['block_number'] - 1) if cursor else 0}

    def prove(self, sampled):
        swap = {**sampled, 'kind': 'swap'}
        swap.pop('observed_at', None)
        swap.pop('canonical', None)
        block = swap['block_number']
        if self.collector.block(block)['hash'] != swap['block_hash']:
            raise ReorgError('Signal is no longer canonical')
        def read(to, data):
            return ('eth_call', [{'to': to, 'data': data}, hex(block)])
        pool = swap['address']
        raw = self.rpc.call_many([read(pool, s) for s in ('0x0dfe1681', '0xd21220a7', '0xddca3f43')])
        t0, t1 = [_address_word(_words(r, 1)[0]) for r in raw[:2]]
        fee = int(_words(raw[2], 1)[0], 16)
        if (t0 in (WETH, USDG)) == (t1 in (WETH, USDG)):
            return None
        base, token = (t0, t1) if t0 in (WETH, USDG) else (t1, t0)
        if int(swap['amount0_raw'] if base == t0 else swap['amount1_raw']) <= 0:
            return None
        if not 0 < fee < 1000000:
            raise RpcError('Invalid pool fee')
        lookup = '0x1698ee82' + t0[2:].zfill(64) + t1[2:].zfill(64) + f'{fee:064x}'
        found = self.rpc.call(*read(FACTORY, lookup))
        if _address_word(_words(found, 1)[0]) != pool:
            return None
        transaction, receipt = self.rpc.call_many([('eth_getTransactionByHash', [swap['tx_hash']]), ('eth_getTransactionReceipt', [swap['tx_hash']])])
        if not isinstance(transaction, dict) or not isinstance(receipt, dict):
            raise RpcError('Transaction evidence unavailable')
        wallet = address(transaction.get('from'))
        proof = verify_purchase(swap, {'pool': pool, 'token0': t0, 'token1': t1}, receipt, transaction, wallet, {WETH: 'WETH', USDG: 'USDG'})
        if proof is not None:
            proof.update(proof_scope='direct wallet base payment and token receipt', spend_basis='verified direct wallet expenditure')
        else:
            proof = verify_recipient_acquisition(swap, {'pool': pool, 'token0': t0, 'token1': t1}, receipt, transaction)
        if proof is None:
            return None
        decimals = int(_words(self.rpc.call(*read(base, '0x313ce567')), 1)[0], 16)
        base_price = self.market.quote(base, now=int(self.clock()))
        spent = token_units(int(proof['spent_raw']), decimals) * base_price.price_usd
        if not spent.is_finite() or spent <= 0:
            raise RpcError('Missing base valuation')
        if self.collector.block(block)['hash'] != swap['block_hash']:
            raise ReorgError('Signal anchor changed during proof')
        return {**proof, 'pool': pool, 'fee': fee, 'factory_verified': True, 'spent_usd': str(spent), 'observed_at': sampled['observed_at'],
                'block_number': block, 'block_hash': swap['block_hash'], 'tx_hash': swap['tx_hash'],
                'log_index': swap['log_index'], 'base_price': base_price.as_dict(),
                'qualification': 'unestablished', 'independence': 'unestablished',
                'transaction': transaction, 'receipt': receipt}
