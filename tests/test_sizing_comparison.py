"""Isolated real admission, sizing, exits and restart; zero external calls."""
from dataclasses import replace
from contextlib import ExitStack, nullcontext
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sniper.ledger import json_text
from sniper.parallel_entry import signal_proofs
from sniper.radeon_exits import activate, RUNNER, binding
from sniper.sizing_comparison import Study, initialize
import test_parallel_entry_engine as fixture_module
from test_parallel_entry_engine import CanonicalRpc, ASSET, POOL
from test_engine import quote


class SizingComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = fixture_module.ParallelEntryEngineTests()
        self.fixture.setUp()
        f = self.fixture
        self.now = f.now
        self.candidate = f.candidate()
        trial = {'account_id': f.ledger._get('account_id'), 'source_id': f.store.identity,
                 'started_at': self.now-100, 'ends_at': self.now+604800,
                 'entry_revision': f.engine.entry_revision, 'entry_rules': f.rules,
                 'policy_version': f.policy.version}
        f.ledger._set('trial_manifest', json_text(trial))
        activate(f.ledger, f.policy, RUNNER, now=self.now, expected_account=trial['account_id'],
                 expected_source=trial['source_id'], expected_entry_revision=trial['entry_revision'], review_reference='TEST')
        folder = self.root / 'runtime/exploration'
        folder.mkdir(parents=True)
        for name, source in [('ledger.sqlite3', f.ledger.conn), ('events.sqlite3', f.store.db)]:
            conn = sqlite3.connect(folder / name)
            source.backup(conn)
            conn.close()
        tape_folder = self.root / 'runtime/paper-comparison'
        tape_folder.mkdir()
        self.tape = sqlite3.connect(tape_folder / 'observations.sqlite3', isolation_level=None)
        self.tape.row_factory = sqlite3.Row
        self.tape.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE observations('
            'seq INTEGER PRIMARY KEY,event_key TEXT,observed_at INTEGER,captured_at REAL,kind TEXT,payload TEXT,digest TEXT);')
        self.tape.execute('INSERT INTO metadata VALUES(?,?)', ('binding', json_text({
            'comparison_id': 'test-tape', 'source_account_id': trial['account_id'], 'source_id': trial['source_id']})))
        self.manifest = initialize(self.root, now=self.now)
        self.study = self.open()
        self.requests = []

    def open(self):
        return Study(self.root, clock=lambda: self.now, registry_loader=lambda *_: self.fixture.registry)

    def tearDown(self):
        self.study.close()
        self.tape.close()
        self.fixture.tearDown()
        self.temp.cleanup()

    def request(self, asset, pool, size, **kwargs):
        self.requests.append((asset, pool, size))
        return quote(asset, self.now, pool_id=pool, route_max_notional_usd=size,
                     route_max_sell_quantity=size, route_impact_bps=D(5)), {'buy_input_usd': str(size)}

    def row(self, candidate=None, cycle=1):
        candidate = candidate or self.candidate
        payload = {'candidate': candidate.as_dict(), 'quote': self.fixture.quote().as_dict(), 'cycle': cycle}
        seq = self.tape.execute('SELECT COALESCE(MAX(seq),0)+1 FROM observations').fetchone()[0]
        digest = hashlib.sha256(json_text(['candidate', self.now, payload]).encode()).hexdigest()
        self.tape.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',
            (seq, 'input:'+str(seq), self.now, self.now, 'candidate', json_text(payload), digest))
        return self.tape.execute('SELECT * FROM observations WHERE seq=?', (seq,)).fetchone()

    def process(self, name, row):
        self.study.process(name, row, rpc=CanonicalRpc(), quote_request=self.request)

    def test_fresh_accounts_same_entries_and_exits_separate_sized_routes(self):
        row = self.row()
        primary_before = list(self.study.primary.iterdump())
        for name, cap in [('control', D(5)), ('usd25', D(25))]:
            self.process(name, row)
            ledger, engine = self.study.portfolios[name]
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)
            p = ledger.positions()[0]
            self.assertLessEqual(p['remaining_basis']+p['reserved_exit_cost'], cap)
            self.assertGreater(p['remaining_basis'], cap * D('.9'))
            self.assertEqual(binding(ledger.conn, p['position_id'])['policy'], RUNNER)
        self.assertTrue(any(size > 20 for _, _, size in self.requests))
        self.assertTrue(any(size <= 5 for _, _, size in self.requests))
        self.assertEqual(primary_before, list(self.study.primary.iterdump()))
        with self.assertRaises(sqlite3.OperationalError):
            self.study.primary.execute("UPDATE metadata SET value='wrong' WHERE key='cash'")
        report = self.study.publish()
        self.assertEqual(report['variants']['usd25']['new_entries'], 1)
        self.assertGreater(D(report['variants']['usd25']['modeled_costs']), 0)

    def test_restart_duplicate_cursor_cannot_repeat_fill(self):
        row = self.row()
        self.process('usd25', row)
        self.study.close(); self.study = self.open()
        self.process('usd25', row)
        ledger, _ = self.study.portfolios['usd25']
        self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)

    def test_rolled_back_cursor_also_rolls_back_fill(self):
        row = self.row()
        ledger, _ = self.study.portfolios['usd25']
        original = ledger._set
        def fail(key, value):
            if key == 'comparison_cursor':
                raise RuntimeError('simulated failed commit')
            original(key, value)
        ledger._set = fail
        with self.assertRaises(RuntimeError): self.process('usd25', row)
        ledger._set = original
        self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
        self.process('usd25', row)
        self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)

    def test_route_failure_retains_cursor_then_recovers(self):
        row = self.row()
        def fail(*args, **kw): raise OSError('mock outage')
        with self.assertRaises(OSError):
            self.study.process('usd25', row, rpc=CanonicalRpc(), quote_request=fail)
        ledger, _ = self.study.portfolios['usd25']
        self.assertEqual(ledger._get('comparison_cursor'), '0')
        self.process('usd25', row)
        self.assertEqual(len(ledger.positions()), 1)

    def test_old_evidence_records_gap_without_network_or_fill(self):
        row = self.row(); self.now += 301
        self.process('usd25', row)
        self.assertEqual(self.requests, [])
        ledger, _ = self.study.portfolios['usd25']
        self.assertEqual(ledger.positions(), [])
        self.assertEqual(ledger._get('comparison_cursor'), '1')

    def test_fresh_safety_with_expired_purchases_advances_once_without_network(self):
        proofs = signal_proofs(self.candidate.metadata['strategy_signals'])
        self.now = max(p['observed_at'] for p in proofs) + 901
        candidate = replace(self.candidate, safety=replace(self.candidate.safety, observed_at=self.now))
        row = self.row(candidate)
        primary_before = list(self.study.primary.iterdump())
        for name in ('control', 'usd25'):
            with patch.object(self.study.portfolios[name][1], 'confirm_proofs',
                              side_effect=AssertionError('Expired proof must not use RPC')):
                self.process(name, row)
            ledger, _ = self.study.portfolios[name]
            self.assertEqual(ledger._get('comparison_cursor'), '1')
            self.assertEqual(ledger.positions(), [])
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            reason = json.loads(ledger.conn.execute("SELECT payload FROM events WHERE event_id='sizing-gap:1'").fetchone()[0])
            self.assertEqual(reason['reason'], 'candidate_signal_expired')
        self.assertEqual(self.requests, [])
        self.assertEqual(primary_before, list(self.study.primary.iterdump()))
        self.study.close(); self.study = self.open()
        self.process('usd25', row)
        self.assertEqual(self.study.portfolios['usd25'][0].conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id='sizing-gap:1'").fetchone()[0], 1)

    def test_purchase_at_exact_confirmation_boundary_still_gets_checked(self):
        proofs = signal_proofs(self.candidate.metadata['strategy_signals'])
        self.now = min(p['observed_at'] for p in proofs) + 900
        candidate = replace(self.candidate, safety=replace(self.candidate.safety, observed_at=self.now))
        engine = self.study.portfolios['control'][1]
        with patch.object(engine, 'confirm_proofs', wraps=engine.confirm_proofs) as confirm:
            self.process('control', self.row(candidate))
            confirm.assert_called_once()
        self.assertTrue(self.requests)

    def test_wrong_pool_quote_is_not_substituted(self):
        row = self.row()
        request = self.request
        def wrong(asset, pool, size, **kw):
            q, evidence = request(asset, pool, size, **kw)
            return replace(q, pool_id='0x'+'9'*40), evidence
        with self.assertRaisesRegex(ValueError, 'substituted'):
            self.study.process('usd25', row, rpc=CanonicalRpc(), quote_request=wrong)
        self.assertEqual(self.study.portfolios['usd25'][0].positions(), [])

    def test_ladder_uses_original_shares_and_unknown_quotes_retain_holdings(self):
        self.process('usd25', self.row())
        ledger, engine = self.study.portfolios['usd25']; p = ledger.positions()[0]
        for stage, factor in enumerate(('2.1', '4.1', '7.1'), 1):
            self.now += 1
            q = quote(ASSET, self.now, pool_id=POOL, price=str(p['entry_price']*D(factor)),
                      route_side='sell', route_max_sell_quantity=p['qty0'], route_max_notional_usd=D(500), route_impact_bps=D(5))
            fills = engine.watch({ASSET:q}, self.now)
            self.assertEqual(fills[0]['reason'], 'take_profit_'+str(stage))
        self.assertEqual(ledger.positions()[0]['qty'], p['qty0']/2)
        self.now += 100
        engine.watch({}, self.now)
        self.assertEqual(ledger.positions()[0]['qty'], p['qty0']/2)
        self.assertEqual(len(self.study.publish()['variants']['usd25']['unresolved_positions']), 1)

    def test_manifest_tampering_and_reinitialization_refused(self):
        with self.assertRaises(ValueError): initialize(self.root, now=self.now)
        path = self.root/'runtime/radeon-sizing/manifest.json'
        value = json.loads(path.read_text()); value['entry_deadline'] += 100
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'binding changed'): self.open()

    def test_source_reorg_cannot_admit_saved_proof(self):
        row = self.row(); rpc = CanonicalRpc(); rpc.hash = '0x'+'9'*64
        with self.assertRaises(Exception): self.study.process('usd25', row, rpc=rpc, quote_request=self.request)
        self.assertEqual(self.requests, [])

    def test_deadline_stops_entries(self):
        row = self.row(); self.now = self.manifest['entry_deadline']
        self.process('usd25', row)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.study.portfolios['usd25'][0].positions(), [])

    def test_input_digest_failure_cannot_advance_cursor(self):
        row = dict(self.row()); row['digest'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'changed content'): self.process('usd25', row)
        self.assertEqual(self.study.portfolios['usd25'][0]._get('comparison_cursor'), '0')

    def test_empty_worker_finishes_without_provider_calls(self):
        import radeon_sizing
        (self.root/'runtime/exploration/status.json').write_text('{}')
        self.study.close(); self.study = self.open()
        with patch.object(radeon_sizing.time, 'time', return_value=self.now), patch.object(radeon_sizing, 'RpcClient') as rpc:
            rpc.return_value.url = 'https://example.invalid'
            result = radeon_sizing.run(self.root, intake_wait_seconds=0)
            rpc.return_value.call.assert_not_called()
        self.assertIsInstance(result['finished_at'], int)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['processed'], {'control': 0, 'usd25': 0})

    def run_waiting_worker(self, on_sleep=None, wait=55):
        """Real study/engine with a virtual clock and explicitly mocked providers."""
        import radeon_sizing as worker
        owner = self
        tick = [float(self.now)]
        (self.root/'runtime/exploration/status.json').write_text('{}')
        rpc = CanonicalRpc()
        rpc.url = 'https://example.invalid'
        rpc.budget = lambda *_: nullcontext()
        class Route:
            def __init__(self, *_): self.last_evidence = {}
            def quote(self, token, pool, *, max_notional, **kwargs):
                q, self.last_evidence = owner.request('eip155:4663/erc20:'+token, pool, max_notional)
                return q
        def sleep(seconds):
            tick[0] += seconds
            owner.now = int(tick[0])
            if on_sleep: on_sleep(tick[0])
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker.time, 'time', lambda: tick[0]))
            stack.enter_context(patch.object(worker, 'RpcClient', return_value=rpc))
            stack.enter_context(patch.object(worker, 'V3RouteClient', Route))
            stack.enter_context(patch.object(worker, 'Study', side_effect=lambda root, clock:
                Study(root, clock=clock, registry_loader=lambda *_: {**self.fixture.registry,
                    'checked_at': self.now, 'updated_at': self.now})))
            network = stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('No external calls')))
            result = worker.run(self.root, intake_wait_seconds=wait, sleep=sleep)
            network.assert_not_called()
        return result, rpc

    def test_candidate_arriving_after_start_is_admitted_before_purchase_expiry(self):
        # The original one-shot worker exited before this input existed. A
        # minute-later invocation would find its purchase proof expired.
        proof_at = min(p['observed_at'] for p in signal_proofs(self.candidate.metadata['strategy_signals']))
        self.now = proof_at + 893
        start = self.now
        primary_before = list(self.study.primary.iterdump())
        captured = []
        def arrive(now):
            if now >= start + 2 and not captured:
                c = replace(self.candidate, safety=replace(self.candidate.safety, observed_at=self.now),
                    metadata={**self.candidate.metadata, 'decision_at': self.now})
                captured.append(self.row(c))
        result, rpc = self.run_waiting_worker(arrive)
        self.assertEqual(result['errors'], [])
        self.assertGreater(result['intake_polls'], 1)
        self.assertLess(result['finished_at'] - start, 7)
        self.assertTrue(rpc.calls)
        for ledger, _ in self.study.portfolios.values():
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)
            event = json.loads(ledger.conn.execute("SELECT payload FROM events WHERE event_id='sizing-input:1'").fetchone()[0])
            self.assertLessEqual(event['processed_at'] - event['captured_at'], 1)
        self.assertEqual(primary_before, list(self.study.primary.iterdump()))

    def test_arrival_after_purchase_expiry_is_still_a_gap_without_provider_calls(self):
        proof_at = max(p['observed_at'] for p in signal_proofs(self.candidate.metadata['strategy_signals']))
        self.now = proof_at + 900
        captured = []
        def arrive(now):
            if self.now > proof_at + 900 and not captured:
                c = replace(self.candidate, safety=replace(self.candidate.safety, observed_at=self.now),
                    metadata={**self.candidate.metadata, 'decision_at': self.now})
                captured.append(self.row(c))
        result, rpc = self.run_waiting_worker(arrive)
        self.assertEqual(result['processed'], {'control': 1, 'usd25': 1})
        self.assertEqual(rpc.calls, [])
        self.assertEqual(self.requests, [])
        for ledger, _ in self.study.portfolios.values():
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            gap = json.loads(ledger.conn.execute("SELECT payload FROM events WHERE event_id='sizing-gap:1'").fetchone()[0])
            self.assertEqual(gap['reason'], 'candidate_signal_expired')

    def test_empty_polling_is_bounded_and_never_calls_providers(self):
        start = self.now
        result, rpc = self.run_waiting_worker(wait=500)
        self.assertEqual(result['finished_at'] - start, 55)
        self.assertGreater(result['intake_polls'], 1)
        self.assertEqual(result['processed'], {'control': 0, 'usd25': 0})
        self.assertEqual(rpc.calls, [])
        self.assertEqual(self.requests, [])

    def test_slow_child_start_does_not_extend_scheduled_wait_window(self):
        start = self.now
        wrapper = self.root/'runtime/desktop/radeon-sizing.json'
        wrapper.parent.mkdir()
        wrapper.write_text(json.dumps({'job': 'radeon-sizing', 'phase': 'STARTING_CHILD',
            'started_at': start-10, 'finished_at': None}))
        result, rpc = self.run_waiting_worker()
        self.assertEqual(result['finished_at'], start+45)
        self.assertEqual(result['intake_wait_seconds'], 45)
        self.assertEqual(rpc.calls, [])

    def test_completed_or_stale_wrapper_cannot_change_wait_window(self):
        wrapper = self.root/'runtime/desktop/radeon-sizing.json'
        wrapper.parent.mkdir()
        for phase, age, finished in [('COMPLETE', 10, self.now), ('STARTING_CHILD', 111, None)]:
            start = self.now
            wrapper.write_text(json.dumps({'job': 'radeon-sizing', 'phase': phase,
                'started_at': start-age, 'finished_at': finished}))
            result, rpc = self.run_waiting_worker()
            self.assertEqual(result['finished_at'], start+55)
            self.assertEqual(rpc.calls, [])

    def test_new_primary_backoff_during_wait_retains_candidate_without_calls(self):
        start = self.now
        captured = []
        def arrive(now):
            if not captured:
                captured.append(self.row())
                (self.root/'runtime/exploration/status.json').write_text(json.dumps({'source_retry_at': start+60}))
        result, rpc = self.run_waiting_worker(arrive)
        self.assertEqual(result['processed'], {'control': 0, 'usd25': 0})
        self.assertEqual(rpc.calls, [])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.study.portfolios['control'][0]._get('comparison_cursor'), '0')

    def test_one_candidate_per_account_limit_survives_multiple_polls(self):
        arrived = []
        def arrive(now):
            if not arrived:
                arrived.extend([self.row(), self.row(replace(self.candidate, event_id='second'), cycle=2)])
        result, _ = self.run_waiting_worker(arrive)
        self.assertEqual(result['processed'], {'control': 1, 'usd25': 1})
        for ledger, _ in self.study.portfolios.values():
            self.assertEqual(ledger._get('comparison_cursor'), '1')
            self.assertEqual(ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 1)

    def test_provider_failure_is_not_retried_in_poll_loop(self):
        arrived = []
        def arrive(now):
            if not arrived: arrived.append(self.row())
        with patch.object(Study, 'process', side_effect=OSError('mock outage')) as process:
            result, rpc = self.run_waiting_worker(arrive)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(len(result['errors']), 2)
        self.assertEqual(rpc.calls, [])
        for ledger, _ in self.study.portfolios.values():
            self.assertEqual(ledger._get('comparison_cursor'), '0')


if __name__ == '__main__': unittest.main()
