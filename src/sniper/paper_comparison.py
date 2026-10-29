"""Isolated prospective paper comparisons on a durable, shared observation tape.

No source-account writes, notification delivery, signing or model calls. Quotes
are observations, never fills. Each portfolio commits its input cursor with its
own accounting so interrupted processing cannot replay a financial operation.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .ledger import Ledger, json_text
from .models import Candidate, MarketQuote, WalletConfirmation, decimal, json_safe
from .policy import RiskPolicy

VARIANTS = ('baseline', 'A', 'B')
FORMAT = 'radeon_comparison_v1'
ENTRY_COMPATIBILITY_SCOPE = ('Dual-entry source signals are comparable only when retained, freshly canonical-checked '
    'proofs independently satisfy the original two-buyer rule in the selected execution pool. '
    'Other dual-entry inputs are recorded as noncomparable gaps; existing exits continue. '
    'The source collector still constrains coverage. No independent strategy returns are inferred.')


def legacy_entry_candidate(candidate, quote, now, policy, source_id):
    """Project only the provable original admission subset from a newer source.

    This is a local evidence check, not a new canonical RPC check. The source
    runner binds the recorded check time and proofs before capture. New entry
    selectors never change the immutable comparison's original admission rule.
    """
    metadata = candidate.metadata
    selectors = ('entry_revision', 'initiating_strategy', 'qualified_strategies', 'strategy_signals', 'canonical_checked_at')
    if not any(key in metadata for key in selectors):
        return candidate
    if not isinstance(metadata.get('entry_revision'), str) or not metadata['entry_revision']:
        return None
    from .adapters.evm import address, hash32
    try:
        checked = metadata.get('canonical_checked_at')
        if (metadata.get('source_id') != source_id or type(checked) is not int
                or not 0 <= now - checked <= policy.max_quote_age_seconds or quote is None
                or quote.asset_id != candidate.asset_id
                or not candidate.asset_id.startswith('eip155:4663/erc20:')):
            return None
        token, pool = address(candidate.asset_id.split(':')[-1]), address(quote.pool_id)
        signals = metadata.get('strategy_signals')
        if not isinstance(signals, list):
            return None
        signals = [s for s in signals if isinstance(s, dict) and s.get('strategy_id') == 'two_wallet_v3'
                   and s.get('qualified') is True]
        if len(signals) != 1:
            return None
        proofs = signals[0].get('source_proofs')
        if not isinstance(proofs, list) or len(proofs) > 1000:
            return None
        eligible = []
        for proof in proofs:
            if (not isinstance(proof, dict) or proof.get('verified_swap') is not True
                    or proof.get('factory_verified') is not True or type(proof.get('chain_id', 4663)) is not int
                    or proof.get('chain_id', 4663) != 4663
                    or address(proof.get('token_address')) != token):
                return None
            # Pool facts remain scoped to the pool they describe. Cross-pool
            # evidence must never be relabelled as proof for this route.
            if address(proof.get('pool')) != pool:
                continue
            at = proof.get('observed_at')
            if (type(at) is not int or not 0 <= now - at <= policy.max_confirmation_age_seconds or at > checked
                    or type(proof.get('block_number')) is not int or proof['block_number'] < 0
                    or type(proof.get('log_index')) is not int or proof['log_index'] < 0):
                return None
            wallet, tx = address(proof.get('wallet')), hash32(proof.get('tx_hash'))
            hash32(proof.get('block_hash'))
            spent = decimal(proof.get('spent_usd'), minimum=policy.min_wallet_spend_usd)
            eligible.append(WalletConfirmation(wallet, 'unreviewed:' + wallet, tx, at, spent, True, False))
        # Maximum distinct-wallet / distinct-transaction matching avoids a
        # greedy choice losing a valid pair when a wallet has several buys.
        by_wallet, matched = {}, {}
        for proof in sorted(eligible, key=lambda p: (-p.observed_at, p.wallet, p.tx_hash)):
            by_wallet.setdefault(proof.wallet, []).append(proof)
        def assign(wallet, seen):
            for proof in by_wallet[wallet]:
                if proof.tx_hash in seen:
                    continue
                seen.add(proof.tx_hash)
                old = matched.get(proof.tx_hash)
                if old is None or assign(old.wallet, seen):
                    matched[proof.tx_hash] = proof
                    return True
            return False
        for wallet in by_wallet:
            assign(wallet, set())
            if len(matched) >= policy.min_independent_wallets:
                break
        confirmations = list(matched.values())
        if len(confirmations) < policy.min_independent_wallets:
            return None
        comparison_metadata = {k: v for k, v in metadata.items() if k not in (
            'entry_revision', 'initiating_strategy', 'qualified_strategies', 'strategy_signals', 'canonical_checked_at')}
        comparison_metadata.update(comparison_source_entry_revision=metadata['entry_revision'],
            comparison_source_signals=metadata['strategy_signals'], comparison_canonical_checked_at=checked,
            comparison_entry_scope='legacy_same_pool_two_wallet')
        return replace(candidate, confirmations=confirmations, metadata=comparison_metadata)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _atomic(path, value):
    _atomic_text(path, json_text(value))


def _atomic_text(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def initialize(folder, source_path, policy, trial, *, now=None):
    """Explicit initialization only; caller holds the active controller lock.

    Seed is one consistent read-only SQLite backup, not invented starting cash.
    Interrupted initialization is retained and refused, never silently reset.
    """
    folder, source_path = Path(folder).resolve(), Path(source_path).resolve()
    now = int(time.time()) if now is None else now
    if folder == source_path.parent or source_path.is_relative_to(folder):
        raise ValueError('Comparison must be separate from the source account')
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Comparison already exists or initialization needs review')
    if now >= trial['ends_at']:
        raise ValueError('Cannot start comparison after the original entry deadline')
    folder.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    seed_path = folder / 'seed.sqlite3'
    seed = sqlite3.connect(seed_path)
    try:
        source.backup(seed)
    finally:
        seed.close(); source.close()
    seed = Ledger(seed_path, policy.starting_cash_usd, require_existing=True)
    try:
        if (seed._get('account_id') != trial['account_id'] or seed._get('policy_version') != policy.version
                or json.loads(seed._get('trial_manifest')) != trial):
            raise ValueError('Source account, policy or trial binding mismatch')
        snapshot = seed.snapshot()
        common = dict(format=FORMAT, comparison_id=str(uuid.uuid4()), source_account_id=trial['account_id'],
            source_id=trial['source_id'], started_at=now, entry_deadline=trial['ends_at'],
            excluded_from_strategy=True, seed_fill_id=seed.conn.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0],
            seed_event_rowid=seed.conn.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0],
            seed_equity_id=seed.conn.execute('SELECT COALESCE(MAX(id),0) FROM equity_history').fetchone()[0],
            seed_closed_ids=[r[0] for r in seed.conn.execute('SELECT position_id FROM closed_positions')],
            seed_equity=str(snapshot['equity']), seed_realized_pnl=str(snapshot['realized_pnl']),
            seed_fees_paid=str(snapshot['fees_paid']), seed_positions=json_safe(snapshot['positions']),
            inherited_position_ids=[p['position_id'] for p in snapshot['positions']],
            policy_version=policy.version, policy=policy.as_dict(),
            entry_quote_scope='Shared current source-account sized route; each portfolio rechecks its own cash/risk and never exceeds observed coverage. Divergent smaller sizes retain the shared conservative cost estimate.',
            intake_scope='Prospective sampled Radeon candidates reaching its engine. Source quorum, source admission, one-entry-per-cycle collection stop and missing observations constrain all variants. Not an exhaustive independent market backtest.')
        accounts = {}
        for variant in VARIANTS:
            target = folder / variant / 'ledger.sqlite3'; target.parent.mkdir()
            conn = sqlite3.connect(target)
            try:
                seed.conn.backup(conn)
            finally:
                conn.close()
            ledger = Ledger(target, policy.starting_cash_usd, require_existing=True)
            try:
                account = str(uuid.uuid4()); accounts[variant] = account
                with ledger.transaction():
                    ledger._set('account_id', account)
                    ledger._set('paper_comparison', json_text({**common, 'variant': variant}))
                    ledger._set('comparison_cursor', '0')
                    ledger._set('comparison_source_halt', ledger._get('entry_halt') or '')
                    ledger._set('source_trial_manifest', ledger._get('trial_manifest'))
                    ledger.conn.execute("DELETE FROM metadata WHERE key='trial_manifest'")
                    ledger._set('last_processed_at', now)
                from .paper_variants import VariantEngine
                VariantEngine(ledger, policy, variant=variant)
            finally:
                ledger.close()
        manifest = {**common, 'accounts': accounts, 'source_path': str(source_path)}
        conn = sqlite3.connect(folder / 'observations.sqlite3')
        try:
            conn.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,
                observed_at INTEGER NOT NULL,captured_at REAL NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
                digest TEXT NOT NULL);''')
            conn.execute('INSERT INTO metadata VALUES(?,?)', ('binding', json_text(manifest)))
            conn.commit()
        finally:
            conn.close()
        _atomic(folder / 'manifest.json', manifest)
        return manifest
    finally:
        seed.close()


class Comparison:
    def __init__(self, folder, *, source_account_id, source_id, policy_version):
        self.folder = Path(folder).resolve()
        self.manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        m = self.manifest
        if (m.get('format') != FORMAT or m.get('source_account_id') != source_account_id
                or m.get('source_id') != source_id or m.get('policy_version') != policy_version
                or m.get('excluded_from_strategy') is not True):
            raise ValueError('Comparison source binding mismatch')
        ids = list(m['accounts'].values())
        if len(ids) != 3 or len(set(ids + [source_account_id])) != 4:
            raise ValueError('Comparison account identities are not isolated')
        self.policy = RiskPolicy(**m['policy'])
        if self.policy.version != policy_version:
            raise ValueError('Comparison policy content changed')
        self.conn = sqlite3.connect((self.folder / 'observations.sqlite3').as_uri() + '?mode=rw', uri=True, timeout=2)
        self.conn.row_factory = sqlite3.Row
        if self.conn.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0] != json_text(m):
            self.conn.close(); raise ValueError('Comparison journal identity changed')
        self.ledgers = {}

    def close(self):
        for ledger, _ in self.ledgers.values():
            ledger.close()
        self.conn.close()

    def portfolios(self):
        if not self.ledgers:
            from .paper_variants import VariantEngine
            opened = {}
            try:
                for name in VARIANTS:
                    path = self.folder / name / 'ledger.sqlite3'
                    ledger = Ledger(path, self.policy.starting_cash_usd, require_existing=True)
                    try:
                        if ledger._get('account_id') != self.manifest['accounts'][name]:
                            raise ValueError('Comparison ledger identity changed')
                        opened[name] = (ledger, VariantEngine(ledger, self.policy, variant=name))
                    except BaseException:
                        ledger.close(); raise
            except BaseException:
                for ledger, _ in opened.values():
                    ledger.close()
                raise
            self.ledgers = opened
        return self.ledgers

    def capture(self, key, kind, now, payload):
        if kind not in ('watch', 'candidate', 'gap', 'control') or type(now) is not int or now < self.manifest['started_at']:
            raise ValueError('Invalid comparison observation')
        encoded = json_text(payload)
        digest = hashlib.sha256(json_text([kind, now, payload]).encode()).hexdigest()
        with self.conn:
            row = self.conn.execute('SELECT seq,digest FROM observations WHERE event_key=?', (key,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise ValueError('Comparison observation identity reused with different content')
                return row['seq']
            latest = self.conn.execute('SELECT MAX(observed_at) FROM observations').fetchone()[0]
            if latest is not None and now < latest:
                raise ValueError('Out-of-order comparison observation')
            return self.conn.execute('INSERT INTO observations(event_key,observed_at,captured_at,kind,payload,digest) VALUES(?,?,?,?,?,?)',
                (key, now, time.time(), kind, encoded, digest)).lastrowid

    def process(self, limit=1000):
        counts = {}
        for name, (ledger, engine) in self.portfolios().items():
            cursor = int(ledger._get('comparison_cursor') or 0)
            rows = self.conn.execute('SELECT * FROM observations WHERE seq>? ORDER BY seq LIMIT ?', (cursor, limit)).fetchall()
            for row in rows:
                payload, now, seq = json.loads(row['payload']), row['observed_at'], row['seq']
                if seq != cursor + 1 or hashlib.sha256(json_text([row['kind'], now, payload]).encode()).hexdigest() != row['digest']:
                    raise ValueError('Comparison observation gap or changed payload')
                with ledger.transaction():
                    if row['kind'] == 'watch':
                        observed = {(q.asset_id, q.pool_id): q for q in map(MarketQuote.from_dict, payload['quotes'])}
                        if payload.get('supplemental'):
                            # Supplemental checks update only covered assets; other existing
                            # marks can be reused within their original freshness window.
                            for event in ledger.conn.execute("SELECT payload FROM events WHERE kind='comparison_quote_observation' ORDER BY rowid DESC LIMIT 100"):
                                q = MarketQuote.from_dict(json.loads(event[0])['quote'])
                                if (q.asset_id, q.pool_id) not in observed and 0 <= now - q.observed_at <= self.policy.max_quote_age_seconds:
                                    observed[(q.asset_id, q.pool_id)] = q
                        for q in observed.values():
                            ledger.record_event(f'comparison-quote:{seq}:{q.asset_id}:{q.pool_id}', 'comparison_quote_observation',
                                {'quote': q.as_dict(), 'input_seq': seq}, now)
                        quotes = {p['asset_id']: observed[(p['asset_id'], p['pool_id'])] for p in ledger.positions()
                                  if (p['asset_id'], p['pool_id']) in observed}
                        engine.watch(quotes, now)
                    elif row['kind'] == 'candidate':
                        if now < self.manifest['entry_deadline']:
                            candidate = Candidate.from_dict(payload['candidate'])
                            quote = MarketQuote.from_dict(payload['quote']) if payload['quote'] is not None else None
                            cycle = str(payload['cycle'])
                            comparable = legacy_entry_candidate(candidate, quote, now, self.policy, self.manifest['source_id'])
                            if comparable is None:
                                ledger.record_event(f'comparison-entry-scope:{seq}', 'comparison_gap',
                                    {'reason': 'entry_strategy_revision_not_comparable', 'candidate': candidate.event_id,
                                     'source_entry_revision': candidate.metadata.get('entry_revision'),
                                     'required_scope': 'legacy_same_pool_two_wallet'}, now)
                            elif ledger._get('comparison_last_buy_cycle') != cycle:
                                candidate = comparable
                                result = engine.consider(candidate, quote, now)
                                if result['verdict'] == 'PAPER_BUY':
                                    ledger._set('comparison_last_buy_cycle', cycle)
                            else:
                                ledger.record_event(f'comparison-cycle-cap:{seq}', 'comparison_gap',
                                    {'reason': 'existing_one_entry_per_cycle_rule', 'candidate': candidate.event_id}, now)
                        else:
                            ledger.record_event(f'comparison-deadline:{seq}', 'comparison_entry_deadline', {}, now)
                    elif row['kind'] == 'control':
                        source_halt = payload.get('entry_halt') or ''
                        prior = ledger._get('comparison_source_halt') or ''
                        if source_halt or ledger._get('entry_halt') == prior:
                            ledger._set('entry_halt', source_halt)
                        ledger._set('comparison_source_halt', source_halt)
                    else:
                        ledger.record_event(f'comparison-gap:{seq}', 'comparison_gap', payload, now)
                    ledger.record_event(f'comparison-input:{seq}', 'comparison_input',
                        {'seq': seq, 'digest': row['digest'], 'source_event_at': now,
                         'captured_at': row['captured_at'], 'processed_at': time.time()}, now)
                    ledger._set('comparison_cursor', seq)
                cursor = seq
            counts[name] = len(rows)
        return counts

    def needed_quotes(self, now, limit=2):
        """Bounded rotating union of actual experimental holdings; no new buys."""
        needed = {}
        for ledger, _ in self.portfolios().values():
            for p in ledger.positions():
                if p['mark_status'] == 'ok' and 0 <= now - p['mark_at'] <= min(25, self.policy.max_quote_age_seconds):
                    continue
                key = (p['asset_id'], p['pool_id'])
                needed[key] = max(needed.get(key, Decimal(0)), p['qty'])
        keys = sorted(needed)
        old = self.conn.execute("SELECT value FROM metadata WHERE key='quote_rotation'").fetchone()
        offset = int(old[0]) % max(1, len(keys)) if old else 0
        keys = (keys[offset:] + keys[:offset])[:limit]
        with self.conn:
            self.conn.execute("INSERT INTO metadata VALUES('quote_rotation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset + len(keys)),))
        return [(a, p, needed[(a, p)]) for a, p in keys]

    def publish(self, now):
        from .comparison_metrics import report
        high = self.conn.execute('SELECT COALESCE(MAX(seq),0) FROM observations').fetchone()[0]
        results = {}
        for name, (ledger, engine) in self.portfolios().items():
            cursor = int(ledger._get('comparison_cursor') or 0)
            # Report is a mark expiry, never a made-up provider observation.
            if cursor == high:
                engine.report(now)
            results[name] = {**report(ledger, now), 'cursor': cursor, 'backlog': high - cursor}
        value = dict(mode='ISOLATED_PAPER_COMPARISON', as_of=now, comparison_id=self.manifest['comparison_id'],
            source_account_id=self.manifest['source_account_id'], excluded_from_strategy=True,
            entry_deadline=self.manifest['entry_deadline'], started_at=self.manifest['started_at'],
            intake_scope=self.manifest['intake_scope'], entry_quote_scope=self.manifest['entry_quote_scope'],
            entry_compatibility_scope=ENTRY_COMPATIBILITY_SCOPE,
            past_results_reconstructed=False, variants=results,
            profitability_claim=False, paid_calls_enabled=False, live_execution_enabled=False)
        _atomic(self.folder / 'report.json', value)
        rows = []
        for name, result in results.items():
            cells = [name, result['net_pnl'], result['modeled_costs'], result['new_entries'],
                     result['closed_win_rate_denominator'], result['max_observed_drawdown_pct'],
                     len(result['unresolved_positions']), result['backlog']]
            rows.append('<tr>' + ''.join('<td>' + html.escape(str(c)) + '</td>' for c in cells) + '</tr>')
        details = ''.join('<details><summary>' + html.escape(name) + ' evidence and scoped results</summary><pre>'
                          + html.escape(json.dumps(result, indent=2)) + '</pre></details>' for name, result in results.items())
        _atomic_text(self.folder / 'report.html', '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Radeon paper comparison</title>
<style>body{font:16px system-ui;background:#111318;color:#eee;max-width:1100px;margin:2rem auto;padding:1rem}
table{border-collapse:collapse;width:100%}td,th{padding:.7rem;text-align:left;border-bottom:1px solid #454750}
.table{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}details{margin:1rem 0}p{line-height:1.5}</style>
<h1>Radeon · isolated paper comparison</h1><p>Baseline: current exits. A: earlier cost-aware protection.
B: recovery before hard-stop re-entry. No automatic promotion. These portfolios are excluded from the active trial.</p>
<p>As of UTC epoch ''' + str(now) + '; original entry deadline ' + str(self.manifest['entry_deadline']) + '''.</p>
<div class="table"><table><thead><tr><th>Variant</th><th>Net change USD</th><th>Fill costs USD</th>
<th>New entries</th><th>Closed sample</th><th>Observed drawdown %</th><th>Unresolved</th><th>Backlog</th></tr></thead><tbody>'''
            + ''.join(rows) + '</tbody></table></div><p>' + html.escape(self.manifest['intake_scope'])
            + '</p><p>' + html.escape(self.manifest['entry_quote_scope']) + '</p><p>' + html.escape(ENTRY_COMPATIBILITY_SCOPE)
            + '</p><p>Unresolved values are conservative unrealized marks, never sales. Small samples and gaps do not establish profitability.</p>'
            + details + '</html>')
        return value
