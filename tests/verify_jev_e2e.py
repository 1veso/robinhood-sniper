"""Real adapter -> loopback provider -> journal -> paper watcher -> HTTP board.

All market/model evidence is synthetic and all accounts are new isolated fixtures.
No secrets are loaded. Non-loopback DNS and socket connections fail closed.
This verifies integration, not TypeSafe availability, model quality or live trading.
"""
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch
import urllib.error

SOURCE = Path(__file__).resolve().parents[1]
SCOPE = 'ISOLATED_SYNTHETIC_E2E_EXCLUDED_FROM_ALL_STRATEGIES'


@contextmanager
def loopback_only():
    counts = {'external_attempts_blocked': 0, 'loopback_connections': 0}
    connect, connect_ex, lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def allowed(host):
        if host not in ('127.0.0.1', '::1', 'localhost'):
            counts['external_attempts_blocked'] += 1
            raise AssertionError('E2E forbids non-loopback networking')

    def guarded(original):
        def call(sock, address):
            allowed(address[0]); counts['loopback_connections'] += 1
            return original(sock, address)
        return call

    def resolve(host, *args, **kwargs):
        allowed(host)
        return lookup(host, *args, **kwargs)

    with patch.object(socket.socket, 'connect', guarded(connect)), \
         patch.object(socket.socket, 'connect_ex', guarded(connect_ex)), \
         patch.object(socket, 'getaddrinfo', resolve):
        yield counts


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


class LocalProvider:
    def __init__(self, response_factory):
        self.reply = response_factory
        self.requests = []
        self.behavior = 'buy'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                assert self.path == '/v1/systemone'
                assert self.headers['Authorization'] == 'Bearer fixture-not-a-real-key'
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(payload)
                behavior = owner.behavior
                if behavior == 'timeout':
                    time.sleep(.15)
                body = owner.reply(behavior if behavior in ('buy', 'watch', 'pass') else 'buy')
                if behavior == 'malformed':
                    body['answers']['action']['probabilities']['buy'] = 'invalid-number'
                raw = json.dumps(body).encode()
                self.send_response(503 if behavior == 'error' else 200)
                self.send_header('Content-Length', str(len(raw))); self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)

    def transport(self, method, url, payload, headers, timeout):
        from sniper.jev import MODEL, QUESTIONS, URL
        assert (method, url, timeout) == ('POST', URL, 12)
        assert payload['model'] == MODEL and payload['questions'] == QUESTIONS
        text = json.dumps(payload)
        assert 'fixture-not-a-real-key' not in text
        assert 'verdict' not in json.dumps(payload['state'])
        assert 'judgment' not in json.dumps(payload['state'])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port,
                                               timeout=.04 if self.behavior == 'timeout' else 5)
        try:
            connection.request(method, '/v1/systemone', text, headers)
            reply = connection.getresponse()
            raw = reply.read()
            if reply.status != 200:
                raise urllib.error.HTTPError(url, reply.status, 'fixture status', {}, None)
            return json.loads(raw)
        finally:
            connection.close()


def readback(root):
    # A new interpreter reads SQLite only; it never imports a model or account writer.
    path = root / 'runtime/jev-review/assessments.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return dict(db.execute('SELECT id,status FROM assessments'))


