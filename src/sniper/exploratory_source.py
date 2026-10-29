"""Bounded, explicitly sampled live V3 signals. No wallet skill is inferred."""
import hashlib
import json
import sqlite3
import time
from decimal import Decimal

from .adapters.evm import (CHAIN_ID, FACTORY, WETH, USDG, SWAP_TOPIC, EvmCollector,
    RpcError, HistoricalStateUnavailable, ReorgError, address, quantity, decode_swap, verify_purchase, token_units,
    _words, _address_word, hash32, decode_transfer, TRANSFER_TOPIC, ZERO_ADDRESS)

COLLECTION_REVISION = 'scheduled-bounded-pool-followup-v2'
EXPIRY_CHECKPOINT = 'exploration_expiry_checkpoint_v1'


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
        store.db.execute('CREATE INDEX IF NOT EXISTS exploration_event_age ON evm_events(chain_id,kind,canonical,observed_at)')
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS exploration_intervals (
                interval_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                from_block INTEGER NOT NULL, through_block INTEGER NOT NULL,
                anchor_hash TEXT NOT NULL, anchor_at INTEGER NOT NULL,
                collected_at INTEGER NOT NULL, revision TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS exploration_interval_block ON exploration_intervals(from_block,through_block);
            CREATE TABLE IF NOT EXISTS exploration_followups (
                interval_id TEXT NOT NULL, pool TEXT NOT NULL, next_block INTEGER NOT NULL,
                selected TEXT NOT NULL, state TEXT NOT NULL, updated_at INTEGER NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(interval_id,pool));
        ''')
        store.db.create_function('exploration_signal_id', 3, lambda tx, index, block_hash:
            signal_id({'tx_hash': tx, 'log_index': index, 'block_hash': block_hash}), deterministic=True)

    def pending(self):
        # Oldest still-fresh selections get their turn before a newer sample.
        # This only returns journaled, canonical observations, never raw follow-up logs.
        now = int(self.clock())
        rows = self.store.db.execute('''SELECT e.payload FROM evm_events e
            WHERE e.chain_id=? AND e.kind='sampled_swap' AND e.canonical=1
              AND e.observed_at BETWEEN ? AND ? AND NOT EXISTS
              (SELECT 1 FROM exploration_outcomes o WHERE o.event_id=
               exploration_signal_id(e.tx_hash,e.log_index,e.block_hash))
            ORDER BY e.observed_at,e.block_number,e.log_index,e.tx_hash''', (CHAIN_ID, now - 900, now)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def expire_pending(self, limit=500, resolve_outcome=None, budget_seconds=1, monotonic=time.monotonic):
        """Reconcile committed decisions, then audit genuinely unevaluated expiry.

        The optional read-only resolver bridges a crash after the ledger commits
        but before source.finish(). Its recorded verdict/reason remain authoritative;
        expiry must not relabel such an evaluated event as UNEVALUATED. The return
        count includes only newly recorded signal_expired outcomes. Scan at most
        ``limit`` journal rows by rowid, not the historical anti-join. Outcomes
        and cursor commit together. Stop before a fresh row so it is revisited
        later, including late-inserted follow-up observations behind that row.
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Expiry batch must be between one and 500')
        if resolve_outcome is not None and not callable(resolve_outcome):
            raise ValueError('Outcome resolver must be callable')
        if isinstance(budget_seconds, bool) or not 0 < budget_seconds <= 1:
            raise ValueError('Expiry budget must be positive and at most one second')
        now = int(self.clock())
        deadline = monotonic() + budget_seconds
        db = self.store.db
        busy_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
        stats = self.expiry_progress = {'scanned': 0, 'expired': 0, 'recovered': 0,
            'row_limit': limit, 'budget_seconds': budget_seconds, 'checkpoint': None,
            'status': 'COMPLETE_BATCH', 'historical_backlog_complete': False}
        expired = 0
        db.execute('PRAGMA busy_timeout=0')
        db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
        try:
            with self.store.transaction():
                generation = db.execute('SELECT COALESCE(MAX(id),0) FROM evm_reorgs WHERE chain_id=?', (CHAIN_ID,)).fetchone()[0]
                row = db.execute('SELECT value FROM evm_metadata WHERE key=?', (EXPIRY_CHECKPOINT,)).fetchone()
                saved = json.loads(row[0]) if row else None
                cursor = 0
                if saved is not None:
                    if saved.get('source_id') != self.store.identity or type(saved.get('rowid')) is not int or saved['rowid'] < 0:
                        raise ValueError('Invalid expiry checkpoint binding')
                    if saved.get('reorg_id') == generation:
                        cursor = saved['rowid']
                rows = db.execute('''SELECT rowid AS journal_rowid,chain_id,kind,canonical,
                    tx_hash,log_index,block_hash,observed_at FROM evm_events
                    WHERE rowid>? ORDER BY rowid LIMIT ?''', (cursor, limit)).fetchall()
                # Remaining statements are indexed point reads/writes. Check the
                # clock between rows, allowing the completed prefix to commit.
                db.set_progress_handler(None, 0)
                for swap in rows:
                    if monotonic() >= deadline:
                        stats['status'] = 'BUDGET_DEFERRED'
                        break
                    selected = swap['chain_id'] == CHAIN_ID and swap['kind'] == 'sampled_swap' and swap['canonical']
                    if selected and swap['observed_at'] >= now - 900:
                        stats['status'] = 'WAITING_FOR_EXPIRY'
                        break  # Never skip a fresh row that will need an expiry audit later.
                    if selected and db.execute('SELECT 1 FROM exploration_outcomes WHERE event_id=?', (signal_id(swap),)).fetchone() is None:
                        recorded = resolve_outcome(signal_id(swap)) if resolve_outcome is not None else None
                        if recorded is not None:
                            if (not isinstance(recorded, dict) or not isinstance(recorded.get('verdict'), str)
                                    or not recorded['verdict'] or not isinstance(recorded.get('reason'), str) or not recorded['reason']):
                                raise ValueError('Invalid recorded source outcome')
                            self.finish(swap, {**recorded, 'observed_at': swap['observed_at'],
                                'recovered_at': now, 'outcome_source': 'existing_ledger_decision'})
                            stats['recovered'] += 1
                        else:
                            self.finish(swap, {'verdict': 'UNEVALUATED', 'reason': 'signal_expired',
                                'observed_at': swap['observed_at'], 'expired_at': now, 'freshness_seconds': 900})
                            expired += 1
                    cursor = swap['journal_rowid']
                    stats['scanned'] += 1
                # The loop is bounded; let its already-completed work commit even
                # when its time allowance elapsed. SQLite busy waits stay disabled.
                db.set_progress_handler(None, 0)
                db.execute('INSERT OR REPLACE INTO evm_metadata(key,value) VALUES(?,?)',
                    (EXPIRY_CHECKPOINT, json.dumps({'source_id': self.store.identity,
                     'reorg_id': generation, 'rowid': cursor}, sort_keys=True)))
                stats['checkpoint'] = cursor
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            expired = 0
            stats.update(scanned=0, recovered=0, checkpoint=None, status='DATABASE_DEFERRED')
        finally:
            db.set_progress_handler(None, 0)
            db.execute(f'PRAGMA busy_timeout={int(busy_timeout)}')
        stats['expired'] = expired
        return expired

    def pending_summary(self, budget_seconds=2, monotonic=time.monotonic, include_history=True):
        """Read diagnostic totals within a cooperative two-second wall budget.

        Fresh work is counted first through its age index. Historical totals may
        be unavailable under host load; diagnostic scans must not delay exits.
        """
        if isinstance(budget_seconds, bool) or not 0 < budget_seconds <= 2:
            raise ValueError('Summary budget must be positive and at most two seconds')
        now = int(self.clock())
        result = dict.fromkeys(('fresh', 'expired_unrecorded', 'oldest_observed_at',
                               'expired_recorded', 'recovered_ledger_outcomes', 'followup_pending'))
        result.update(summary_complete=False, summary_unavailable_reason=None)
        deadline = monotonic() + budget_seconds
        expired = False
        def interrupted():
            nonlocal expired
            expired = expired or monotonic() >= deadline
            return int(expired)
        def read(sql, params=()):
            if interrupted():
                raise TimeoutError('Summary time budget exceeded')
            return self.store.db.execute(sql, params).fetchone()
        busy_timeout = self.store.db.execute('PRAGMA busy_timeout').fetchone()[0]
        self.store.db.execute('PRAGMA busy_timeout=0')
        self.store.db.set_progress_handler(interrupted, 1000)
        try:
            fresh = read('''SELECT COUNT(*) AS count,MIN(e.observed_at) AS oldest
                FROM evm_events e WHERE e.chain_id=? AND e.kind='sampled_swap' AND e.canonical=1
                AND e.observed_at BETWEEN ? AND ? AND NOT EXISTS
                (SELECT 1 FROM exploration_outcomes o WHERE o.event_id=
                 exploration_signal_id(e.tx_hash,e.log_index,e.block_hash))''', (CHAIN_ID, now - 900, now))
            result['fresh'] = fresh['count']
            result['followup_pending'] = read("SELECT COUNT(*) FROM exploration_followups WHERE state='PENDING'")[0]
            if not include_history:
                result['summary_unavailable_reason'] = 'historical totals omitted from trading cycle; available in journal audit'
                return result
            outcomes = read('''SELECT
                SUM(CASE WHEN json_extract(payload,'$.reason')='signal_expired' THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN json_extract(payload,'$.outcome_source')='existing_ledger_decision' THEN 1 ELSE 0 END) AS recovered
                FROM exploration_outcomes''')
            result.update(expired_recorded=outcomes['expired'] or 0, recovered_ledger_outcomes=outcomes['recovered'] or 0)
            historical = read('''SELECT COUNT(*) AS count,MIN(e.observed_at) AS oldest
                FROM evm_events e WHERE e.chain_id=? AND e.kind='sampled_swap' AND e.canonical=1 AND e.observed_at<?
                AND NOT EXISTS (SELECT 1 FROM exploration_outcomes o WHERE o.event_id=
                  exploration_signal_id(e.tx_hash,e.log_index,e.block_hash))''', (CHAIN_ID, now - 900))
            result['expired_unrecorded'] = historical['count']
            known_times = [value for value in (fresh['oldest'], historical['oldest']) if value is not None]
            result['oldest_observed_at'] = min(known_times) if known_times else None
            result['summary_complete'] = True
        except (TimeoutError, sqlite3.Error):
            result['summary_unavailable_reason'] = 'summary time budget exceeded' if expired else 'summary read unavailable'
        finally:
            self.store.db.set_progress_handler(None, 0)
            self.store.db.execute(f'PRAGMA busy_timeout={int(busy_timeout)}')
        return result

    def finish(self, swap, result):
        self.store.db.execute('INSERT OR IGNORE INTO exploration_outcomes VALUES(?,?)', (signal_id(swap), json.dumps(result, sort_keys=True)))

    def collect(self, sample_size=32, watch_pools=()):
        if type(sample_size) is not int or not 1 <= sample_size <= 32:
            raise ValueError('Global sample must contain between one and 32 transactions')
        self.rpc.verify_chain()
        reorg = self.collector._recover_reorg()
        cursor = self.store.cursor(CHAIN_ID)
        tip = quantity(self.rpc.call('eth_blockNumber')) - 5
        if cursor and tip <= cursor['block_number']:
            return {'sampled': 0, 'reorg': reorg, 'collection_revision': COLLECTION_REVISION}
        # At ~10 blocks/second a two-minute worker advances ~1200 blocks.
        # A 1000-block window dropped observations even during normal operation.
        # Cover up to five minutes of cadence/startup delay; retain the same
        # bounded transaction sample and disclose longer downtime gaps.
        start = max(0, tip - 2999, cursor['block_number'] + 1 if cursor else 0)
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
        watched = list(dict.fromkeys(address(p) for p in watch_pools))[:8]
        for pool in watched:
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
        interval_id = hashlib.sha256(f'{self.store.identity}:{start}:{tip}:{anchor["hash"]}'.encode()).hexdigest()
        def committed(_inserted):
            now = int(self.clock())
            self.store.db.execute('INSERT OR IGNORE INTO exploration_intervals VALUES(?,?,?,?,?,?,?,?)',
                (interval_id, self.store.identity, start, tip, anchor['hash'], anchor['timestamp'], now, COLLECTION_REVISION))
            for pool in watched:
                self.store.db.execute('INSERT OR IGNORE INTO exploration_followups VALUES(?,?,?,?,?,?,?,?)',
                    (interval_id, pool, tip + 1, '[]', 'COMPLETE', now, 0, 0))
        self.store.commit_chunk(CHAIN_ID, selected, list(headers.values()), anchor, expected_cursor=cursor, on_commit=committed)
        return {'from_block': start, 'through_block': tip, 'logs_seen': len(logs), 'sampled': len(selected),
                'sampled_transactions': len({s['tx_hash'] for s in selected}), 'global_sampled_transactions': len(seen),
                'focused_pool_signals': focused,
                'blocks_scanned': tip - start + 1, 'max_scan_blocks': 3000,
                'coverage': 'bounded hash sample plus bounded same-interval candidate-pool follow-up; not all chain activity',
                'collection_revision': COLLECTION_REVISION, 'interval_id': interval_id, 'reorg': reorg,
                'skipped_blocks': max(0, start - cursor['block_number'] - 1) if cursor else 0}

    def followup(self, pool, sampled=None):
        """Recover bounded legs for a newly proven pool in its original interval.

        The discovery must already have canonical funded-buy evidence in the
        confirmation book. This method only schedules raw observations; prove()
        and ConfirmationBook.confirm remain mandatory before they form a quorum.
        """
        pool, now = address(pool), int(self.clock())
        if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_confirmations'").fetchone() is None:
            return {'state': 'NO_VERIFIED_BUYER', 'pool': pool, 'inserted': 0}
        params = [CHAIN_ID, pool, now - 900, now]
        selected_proof = ''
        if sampled is not None:
            selected_proof = ' AND p.tx_hash=? AND p.log_index=? AND p.block_hash=?'
            params.extend((sampled['tx_hash'], sampled['log_index'], sampled['block_hash']))
        verified = self.store.db.execute('''SELECT p.payload FROM paper_confirmations p
            JOIN evm_events e ON e.chain_id=? AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index
             AND e.block_hash=p.block_hash AND e.kind='sampled_swap' AND e.canonical=1
            WHERE p.pool=? AND p.observed_at BETWEEN ? AND ?''' + selected_proof +
            ' ORDER BY p.observed_at DESC,p.event_id LIMIT 1', params).fetchone()
        if verified is None:
            return {'state': 'NO_VERIFIED_BUYER', 'pool': pool, 'inserted': 0}
        proof = json.loads(verified['payload'])
        if proof.get('verified_swap') is not True or proof.get('factory_verified') is not True:
            raise ValueError('Follow-up requires verified funded acquisition evidence')
        number = proof['block_number']
        rows = self.store.db.execute('''SELECT i.* FROM exploration_intervals i
                JOIN evm_checkpoints c ON c.chain_id=? AND c.block_number=i.through_block
                 AND c.block_hash=i.anchor_hash AND c.canonical=1
                WHERE i.source_id=? AND i.from_block<=? AND i.through_block>=? AND i.anchor_at>=?
                ORDER BY i.collected_at DESC,i.through_block DESC LIMIT 1''',
                (CHAIN_ID, self.store.identity, number, number, now - 900)).fetchall()
        if not rows:
            return {'state': 'NO_FRESH_SAVED_INTERVAL', 'pool': pool, 'inserted': 0}
        interval = dict(rows[0])
        with self.store.transaction():
            present = self.store.db.execute('SELECT state FROM exploration_followups WHERE interval_id=? AND pool=?',
                (interval['interval_id'], pool)).fetchone()
            if present is None:
                count = self.store.db.execute('SELECT COUNT(*) FROM exploration_followups WHERE interval_id=?', (interval['interval_id'],)).fetchone()[0]
                if count >= 8:
                    return {'state': 'POOL_BOUND_REACHED', 'pool': pool, 'interval_id': interval['interval_id'], 'inserted': 0}
                self.store.db.execute('INSERT INTO exploration_followups VALUES(?,?,?,?,?,?,?,?)',
                    (interval['interval_id'], pool, interval['from_block'], '[]', 'PENDING', now, 0, 0))
        return self._followup(interval, pool)

    def resume_followups(self):
        rows = self.store.db.execute('''SELECT i.*,f.pool FROM exploration_followups f
            JOIN exploration_intervals i ON i.interval_id=f.interval_id
            WHERE f.state='PENDING' ORDER BY i.anchor_at,i.interval_id,f.pool LIMIT 8''').fetchall()
        return {'jobs': [self._followup(dict(row), row['pool']) for row in rows],
                'collection_revision': COLLECTION_REVISION}

    def _followup(self, interval, pool):
        job = self.store.db.execute('SELECT * FROM exploration_followups WHERE interval_id=? AND pool=?',
                                   (interval['interval_id'], pool)).fetchone()
        result = {'pool': pool, 'interval_id': interval['interval_id'], 'state': job['state'], 'inserted': job['inserted']}
        if job['state'] != 'PENDING':
            return result
        now = int(self.clock())
        if interval['anchor_at'] < now - 900:
            self.store.db.execute("UPDATE exploration_followups SET state='EXPIRED',updated_at=? WHERE interval_id=? AND pool=?",
                                  (now, interval['interval_id'], pool))
            return {**result, 'state': 'EXPIRED'}
        # Capture the journal generation before any provider work, not after it.
        # A competing collector can move/rewind while the raw chunk selection is
        # being gathered. Its progress must never authorize these older reads.
        with self.store.transaction():
            cursor = self.store.cursor(CHAIN_ID)
            generation = self.store.db.execute('SELECT COALESCE(MAX(id),0) FROM evm_reorgs WHERE chain_id=?', (CHAIN_ID,)).fetchone()[0]
            checkpoint = self.store.db.execute('SELECT 1 FROM evm_checkpoints WHERE chain_id=? AND block_number=? AND block_hash=? AND canonical=1',
                (CHAIN_ID, interval['through_block'], interval['anchor_hash'])).fetchone()
            if interval['source_id'] != self.store.identity:
                raise ReorgError('Saved follow-up interval is no longer canonical')
            invalidated = checkpoint is None or cursor is None or cursor['block_number'] < interval['through_block']
            if invalidated:
                self.store.db.execute("UPDATE exploration_followups SET state='INVALIDATED',updated_at=? WHERE interval_id=? AND pool=?",
                                      (now, interval['interval_id'], pool))
        if invalidated:
            # Surface the reorg once, preserve the raw progress for audit, then
            # let later resumes skip this definitively invalidated interval.
            raise ReorgError('Saved follow-up checkpoint was invalidated')
        anchor = self.collector.block(interval['through_block'])
        if anchor['hash'] != interval['anchor_hash']:
            raise ReorgError('Saved follow-up interval is no longer canonical')
        selected = json.loads(job['selected'])
        for first in range(job['next_block'], interval['through_block'] + 1, 500):
            last = min(first + 499, interval['through_block'])
            raw = self.rpc.call('eth_getLogs', [{'address': pool, 'topics': [SWAP_TOPIC], 'fromBlock': hex(first), 'toBlock': hex(last)}])
            if not isinstance(raw, list) or len(raw) > 20000:
                raise RpcError('Invalid or oversized pool follow-up response')
            swaps = [decode_swap(value) for value in raw]
            if any(s['address'] != pool or not first <= s['block_number'] <= last for s in swaps):
                raise RpcError('Pool follow-up returned an event outside its requested scope')
            choices = {signal_id(s): s for s in selected + swaps}
            selected = [choices[k] for k in sorted(choices)[:16]]
            # This bounded raw selection is not canonical admission. Its cursor
            # survives interruption; final block proofs happen before insertion.
            self.store.db.execute('''UPDATE exploration_followups SET next_block=?,selected=?,updated_at=?,requests=requests+1
                WHERE interval_id=? AND pool=? AND state='PENDING' ''',
                (last + 1, json.dumps(selected, sort_keys=True), int(self.clock()), interval['interval_id'], pool))
        headers = {}
        for swap in selected:
            number = swap['block_number']
            if number not in headers:
                headers[number] = self.collector.block(number)
            header = headers[number]
            if header['hash'] != swap['block_hash']:
                raise ReorgError('Follow-up event block changed')
            swap.update(kind='sampled_swap', observed_at=header['timestamp'])
        if self.collector.block(interval['through_block'])['hash'] != interval['anchor_hash']:
            raise ReorgError('Follow-up anchor changed')
        inserted = [0]
        def completed(count):
            checkpoint = self.store.db.execute('SELECT 1 FROM evm_checkpoints WHERE chain_id=? AND block_number=? AND block_hash=? AND canonical=1',
                (CHAIN_ID, interval['through_block'], interval['anchor_hash'])).fetchone()
            current_generation = self.store.db.execute('SELECT COALESCE(MAX(id),0) FROM evm_reorgs WHERE chain_id=?', (CHAIN_ID,)).fetchone()[0]
            if interval['source_id'] != self.store.identity or checkpoint is None or current_generation != generation:
                raise ReorgError('Follow-up journal generation changed during provider work')
            inserted[0] = count
            self.store.db.execute("UPDATE exploration_followups SET state='COMPLETE',updated_at=?,inserted=? WHERE interval_id=? AND pool=?",
                (int(self.clock()), count, interval['interval_id'], pool))
        # Use the existing atomic canonical journal writer at the unchanged tip.
        # Admission/deduplication is the same as global sampling; cursor never rewinds.
        self.store.commit_chunk(CHAIN_ID, selected, list(headers.values()),
            {'number': cursor['block_number'], 'hash': cursor['block_hash']}, expected_cursor=cursor, on_commit=completed)
        return {**result, 'state': 'COMPLETE', 'selected_legs': len(selected), 'inserted': inserted[0],
                'from_block': interval['from_block'], 'through_block': interval['through_block'],
                'collection_revision': COLLECTION_REVISION}

    def prove(self, sampled, fallback_rpc=None):
        swap = {**sampled, 'kind': 'swap'}
        swap.pop('observed_at', None)
        swap.pop('canonical', None)
        block = swap['block_number']
        if self.collector.block(block)['hash'] != swap['block_hash']:
            raise ReorgError('Signal is no longer canonical')
        def read(to, data):
            return ('eth_call', [{'to': to, 'data': data}, hex(block)])
        fallback_binding = None
        def fallback_block(number):
            raw = fallback_rpc.call('eth_getBlockByNumber', [hex(number), False])
            if not isinstance(raw, dict) or quantity(raw.get('number')) != number:
                raise RpcError('Historical fallback block unavailable or mismatched')
            return hash32(raw.get('hash'))
        def observe(calls):
            nonlocal fallback_binding
            try:
                return self.rpc.call_many(calls) if len(calls) > 1 else [self.rpc.call(*calls[0])]
            except HistoricalStateUnavailable:
                if fallback_rpc is None:
                    raise
                if any(method != 'eth_call' or params[1] != hex(block) for method, params in calls):
                    raise RpcError('Historical fallback refuses a different method or block')
                if fallback_binding is None:
                    self.rpc.verify_chain(CHAIN_ID)
                    fallback_rpc.verify_chain(CHAIN_ID)
                    genesis = self.collector.block(0)['hash']
                    if fallback_block(0) != genesis:
                        raise RpcError('Historical fallback genesis mismatch')
                    if self.collector.block(block)['hash'] != swap['block_hash'] or fallback_block(block) != swap['block_hash']:
                        raise ReorgError('Historical fallback signal block mismatch')
                    fallback_binding = {'chain_id': CHAIN_ID, 'genesis_hash': genesis,
                        'block_number': block, 'block_hash': swap['block_hash']}
                return fallback_rpc.call_many(calls) if len(calls) > 1 else [fallback_rpc.call(*calls[0])]
        pool = swap['address']
        raw = observe([read(pool, s) for s in ('0x0dfe1681', '0xd21220a7', '0xddca3f43')])
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
        found = observe([read(FACTORY, lookup)])[0]
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
        decimals = int(_words(observe([read(base, '0x313ce567')])[0], 1)[0], 16)
        base_price = self.market.quote(base, now=int(self.clock()))
        spent = token_units(int(proof['spent_raw']), decimals) * base_price.price_usd
        if not spent.is_finite() or spent <= 0:
            raise RpcError('Missing base valuation')
        if self.collector.block(block)['hash'] != swap['block_hash']:
            raise ReorgError('Signal anchor changed during proof')
        if fallback_binding is not None and fallback_block(block) != swap['block_hash']:
            raise ReorgError('Historical fallback signal anchor changed during proof')
        return {**proof, 'pool': pool, 'fee': fee, 'factory_verified': True, 'spent_usd': str(spent), 'observed_at': sampled['observed_at'],
                'block_number': block, 'block_hash': swap['block_hash'], 'tx_hash': swap['tx_hash'],
                'log_index': swap['log_index'], 'base_price': base_price.as_dict(),
                'historical_state_provider': 'quote_rpc' if fallback_binding else 'collection_rpc',
                'historical_state_binding': fallback_binding,
                'qualification': 'unestablished', 'independence': 'unestablished',
                'transaction': transaction, 'receipt': receipt}
