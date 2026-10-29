"""Durable discovery provenance and public-address exports, never qualification.

The journal records why an address entered research. A successful swap sender,
a token recipient and a base-asset payer are separate roles, not independent
controllers. Source-journal canonical checks and historical RPC capture checks
are deliberately different evidence scopes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .adapters.evm import CHAIN_ID, address, hash32, quantity


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _atomic(path, content):
    path = Path(path)
    try:
        if path.read_bytes() == content.encode('utf-8'):
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        value = _json(value)
    if isinstance(value, str) and value.lstrip(' \t\r\n').startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def _write_address(job):
    """Only independent public view files; SQLite stays on the calling thread."""
    folder, summary, payload, evidence, fields = job
    _atomic(folder/summary['json_path'], _json(payload)+'\n')
    _csv(folder/summary['csv_path'], evidence, fields)


def _public_proof(proof):
    """Only public addresses and validated values; never raw provider metadata."""
    values = {}
    for key in ('amount_raw','spent_raw'):
        value = proof.get(key)
        if value is not None:
            if not isinstance(value,str) or not re.fullmatch(r'[0-9]+',value) or int(value)<=0:
                raise ValueError('Invalid positive raw token quantity')
            values[key] = str(int(value))
        else:
            values[key] = None
    value = proof.get('spent_usd')
    if value is not None:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation,ValueError):
            raise ValueError('Invalid observed spend') from None
        if isinstance(value,bool) or not amount.is_finite() or amount <= 0:
            raise ValueError('Invalid observed spend')
        values['spent_usd'] = str(amount)
    else:
        values['spent_usd'] = None
    values['quote_address'] = address(proof['quote_address']) if proof.get('quote_address') else None
    sender = proof.get('transaction_sender') or (proof.get('transaction') or {}).get('from')
    values['transaction_sender'] = address(sender) if sender else None
    values['base_payers'] = sorted({address(p) for p in proof.get('base_payers',[])})
    direct = 'direct wallet base payment and token receipt'
    recipient = 'funded pool buy and net recipient acquisition; funding ownership unestablished'
    values['source_proof_scope'] = proof.get('proof_scope') if proof.get('proof_scope') in (direct,recipient) else 'source role observation; further route and ownership evidence required'
    known_spend = ('verified direct wallet expenditure','pool base input, not proven personal wallet expenditure')
    values['source_spend_basis'] = proof.get('spend_basis') if proof.get('spend_basis') in known_spend else 'unestablished'
    return values


def _csv(path, values, fields):
    import io
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows({key: _cell(row.get(key)) for key in fields} for row in values)
    _atomic(path, stream.getvalue())


class CandidateEvidence:
    """A separate research journal; it never opens a paper account for writing."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proofs(digest TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
                source_id TEXT NOT NULL,chain_id INTEGER NOT NULL,tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,block_number INTEGER NOT NULL,block_hash TEXT NOT NULL,
                wallet TEXT NOT NULL,role TEXT NOT NULL,observed_at INTEGER NOT NULL,pool TEXT NOT NULL,
                token TEXT,source_rowid INTEGER,canonical INTEGER NOT NULL,canonical_scope TEXT NOT NULL,
                canonical_checked_at INTEGER NOT NULL,proof_digest TEXT NOT NULL,raw_evidence_available INTEGER NOT NULL,
                PRIMARY KEY(source_id,chain_id,tx_hash,log_index,block_hash,wallet,role));
            CREATE INDEX IF NOT EXISTS evidence_wallet ON observations(wallet,observed_at);
            CREATE INDEX IF NOT EXISTS evidence_source_row ON observations(source_id,source_rowid);
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def _get(self, key):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    @staticmethod
    def _base(proof):
        return {'chain_id': CHAIN_ID, 'tx_hash': hash32(proof['tx_hash']),
                'log_index': _integer(proof['log_index'], 'log_index'),
                'block_number': _integer(proof['block_number'], 'block_number'),
                'block_hash': hash32(proof['block_hash']),
                'observed_at': _integer(proof['observed_at'], 'observed_at'),
                'pool': address(proof['pool']),
                'token': address(proof['token_address']) if proof.get('token_address') else None}

    def _insert(self, base, roles, proof):
        encoded = _json(proof)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO proofs VALUES(?,?)', (digest, encoded))
        inserted = 0
        columns = ('source_id','chain_id','tx_hash','log_index','block_number','block_hash','wallet','role',
                   'observed_at','pool','token','source_rowid','canonical','canonical_scope','canonical_checked_at',
                   'proof_digest','raw_evidence_available')
        for wallet, role in sorted(set(roles)):
            record = {**base, 'wallet': address(wallet), 'role': role, 'proof_digest': digest}
            inserted += self.db.execute('INSERT OR IGNORE INTO observations (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(record.get(k) for k in columns)).rowcount
        return inserted

    @staticmethod
    def _validate_raw(proof, base):
        receipt, tx = proof.get('receipt'), proof.get('transaction')
        if receipt is None and tx is None:
            return False
        if not isinstance(receipt, dict) or not isinstance(tx, dict):
            raise ValueError('Incomplete transaction/receipt evidence')
        if (hash32(receipt['transactionHash']) != base['tx_hash'] or hash32(tx['hash']) != base['tx_hash']
                or hash32(receipt['blockHash']) != base['block_hash'] or hash32(tx['blockHash']) != base['block_hash']
                or quantity(receipt['blockNumber']) != base['block_number'] or quantity(receipt['status']) != 1):
            raise ValueError('Transaction/receipt identity mismatch')
        sender = address(tx['from'])
        if tx.get('blockNumber') is not None and quantity(tx['blockNumber']) != base['block_number']:
            raise ValueError('Transaction block number mismatch')
        if proof.get('transaction_sender') is not None and address(proof['transaction_sender']) != sender:
            raise ValueError('Transaction sender role mismatch')
        return True

    def import_confirmations(self, source_path, state, *, limit=500, expected_source_id=None):
        """Copy an oldest-first bounded batch and its checkpoint atomically.

        Canonical status is reconciled from the same read snapshot on every run.
        Candidate JSON may lag a committed import; rebuilding recipient metadata
        from the journal makes that crash boundary idempotent.
        The bound applies to this batch, not the lifetime candidate universe:
        old candidates and their histories never need eviction to admit evidence.
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('limit must be 1..500')
        path = Path(source_path)
        if not path.exists():
            return {'added_candidates': 0, 'imported_observations': 0, 'remaining': 0,
                    'source_id': None, 'last_rowid': 0, 'canonical_reconciled': 0, 'source_available': False}
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        source.row_factory = sqlite3.Row
        try:
            source.execute('PRAGMA query_only=ON')
            source.execute('BEGIN')
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'paper_confirmations' not in tables:
                return {'added_candidates': 0, 'imported_observations': 0, 'remaining': 0,
                        'source_id': None, 'last_rowid': 0, 'canonical_reconciled': 0, 'source_available': False}
            identity_row = source.execute("SELECT value FROM evm_metadata WHERE key='identity'").fetchone()
            identity = str(uuid.UUID(identity_row[0])) if identity_row else None
            if not identity or (expected_source_id and identity != expected_source_id):
                raise ValueError('Observed source identity mismatch')
            bound = self._get('confirmation_source_id')
            if bound and bound != identity:
                raise ValueError('Observed source journal was replaced')
            checkpoint = int(self._get('confirmation_rowid') or 0)
            candidates = copy.deepcopy(state.get('candidates', {}))
            initial = set(candidates)
            now = int(time.time())
            imported = reconciled = 0
            source_rows = source.execute('''SELECT p.rowid AS source_rowid,p.tx_hash,p.log_index,p.block_hash,
                e.canonical FROM paper_confirmations p LEFT JOIN evm_events e ON e.chain_id=?
                AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index AND e.block_hash=p.block_hash
                AND e.kind='sampled_swap' ''', (CHAIN_ID,)).fetchall()
            canonical = {r['source_rowid']: int(r['canonical'] == 1) for r in source_rows}
            batch = source.execute('''SELECT p.rowid AS source_rowid,p.*,e.block_number AS event_block,
                e.observed_at AS event_at,e.canonical AS event_canonical FROM paper_confirmations p
                LEFT JOIN evm_events e ON e.chain_id=? AND e.tx_hash=p.tx_hash AND e.log_index=p.log_index
                AND e.block_hash=p.block_hash AND e.kind='sampled_swap'
                WHERE p.rowid>? ORDER BY p.rowid LIMIT ?''', (CHAIN_ID, checkpoint, limit)).fetchall()
            with self.db:
                self._set('confirmation_source_id', identity)
                old_rows = self.db.execute('SELECT DISTINCT source_rowid,canonical FROM observations WHERE source_id=?', (identity,)).fetchall()
                for row in old_rows:
                    current = canonical.get(row['source_rowid'], 0)
                    reconciled += row['canonical'] != current
                    self.db.execute('UPDATE observations SET canonical=?,canonical_checked_at=? WHERE source_id=? AND source_rowid=?', (current, now, identity, row['source_rowid']))
                for row in batch:
                    proof = json.loads(row['payload'])
                    base = self._base(proof)
                    wallet = address(proof['wallet'])
                    if proof.get('verified_swap') is not True or proof.get('factory_verified') is not True:
                        raise ValueError('Confirmation is not a verified factory acquisition')
                    if (base['tx_hash'] != row['tx_hash'] or base['log_index'] != row['log_index']
                            or base['block_hash'] != row['block_hash'] or base['pool'] != row['pool']
                            or base['token'] != row['token'] or base['observed_at'] != row['observed_at']
                            or (row['event_block'] is not None and base['block_number'] != row['event_block'])
                            or (row['event_at'] is not None and base['observed_at'] != row['event_at'])):
                        raise ValueError('Confirmation payload differs from source event')
                    active = canonical[row['source_rowid']]
                    if active and wallet not in candidates:
                        candidates[wallet] = {'wallet': wallet, 'first_seen_at': now, 'sampled_transactions': 0,
                            'example_transaction': base['tx_hash'], 'example_pool': base['pool'], 'qualified': False,
                            'source': 'canonical funded recipient observation; historical qualification pending'}
                    raw_available = self._validate_raw(proof, base)
                    sender = proof.get('transaction_sender') or (proof.get('transaction') or {}).get('from')
                    roles = [(wallet, 'funded_recipient')]
                    if sender:
                        roles.append((address(sender), 'transaction_sender'))
                    payers = proof.get('base_payers', [])
                    if not isinstance(payers, list):
                        raise ValueError('base_payers must be an address list')
                    roles.extend((address(payer), 'base_payer') for payer in payers)
                    base.update(source_id=identity, source_rowid=row['source_rowid'], canonical=active,
                        canonical_scope='source_journal_current', canonical_checked_at=now,
                        raw_evidence_available=int(raw_available))
                    imported += self._insert(base, roles, proof)
                    checkpoint = row['source_rowid']
                    self._set('confirmation_rowid', checkpoint)
                # Recompute recipient priority, including revocations. Do not
                # erase candidates or pretend orphaned discovery never happened.
                for item in candidates.values():
                    if item.get('observed_recipient'):
                        item['observed_recipient'] = False
                    item['observed_recipient_transactions'] = 0
                recipient_rows = self.db.execute('''SELECT wallet,MIN(observed_at) first_at,MAX(observed_at) last_at,
                    COUNT(DISTINCT tx_hash) transactions FROM observations WHERE source_id=?
                    AND role='funded_recipient' AND canonical=1 GROUP BY wallet ORDER BY first_at,wallet''', (identity,)).fetchall()
                for row in recipient_rows:
                    wallet = row['wallet']
                    if wallet not in candidates:
                        example = self.db.execute('SELECT tx_hash,pool FROM observations WHERE source_id=? AND wallet=? AND role=? AND canonical=1 ORDER BY observed_at LIMIT 1', (identity,wallet,'funded_recipient')).fetchone()
                        candidates[wallet] = {'wallet':wallet, 'first_seen_at':now, 'sampled_transactions':0,
                            'example_transaction':example['tx_hash'], 'example_pool':example['pool'],
                            'source':'canonical funded recipient observation; historical qualification pending', 'qualified':False}
                    candidates[wallet].update(observed_recipient=True, observed_recipient_transactions=row['transactions'],
                        first_observed_at=row['first_at'], last_seen_at=max(candidates[wallet].get('last_seen_at',0),row['last_at']))
            state['candidates'] = candidates
            remaining = source.execute('SELECT COUNT(*) FROM paper_confirmations WHERE rowid>?', (checkpoint,)).fetchone()[0]
            return {'added_candidates':len(set(candidates)-initial), 'imported_observations':imported,
                    'remaining':remaining, 'source_id':identity, 'last_rowid':checkpoint,
                    'canonical_reconciled':reconciled, 'capacity_blocked':False, 'source_available':True,
                    'import_limit':limit, 'candidate_limit':None}
        finally:
            source.close()

    def add_sender_observations(self, observations):
        """Persist successfully committed broad-discovery samples, idempotently."""
        count = 0
        with self.db:
            for proof in observations:
                if proof.get('source_id') != 'rpc-swap-discovery-v1' or proof.get('role') != 'transaction_sender' or proof.get('canonical_at_capture') is not True:
                    raise ValueError('Invalid RPC discovery scope')
                base = self._base(proof)
                raw = self._validate_raw(proof, base)
                if not raw:
                    raise ValueError('RPC discovery requires original transaction/receipt')
                block = proof['block_header']
                if hash32(block['hash']) != base['block_hash'] or quantity(block['number']) != base['block_number'] or quantity(block['timestamp']) != base['observed_at']:
                    raise ValueError('RPC discovery block identity mismatch')
                wallet = address(proof['wallet'])
                if address(proof['transaction']['from']) != wallet:
                    raise ValueError('RPC discovery sender mismatch')
                base.update(source_id='rpc-swap-discovery-v1', source_rowid=None, canonical=1,
                    canonical_scope='rpc_verified_at_capture_only', canonical_checked_at=int(time.time()), raw_evidence_available=1)
                count += self._insert(base, [(wallet,'transaction_sender')], proof)
        return count

    def export(self, folder, state, reports, selected, snapshot, now):
        """Publish allowlisted evidence and assessment pointers atomically per file.

        The index is written last and carries a generation timestamp. Full raw
        transaction/receipt proof stays in this local SQLite journal, addressed
        by SHA-256; public exports never serialize provider URLs or credentials.
        """
        _integer(now, 'now')
        folder = Path(folder)
        by_wallet = defaultdict(list)
        observations = []
        proof_values = {}
        for row in self.db.execute('SELECT * FROM observations ORDER BY wallet,observed_at,tx_hash,log_index,role'):
            value = dict(row)
            digest = value['proof_digest']
            if digest not in proof_values:
                proof_values[digest] = _public_proof(json.loads(self.db.execute('SELECT payload FROM proofs WHERE digest=?',(digest,)).fetchone()[0]))
            value.update(proof_values[digest])
            value['canonical'] = bool(value['canonical'])
            value['raw_evidence_available'] = bool(value['raw_evidence_available'])
            value['qualification'] = 'unestablished'
            value['controller_independence'] = 'unestablished'
            value['funding_ownership'] = 'unestablished'
            value['proof_scope'] = ('funded pool acquisition; recipient, sender and payer remain distinct'
                                    if value['role']=='funded_recipient' else 'observed address role only; trading intent and ownership unestablished')
            if (value['role']=='funded_recipient' and value['raw_evidence_available']
                    and value['source_proof_scope']=='direct wallet base payment and token receipt'
                    and value['wallet']==value['transaction_sender']):
                value['funding_ownership'] = 'direct_wallet_payment_verified'
            observations.append(value)
            by_wallet[value['wallet']].append(value)
        candidates = {address(w): c for w,c in state.get('candidates',{}).items()}
        assessment = {address(r['wallet']):r for r in reports}
        index, address_jobs = [], []
        fields = ['wallet','role','source_id','chain_id','tx_hash','log_index','block_number','block_hash','observed_at','pool','token','quote_address','amount_raw','spent_raw','spent_usd','transaction_sender','base_payers','canonical','canonical_scope','canonical_checked_at','raw_evidence_available','proof_digest','qualification','controller_independence','funding_ownership','proof_scope','source_proof_scope','source_spend_basis']
        for wallet in sorted(set(candidates) | set(by_wallet)):
            evidence = by_wallet[wallet]
            candidate = candidates.get(wallet, {})
            report = assessment.get(wallet)
            selection = selected.get(wallet, {})
            review = selection.get('qualification_evidence', {}).get('controller_review', {})
            reviewed, expires = review.get('reviewed_at'), review.get('expires_at')
            review_current = bool(report and type(reviewed) is int and type(expires) is int
                and 0 <= reviewed <= now <= expires and expires-reviewed <= 7*86400
                and review.get('chain_id') == CHAIN_ID
                and review.get('history_cutoff') == report.get('cutoff')
                and selection.get('qualification_evidence', {}).get('history_cutoff') == report.get('cutoff')
                and review.get('independence_reviewed') is True
                and isinstance(review.get('scope'), str) and review['scope'].strip()
                and isinstance(review.get('cluster_id'), str) and review['cluster_id']
                and review['cluster_id'] == selection.get('cluster_id')
                and isinstance(review.get('evidence_urls'), list) and review['evidence_urls']
                and all(isinstance(url,str) and url.startswith('https://') for url in review['evidence_urls']))
            complete = report.get('history_complete') is True if report else False
            performance_available = bool(complete and report.get('performance_estimate_available') is True)
            pnl, gas, closed = None, None, None
            if performance_available:
                try:
                    raw_pnl, raw_gas = report.get('net_pnl_after_follower_stress_usd'), report.get('gas_usd')
                    pnl, gas = Decimal(str(raw_pnl)), Decimal(str(raw_gas))
                    closed = report.get('closed_positions')
                    performance_available = (not isinstance(raw_pnl, bool) and not isinstance(raw_gas, bool)
                        and pnl.is_finite() and gas.is_finite() and gas >= 0
                        and type(closed) is int and closed >= 0)
                except (InvalidOperation, ValueError):
                    performance_available = False
            qualified = bool(performance_available and report.get('performance_qualified') is True
                and selection.get('qualified') is True and review_current)
            history = {'assessment': report.get('assessment') if report else 'NOT_ASSESSED',
                'cutoff':report.get('cutoff') if report else None,
                'history_complete': complete, 'performance_qualified': bool(performance_available and report.get('performance_qualified') is True),
                'performance_estimate_available': performance_available,
                'net_pnl_after_follower_stress_usd': str(pnl) if performance_available else None,
                'gas_usd': str(gas) if performance_available else None,
                'closed_positions': closed if performance_available else None,
                'qualification_blockers': report.get('qualification_blockers',[]) if report else ['history_not_assessed'],
                'controller_status': report.get('controller_status','UNKNOWN') if report else 'UNKNOWN',
                'controller_review_current':review_current,
                'qualification_checked_at':now,
                'independence_verified': False, 'qualified': qualified,
                'history_path': f"cohort-{snapshot['block']}/history-{wallet}.json" if snapshot and (folder/f"cohort-{snapshot['block']}/history-{wallet}.json").exists() else None,
                'report_path': f"cohort-{snapshot['block']}/wallet-{wallet}.json" if snapshot and (folder/f"cohort-{snapshot['block']}/wallet-{wallet}.json").exists() else None}
            roles = dict(Counter(row['role'] for row in evidence))
            current = sum(row['canonical'] and row['canonical_scope']=='source_journal_current' for row in evidence)
            capture = sum(row['canonical_scope']=='rpc_verified_at_capture_only' for row in evidence)
            summary = {'wallet':wallet, 'research_candidate':wallet in candidates, 'evidence_count':len(evidence),
                'first_observed_at':min((r['observed_at'] for r in evidence),default=None),
                'last_observed_at':max((r['observed_at'] for r in evidence),default=None),
                'current_source_checked_observations':current, 'capture_only_observations':capture,
                'invalidated_observations':sum(not r['canonical'] for r in evidence), 'roles':roles,
                'evidence_status':'indexed_observations' if evidence else 'legacy_reference_only',
                'observed_recipient':candidate.get('observed_recipient',False), 'qualified':qualified,
                'history_complete':complete, 'performance_qualified':history['performance_qualified'],
                'json_path':f'addresses/{wallet}.json', 'csv_path':f'addresses/{wallet}.csv'}
            legacy = None
            if not evidence and candidate.get('example_transaction'):
                legacy = {'transaction_hash':hash32(candidate['example_transaction']),
                          'scope':'Legacy discovery reference; corroborating raw proof is not indexed here'}
            payload = {'schema_version':1,'mode':'RESEARCH_ONLY','generated_at':now,**summary,
                'legacy_reference':legacy,'history':history,'observations':evidence,
                'raw_proof_location':self.path.name+' / proofs.digest',
                'limitations':'Activity is candidate discovery, not evidence of profitable skill, independent control, current safety or executable routes. RPC samples are canonical only as checked at capture; source-backed rows reflect the last local-journal reconciliation.'}
            address_jobs.append((folder, summary, payload, evidence, fields))
            index.append(summary)
        # Thousands of sequential fsync/replaces exceeded the worker deadline on
        # Windows. Eight writers and 64 queued addresses bound disk concurrency;
        # retain exact contents,
        # fresh qualification checks and fsync. Publish the index only after ALL
        # address files succeed. Exceptions propagate; no false completion.
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix='evidence-export') as pool:
            for offset in range(0, len(address_jobs), 64):
                list(pool.map(_write_address, address_jobs[offset:offset+64]))
        _csv(folder/'wallet-evidence.csv', observations, fields)
        _atomic(folder/'wallet-evidence.jsonl', ''.join(_json(row)+'\n' for row in observations))
        result = {'schema_version':1,'mode':'RESEARCH_ONLY','generated_at':now,'address_count':len(index),
            'candidate_count':len(candidates),'observation_count':len(observations),
            'source_checked_observations':sum(x['current_source_checked_observations'] for x in index),
            'capture_only_observations':sum(x['capture_only_observations'] for x in index),
            'invalidated_observations':sum(x['invalidated_observations'] for x in index),
            'legacy_reference_only_count':sum(x['evidence_status']=='legacy_reference_only' for x in index),
            'qualified_count':sum(x['qualified'] for x in index),
            'index_file':str(folder/'wallet-evidence-index.json'),'csv_file':str(folder/'wallet-evidence.csv'),
            'jsonl_file':str(folder/'wallet-evidence.jsonl'),'addresses_directory':str(folder/'addresses'),
            'addresses':index}
        _atomic(folder/'wallet-evidence-index.json', _json(result)+'\n')
        return {k:v for k,v in result.items() if k!='addresses'}
