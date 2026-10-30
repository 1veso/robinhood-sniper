"""Prospective size experiment. Primary ledgers and input tape are read-only.

Each fresh portfolio uses the real parallel admission and bound runner exits.
There are no copied trades, scaled fills, notifications, model calls or signers.
"""
from dataclasses import replace
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid

from .ledger import Ledger, json_text
from .models import Candidate, MarketQuote
from .policy import RiskPolicy
from .parallel_entry import ParallelEntryEngine, signal_proofs
from .paper_comparison import _atomic, _atomic_text
from .radeon_exits import activate, revision
from .comparison_metrics import report

FORMAT = 'radeon_size_comparison_v1'
VARIANTS = ('control', 'usd25')
SCOPE = ('Fresh equal $1,000 accounts; prospective Radeon candidates reaching its existing observation tape. '
         'Primary account blockers, sampled collection and its one-entry-per-cycle stop constrain intake. '
         'Not exhaustive market coverage or a replay of primary returns. Each variant obtains its own fresh '
         'size-covered pinned-pool routes and rechecks canonical purchases. Sequential quote timing differs. '
         'Missing/stale evidence is a gap, never an invented fill. No automatic promotion.')


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def metadata(conn, table='metadata'):
    return {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM ' + table)}


def initialize(root, *, now=None):
    root = Path(root).resolve()
    folder = root / 'runtime/radeon-sizing'
    now = int(time.time()) if now is None else now
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('Sizing experiment exists; never reset or silently repair it')
    primary = readonly(root / 'runtime/exploration/ledger.sqlite3')
    tape = readonly(root / 'runtime/paper-comparison/observations.sqlite3')
    source = readonly(root / 'runtime/exploration/events.sqlite3')
    try:
        primary.execute('BEGIN')
        meta = metadata(primary)
        trial = json.loads(meta['trial_manifest'])
        rules = json.loads(meta['entry_rules'])
        exits = json.loads(meta['radeon_exit_policy'])
        policy = RiskPolicy(**json.loads(meta['policy']))
        tape_binding = json.loads(metadata(tape)['binding'])
        source_id = metadata(source, 'evm_metadata')['identity']
        high = tape.execute('SELECT COALESCE(MAX(seq),0) FROM observations').fetchone()[0]
        if (now >= trial['ends_at'] or source_id != trial['source_id']
                or tape_binding['source_account_id'] != trial['account_id']
                or tape_binding['source_id'] != source_id or meta['account_id'] != trial['account_id']
                or exits['revision'] != trial['exit_revision'] or exits['revision'] != revision(exits['policy'])
                or exits['entry_revision'] != trial['entry_revision']
                or policy.starting_cash_usd != Decimal(1000)
                or policy.per_position_risk_pct != Decimal('.5') or policy.max_account_risk_pct != Decimal(2)):
            raise ValueError('Source bindings or approved control risk differ')
        policies = {'control': policy, 'usd25': replace(policy, ticket_usd=Decimal(25),
                    per_position_risk_pct=Decimal('2.5'), max_account_risk_pct=Decimal(10))}
        manifest = dict(format=FORMAT, comparison_id=str(uuid.uuid4()), started_at=now,
            source_account_id=trial['account_id'], source_id=source_id,
            tape_comparison_id=tape_binding['comparison_id'], tape_start=high,
            entry_deadline=trial['ends_at'], entry_revision=trial['entry_revision'], entry_rules=rules,
            exit_revision=exits['revision'], exit_policy=exits['policy'],
            policies={k: v.as_dict() for k, v in policies.items()},
            accounts={k: str(uuid.uuid4()) for k in VARIANTS}, excluded_from_strategy=True,
            source_tape=str(root / 'runtime/paper-comparison/observations.sqlite3'), intake_scope=SCOPE,
            size_definition='Maximum full-loss commitment including entry costs and exit reserve; actual notional may be lower.',
            paid_calls_enabled=False, live_execution_enabled=False)
        folder.mkdir(parents=True)
        for name, p in policies.items():
            ledger = Ledger(folder / name / 'ledger.sqlite3', p.starting_cash_usd)
            try:
                own_trial = {**trial, 'account_id': manifest['accounts'][name], 'started_at': now,
                             'policy_version': p.version}
                own_trial.pop('exit_revision', None)
                own_trial.pop('exit_revision_started_at', None)
                with ledger.transaction():
                    for key, value in [('account_id', manifest['accounts'][name]), ('execution_mode', 'paper'),
                        ('entry_revision', trial['entry_revision']), ('entry_rules', json_text(rules)),
                        ('trial_manifest', json.dumps(own_trial, sort_keys=True)), ('policy_version', p.version),
                        ('policy', json_text(p.as_dict())), ('engine_mode', 'exploratory_paper_v1'),
                        ('sizing_binding', json_text(manifest)), ('comparison_cursor', str(high))]:
                        ledger._set(key, value)
                    ledger._set('paper_comparison', json_text({**manifest, 'variant': name,
                        'seed_fill_id': 0, 'seed_equity_id': 0, 'seed_event_rowid': 0,
                        'seed_equity': '1000', 'seed_realized_pnl': '0', 'seed_fees_paid': '0',
                        'seed_closed_ids': [], 'seed_positions': [], 'inherited_position_ids': []}))
                activate(ledger, p, exits['policy'], now=now, expected_account=manifest['accounts'][name],
                    expected_source=source_id, expected_entry_revision=trial['entry_revision'],
                    review_reference='Explicit isolated $25 size comparison; docs/RADEON_SIZING_COMPARISON.md')
            finally:
                ledger.close()
        _atomic(folder / 'manifest.json', manifest)
        return manifest
    finally:
        primary.close(); tape.close(); source.close()


class Study:
    def __init__(self, root, *, clock=time.time, registry_loader=None):
        self.root = Path(root).resolve()
        self.folder = self.root / 'runtime/radeon-sizing'
        self.manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        m = self.manifest
        if (m['format'] != FORMAT or m['excluded_from_strategy'] is not True
                or set(m['accounts']) != set(VARIANTS)
                or len(set([m['source_account_id'], *m['accounts'].values()])) != 3):
            raise ValueError('Invalid sizing account boundaries')
        self.clock, self.portfolios = clock, {}
        self.tape = readonly(self.root / 'runtime/paper-comparison/observations.sqlite3')
        self.source = readonly(self.root / 'runtime/exploration/events.sqlite3')
        self.primary = readonly(self.root / 'runtime/exploration/ledger.sqlite3')
        try:
            tape_meta = json.loads(metadata(self.tape)['binding'])
            if (tape_meta['comparison_id'] != m['tape_comparison_id']
                    or tape_meta['source_account_id'] != m['source_account_id']
                    or tape_meta['source_id'] != m['source_id']
                    or metadata(self.source, 'evm_metadata')['identity'] != m['source_id']
                    or metadata(self.primary)['account_id'] != m['source_account_id']):
                raise ValueError('Sizing source identity changed')
            store = SimpleNamespace(db=self.source, identity=m['source_id'])
            for name in VARIANTS:
                p = RiskPolicy(**m['policies'][name])
                ledger = Ledger(self.folder / name / 'ledger.sqlite3', p.starting_cash_usd, require_existing=True)
                self.portfolios[name] = (ledger, None)
                if ledger._get('account_id') != m['accounts'][name] or ledger._get('sizing_binding') != json_text(m):
                    raise ValueError('Sizing ledger binding changed')
                kwargs = {'registry_loader': registry_loader} if registry_loader else {}
                engine = ParallelEntryEngine(ledger, p, entry_rules=m['entry_rules'], runtime=self.root / 'runtime',
                    store=store, clock=clock, read_only_source=True, **kwargs)
                self.portfolios[name] = (ledger, engine)
        except BaseException:
            self.close()
            raise

    def close(self):
        for ledger, _ in self.portfolios.values():
            ledger.close()
        for conn in (self.tape, self.source, self.primary):
            conn.close()

    def gap(self, ledger, key, reason, **details):
        now = int(self.clock())
        ledger.record_event(key, 'comparison_gap', {'reason': reason, **details}, now)

    def process(self, name, row, *, rpc, quote_request):
        """Network first; decision + cursor atomic. Exceptions retain input for retry."""
        from explore import sized_entry_quote, entry_account_blocker
        ledger, engine = self.portfolios[name]
        cursor = int(ledger._get('comparison_cursor'))
        if row['seq'] <= cursor:
            return
        payload = json.loads(row['payload'])
        if (row['seq'] != cursor + 1 or hashlib.sha256(json_text(
                [row['kind'], row['observed_at'], payload]).encode()).hexdigest() != row['digest']):
            raise ValueError('Input tape gap or changed content')
        now = int(self.clock())
        candidate = quote = evidence = None
        reason = None
        if row['kind'] == 'candidate':
            candidate = Candidate.from_dict(payload['candidate'])
            source_quote = MarketQuote.from_dict(payload['quote']) if payload.get('quote') else None
            reason = ('entry_deadline' if now >= self.manifest['entry_deadline'] else
                'predates_experiment' if row['observed_at'] < self.manifest['started_at'] else
                'source_entry_revision_changed' if candidate.metadata.get('entry_revision') != self.manifest['entry_revision'] else
                'source_route_missing' if source_quote is None else
                'candidate_evidence_expired' if not 0 <= now - candidate.safety.observed_at <= engine.policy.max_evidence_age_seconds else
                'existing_one_entry_per_cycle_rule' if ledger._get('comparison_last_buy_cycle') == str(payload['cycle']) else
                'source_manual_entry_halt' if metadata(self.primary).get('entry_halt') else
                'source_known_safety_failure' if metadata(self.primary).get('safety_rejection:' + candidate.asset_id.split(':')[-1]) else
                entry_account_blocker(engine, candidate.asset_id, now))
            if not reason:
                # Never treat a saved process's receipt or smaller quote as current evidence.
                checked = engine.confirm_proofs(signal_proofs(candidate.metadata['strategy_signals']), rpc)
                quote, evidence = sized_entry_quote(engine, lambda size, **kw:
                    quote_request(candidate.asset_id, source_quote.pool_id, size, **kw), clock=self.clock)
                candidate = replace(candidate, metadata={**candidate.metadata, 'canonical_checked_at': checked,
                    'comparison_input_seq': row['seq'], 'source_captured_at': row['captured_at']})
                if quote.pool_id != source_quote.pool_id or quote.asset_id != candidate.asset_id:
                    raise ValueError('Comparison quote substituted asset or pool')
        now = int(self.clock())
        with ledger.transaction():
            if row['kind'] == 'candidate':
                if reason:
                    self.gap(ledger, f'sizing-gap:{row["seq"]}', reason, candidate=candidate.event_id)
                else:
                    ledger.record_event(f'sizing-route:{row["seq"]}', 'comparison_quote_observation',
                        {'quote': quote.as_dict(), 'evidence': evidence, 'source_seq': row['seq']}, now)
                    result = engine.consider(candidate, quote, now)
                    if result['verdict'] == 'PAPER_BUY':
                        ledger._set('comparison_last_buy_cycle', str(payload['cycle']))
            elif row['kind'] == 'gap':
                self.gap(ledger, f'sizing-source-gap:{row["seq"]}', 'source_gap', source=payload)
            # Primary watch quotes are deliberately not used for a different position size.
            ledger.record_event(f'sizing-input:{row["seq"]}', 'comparison_input',
                {'seq': row['seq'], 'digest': row['digest'], 'source_observed_at': row['observed_at'],
                 'captured_at': row['captured_at'], 'processed_at': now}, now)
            ledger._set('comparison_cursor', str(row['seq']))

    def publish(self):
        now = int(self.clock())
        high = self.tape.execute('SELECT COALESCE(MAX(seq),0) FROM observations').fetchone()[0]
        results = {}
        for name, (ledger, engine) in self.portfolios.items():
            engine.report(now)
            cursor = int(ledger._get('comparison_cursor'))
            if cursor > high:
                raise ValueError('Source tape rolled back behind committed cursor')
            results[name] = {**report(ledger, now), 'cursor': cursor, 'backlog': high - cursor,
                             'policy': engine.policy.as_dict()}
        result = {**self.manifest, 'as_of': now, 'variants': results}
        _atomic(self.folder / 'report.json', result)
        rows = []
        stamp = lambda value: datetime.fromtimestamp(value, ZoneInfo('Europe/Berlin')).strftime('%d %b %Y, %H:%M:%S %Z')
        for name, r in results.items():
            cells = [name, r['net_pnl'], r['realized_pnl'], r['modeled_costs'], r['max_observed_drawdown_pct'],
                     r['new_entries'], r['closed_win_rate_denominator'], len(r['unresolved_positions']), r['backlog']]
            rows.append('<tr>' + ''.join('<td>' + html.escape(str(c)) + '</td>' for c in cells) + '</tr>')
        _atomic_text(self.folder / 'report.html', '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radeon size study</title>'
            '<link rel="stylesheet" href="/sizing.css"><link rel="icon" href="/favicon.svg" type="image/svg+xml">'
            '<h1>Radeon · $25 paper sizing study</h1><p>Fresh $1,000 control and $1,000 size variant. '
            'Control: up to $5 per position / $20 aggregate. Variant: up to $25 / $100. '
            'Both retain a 2% daily equity-loss entry halt and identical bound entry/runner exits. '
            'Limits include modeled costs and reserves and can shrink with equity.</p><p>Updated '
            + stamp(now) + '. Entries stop ' + stamp(self.manifest['entry_deadline'])
            + '.</p><p><a href="/radeon-sizing">Refresh report</a> · <a href="/api/radeon-sizing" download>Full metrics JSON</a></p><section><table><tr>'
            '<th>Portfolio</th><th>Conservative P&amp;L</th><th>Realized</th><th>Fill costs</th><th>Drawdown %</th>'
            '<th>Entries</th><th>Closed</th><th>Unresolved</th><th>Backlog</th></tr>' + ''.join(rows)
            + '</table></section><p>' + html.escape(SCOPE) + '</p><p>Zero samples do not show an edge. '
            'Unknown marks retain holdings. Observed drawdown is not a continuous market path.</p></html>')
        return result