def execute(app_root, output):
    sys.path[:0] = [str(app_root / 'src'), str(app_root / 'tests'), str(app_root)]
    from test_jev import fixture, response, Book, check, normalize, raw_report, quote, MINT, BUY
    from test_jev_promotion import app_fixture, acceptance, enrich
    from sniper.jev import JevClient, MODEL, gate
    from sniper.jev_dossier import build, encode, validate
    from sniper.jev_review import Journal, run, detail, DEFAULT_SETTINGS
    from sniper.jev_lifecycle import record, CODE_FILES
    from sniper.console import ConsoleData, make_server
    from rug_desk.watch import watch
    from rug_desk.report import write_report

    now = int(time.time())
    checks, traces = {}, {}

    def passed(name, evidence):
        checks[name] = {'status': 'PASS', 'evidence': evidence}

    def market_at(at, price='1'):
        return Mock(quote=Mock(return_value={**quote(now=at, price=price),
                    'created_at_ms': (now - 3600) * 1000, 'sol_usd': '200', 'sol_observed_at': at}))

    def providers(at):
        return {'market': market_at(at),
                'rugcheck': Mock(report=Mock(return_value=normalize(MINT, raw_report(), at))),
                'judge': Mock(ask=Mock(return_value=BUY)), 'clock': lambda: at}

    def synthetic(dossier):
        enrich(dossier)
        dossier['limitations'].append(SCOPE + ': history, independence and exit capacity are fixtures.')
        return dossier

    def board(root, identifier):
        with patch.object(ConsoleData, 'jobs', return_value={'available': False, 'items': [],
                          'checked_at': None, 'scope': 'isolated fixture'}), serving(make_server(root, port=0)) as port:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            try:
                values = {}
                for name, url in (('board', '/api/state?bot=jev'),
                                  ('detail', '/api/jev-assessment?id=' + identifier),
                                  ('baseline_board', '/api/state?bot=solana')):
                    connection.request('GET', url)
                    reply = connection.getresponse(); body = reply.read()
                    assert reply.status == 200, (name, reply.status, body)
                    values[name] = json.loads(body)
                connection.request('POST', '/api/jev-assessment?id=' + identifier, '{}')
                reply = connection.getresponse(); reply.read()
                assert reply.status in (405, 501)
                return values
            finally:
                connection.close()

    provider = LocalProvider(response)
    with loopback_only() as network, serving(provider.server):
        client = JevClient('fixture-not-a-real-key', provider.transport)
        shadow = output / 'shadow'
        book = app_fixture(shadow, now)
        try:
            entry = check(book, MINT, **providers(now))
            assert entry['entry'] and len(book.fills()) == 1
            before = book.fills()
            collected = run(shadow, now=now)
            assert collected['orders_created'] == 0 and not collected['external_calls_enabled']
            row = dict(book.db.execute('SELECT * FROM checks').fetchone())
            dossier, baseline = build('rug_desk', book.identity, row)
            assert baseline['eligible'] and validate(dossier, now)['missing_required']
            assert not provider.requests
            passed('candidate_worker_preserves_missing_evidence', collected)
            synthetic(dossier)
            provider.behavior = 'pass'
            journal = Journal(shadow)
            first = journal.assess(dossier, baseline, now=now + 1, client=client, clock=lambda: now + 1)
            journal.close()
            assert first['status'] == 'COMPLETE' and first['result']['comparison_disagreement']
            journal = Journal(shadow)
            again = journal.assess(dossier, baseline, now=now + 2, client=client)
            journal.close()
            assert first == again and len(provider.requests) == 1 and book.fills() == before
            assert check(book, MINT, **providers(now + 2))['stage'] == 'seen_once'
            # Disable the observation worker: it must not alter the baseline account.
            (shadow / 'runtime/jev-review/settings.json').write_text(encode({**DEFAULT_SETTINGS, 'enabled': False}))
            disabled = run(shadow, now=now + 2)
            assert not disabled['enabled'] and book.fills() == before
            missing = watch(book, market=Mock(quote=Mock(side_effect=TimeoutError())), clock=lambda: now + 2)
            assert book.positions() and book.fills() == before
            exits = watch(book, market=market_at(now + 3, '.4'), clock=lambda: now + 3)
            assert not book.positions() and len(book.fills()) == 2
            write_report(book, now + 3)
            traces['shadow'] = board(shadow, first['id'])
            assert traces['shadow']['detail'] == first
            assert traces['shadow']['board']['comparison']['exclusions']['late_assessment'] == 1
            assert not traces['shadow']['board']['comparison']['performance_available']
            assert any(d.get('jev', {}).get('id') == first['id'] for d in traces['shadow']['baseline_board']['decisions'])
            passed('shadow_duplicate_no_extra_order', {'assessment_id': first['id'], 'provider_calls': 1,
                   'baseline_entry_fills_before_and_after': 1, 'candidate_id': dossier['candidate_id']})
            passed('disabled_shadow_preserves_baseline_and_original_exits', {'missing_price': missing, 'original_exit': exits})
            passed('linked_read_only_board_and_no_lookahead', {'assessment_id': first['id'], 'late_assessment_excluded': True})
        finally:
            book.close()

        active = output / 'active-paper'
        book = app_fixture(active, now)
        try:
            # Synthetic transition evidence belongs only to this isolated account.
            evidence = acceptance(active, book.identity['account_id'], now)
            (active / 'runtime/jev-review/paper-acceptance.json').write_text(encode(evidence))
            record(active, 'ACTIVE_PAPER', evidence, SCOPE, paper_account_id=book.identity['account_id'], now=now)
            provider.behavior = 'buy'
            count = len(provider.requests)
            entry = check(book, MINT, **providers(now), jev_client=client, jev_enrich=synthetic)
            assert entry['entry'], entry
            assessment_id = entry['jev']['assessment_id']
            saved = detail(active, assessment_id)
            assert saved['finished'] <= book.positions()[0]['opened_at']
            assert check(book, MINT, **providers(now + 1), jev_client=client, jev_enrich=synthetic)['stage'] == 'seen_once'
            assert len(provider.requests) == count + 1 and len(book.fills()) == 1
            watch(book, market=market_at(now + 2, '.4'), clock=lambda: now + 2)
            assert not book.positions() and len(book.fills()) == 2
            position = json.loads(book.db.execute('SELECT payload FROM positions').fetchone()[0])
            write_report(book, now + 2)
            traces['active_paper'] = board(active, assessment_id)
            comparison = traces['active_paper']['board']['comparison']
            assert comparison['performance_available'] and len(comparison['records']) == 1
            paired = comparison['records'][0]
            assert paired['retained']
            assert Decimal(paired['baseline_net_pnl']) == Decimal(paired['filtered_net_pnl']) == Decimal(position['realized_pnl'])
            assert position['execution_model'] == book.cost_policy()['model']
            assert all(f['execution_costs']['model'] == book.cost_policy()['model'] for f in book.fills())
            assert sum(Decimal(f['fee']) for f in book.fills()) > 0
            traces['active_paper']['fills'] = book.fills()
            passed('prospective_paper_entry_exit_and_cost_matched_comparison', {
                'account_id': book.identity['account_id'], 'position_id': position['id'],
                'assessment_id': assessment_id, 'fills': 2, 'comparison': comparison})
            a = saved['result']['response']['answers']
            assert a['organic']['noul'] == .8 and a['action']['confidence'] == .25
            assert a['action']['probabilities']['buy'] == .8 and a['exit_risk']['score'] == 1.5
            assert len(a['exit_risk']['probabilities']) == 5
            assert gate(response(score=2))[0] == 'BUY' and gate(response(score=2.01))[0] == 'PASS'
            passed('typed_probabilities_and_fractional_score', a)
        finally:
            book.close()

        failure_root = output / 'failure-cases'
        journal = Journal(failure_root)

        def sample(label, at=now):
            d, b = fixture(at); synthetic(d)
            d['candidate_id'] += ':' + label; b['candidate_id'] = d['candidate_id']
            return d, b

        failures = {}
        try:
            for kind, status in (('missing', 'INSUFFICIENT_EVIDENCE'), ('invalid', 'INVALID_DATA'),
                                 ('hard', 'HARD_REJECTION'), ('disabled', 'DISABLED'),
                                 ('error', 'API_ERROR'), ('malformed', 'MALFORMED_RESPONSE'), ('timeout', 'TIMEOUT')):
                d, b = sample(kind)
                if kind == 'missing': d['observations']['holders_then'] = None
                if kind == 'invalid': d['observations']['independent_buyers']['value'] = 'unknown'
                if kind == 'hard': b['eligible'] = False
                provider.behavior = kind
                count = len(provider.requests)
                result = journal.assess(d, b, now=now, client=None if kind == 'disabled' else client, clock=lambda: now)
                assert result['status'] == status, result
                again = journal.assess(d, b, now=now + 1, client=client)
                expected = 1 if kind in ('error', 'malformed', 'timeout') else 0
                assert again['id'] == result['id'] and len(provider.requests) - count == expected
                failures[kind] = {'id': result['id'], 'status': status, 'loopback_calls': expected}
            provider.behavior = 'watch'
            d, b = sample('watch')
            first = journal.assess(d, b, now=now, client=client, clock=lambda: now)
            assert first['result']['proposed'] == 'WATCH'
            journal.close(); journal = Journal(failure_root)
            for offset, expected in ((600, 'REFRESH_BLOCKED'), (1800, 'COMPLETE'), (3600, 'REFRESH_BLOCKED')):
                d, b = sample('watch', now + offset)
                result = journal.assess(d, b, now=now + offset, client=client, clock=lambda: now + offset)
                assert result['status'] == expected, result
                if offset == 1800:
                    assert result['watch_count'] == 1 and result['result']['proposed'] == 'PASS'
            passed('failure_states_no_retry_and_hard_rejection', failures)
            passed('watch_refresh_survives_reopen_without_looping', {'first': first['id'], 'maximum_refreshes': 1})
        finally:
            journal.close()

        # A separate Python process verifies durability after all writers close.
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--readback', str(active)],
                               check=True, capture_output=True, text=True, timeout=20)
        restarted = json.loads(child.stdout)
        assert restarted[assessment_id] == 'COMPLETE'
        passed('fresh_process_persistence', restarted)
        assert network['external_attempts_blocked'] == 0
        result = {'scope': SCOPE, 'at': now, 'app_root': str(app_root), 'model_pin': MODEL,
                  'checks': checks, 'network': dict(network), 'loopback_model_requests': len(provider.requests),
                  'paid_calls': 0, 'external_provider_calls': 0, 'telegram_calls': 0,
                  'signatures': 0, 'broadcasts': 0, 'production_accounts_modified': False,
                  'code_sha256': {p: __import__('hashlib').sha256((app_root / p).read_bytes()).hexdigest() for p in CODE_FILES},
                  'limitations': ['TypeSafe authentication, availability and real inference NOT VERIFIED.',
                     'Complete holder/controller/social history and executable exit evidence are NOT CONNECTED.',
                     'No Pump launch feed; existing eligible Solana candidates only.',
                     'Synthetic fixtures are not strategy performance or Solana live readiness.']}
        (output / 'http-requests.json').write_text(encode(provider.requests), encoding='utf-8')
        (output / 'board-traces.json').write_text(encode(traces), encoding='utf-8')
        (output / 'verification.json').write_text(encode(result), encoding='utf-8')
        print(encode({'scope': SCOPE, 'checks_passed': len(checks), 'paid_calls': 0, 'report': str(output / 'verification.json')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--readback', type=Path)
    args = parser.parse_args()
    if args.readback:
        print(json.dumps(readback(args.readback.resolve()))); return
    if args.output is None:
        parser.error('--output is required')
    output = args.output.resolve()
    allowed = (SOURCE / 'runtime/jev-acceptance').resolve()
    if output == allowed or not output.is_relative_to(allowed) or output.exists():
        parser.error('A new isolated source runtime/jev-acceptance child is required')
    output.mkdir(parents=True)
    execute(args.app_root.resolve(), output)


if __name__ == '__main__':
    main()
