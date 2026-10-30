"""Separate prospective Radeon $25 PAPER comparison; no primary account writes."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from supervise import instance_lock
from sniper.sizing_comparison import Study, initialize
from sniper.paper_comparison import _atomic
from sniper.adapters.evm import RpcClient, DEFAULT_RPC
from sniper.adapters.solana import DexScreenerClient, request_budget
from sniper.adapters.v3_route import V3RouteClient


def run(root, *, intake_wait_seconds=55, sleep=time.sleep):
    root = Path(root)
    runtime, folder = root / 'runtime', root / 'runtime/radeon-sizing'
    previous = json.loads((folder / 'status.json').read_text()) if (folder / 'status.json').exists() else {}
    started, deadline = int(time.time()), time.time() + 100
    status = {'started_at': started, 'finished_at': None, 'errors': [], 'processed': {},
              'mode': 'ISOLATED_PAPER_COMPARISON', 'paid_calls': 0, 'signing_calls': 0, 'broadcast_calls': 0,
              'retry_at': previous.get('retry_at', 0)}
    _atomic(folder / 'status.json', status)
    def endpoint(name, fallback):
        p = runtime / name
        return p.read_text(encoding='utf-8-sig').strip() if p.exists() else fallback
    rpc = RpcClient(endpoint('observation-rpc-url.txt', DEFAULT_RPC), timeout=8, max_retries=0)
    quote_url = endpoint('quote-rpc-url.txt', rpc.url)
    primary = json.loads((runtime / 'exploration/status.json').read_text(encoding='utf-8'))
    quote_retry = max(status['retry_at'], primary.get('quote_retry_at', 0))
    source_retry = max(status['retry_at'], primary.get('source_retry_at', 0))
    def refresh_backoff():
        nonlocal quote_retry, source_retry
        # Intake can now remain alive while a primary cycle publishes new
        # backoff. Re-read the local view before making another provider call.
        current = json.loads((runtime / 'exploration/status.json').read_text(encoding='utf-8'))
        quote_retry = max(quote_retry, status['retry_at'], current.get('quote_retry_at', 0))
        source_retry = max(source_retry, status['retry_at'], current.get('source_retry_at', 0))
    def route(asset, pool, size, *, deadline=None, quantity=Decimal(0)):
        refresh_backoff()
        if time.time() < quote_retry:
            raise RuntimeError('Quote provider backoff')
        route_rpc = RpcClient(quote_url, timeout=8, max_retries=0)
        client = V3RouteClient(route_rpc, DexScreenerClient(chain_id='robinhood'))
        end = min(deadline or time.time() + 15, overall_deadline)
        with request_budget(end, clock=time.time), route_rpc.budget(max(.001, end-time.time())):
            q = client.quote(asset.split(':')[-1], pool, max_notional=size,
                             sell_quantity=quantity, **({'exit_only': True} if quantity > 0 else {}))
        return q, client.last_evidence
    def failure(study, ledger, key, exc):
        nonlocal quote_retry, source_retry
        # Provider URLs can contain bearer material; retain only typed errors.
        status['errors'].append({'scope': key, 'type': type(exc).__name__})
        if '429' in str(exc):
            status['retry_at'] = int(time.time()) + 60
            quote_retry = source_retry = status['retry_at']
        study.gap(ledger, f'sizing-provider:{started}:{key}', 'provider_or_budget_failure', error_type=type(exc).__name__)
    overall_deadline = deadline
    study = Study(root, clock=time.time)
    try:
        # Alternate processing order; sequential quotes are never simultaneous-fill evidence.
        names = list(study.portfolios)
        if (started // 60) % 2:
            names.reverse()
        # All exits precede entry work and do not depend on source/model admission.
        for name in names:
            ledger, engine = study.portfolios[name]
            quotes = {}
            positions = ledger.positions()
            offset = int(ledger._get('sizing_watch_offset') or 0)
            ordered = positions[offset % len(positions):] + positions[:offset % len(positions)] if positions else []
            attempted = 0
            for p in ordered[:2]:
                if time.time() > deadline - 45 or time.time() < quote_retry:
                    break
                attempted += 1
                try:
                    q, evidence = route(p['asset_id'], p['pool_id'], Decimal(25), quantity=p['qty'])
                    quotes[p['asset_id']] = q
                    ledger.record_event(f'sizing-watch:{started}:{p["position_id"]}', 'comparison_quote_observation',
                        {'quote': q.as_dict(), 'evidence': evidence}, int(time.time()))
                except Exception as exc:
                    failure(study, ledger, name + ':' + p['position_id'], exc)
            engine.watch(quotes, int(time.time()))
            with ledger.transaction():
                ledger._set('sizing_watch_offset', str(offset + attempted))
        # A single snapshot followed by exit could add almost a minute to
        # candidates captured with only seconds left on their purchase proofs.
        # Watch the local tape for a bounded part of this existing minute job.
        # Empty polls make no provider requests. Per-run limits span all polls.
        intake_until = min(started + max(0, min(55, intake_wait_seconds)), deadline - 5)
        candidate_counts = {name: 0 for name in names}
        status['processed'] = {name: 0 for name in names}
        status['intake_polls'] = 0
        status['intake_wait_seconds'] = max(0, intake_until - started)
        failed = set()
        while True:
            status['intake_polls'] += 1
            for name in names:
                if name in failed:
                    continue
                ledger, _ = study.portfolios[name]
                remaining = 500 - status['processed'][name]
                rows = study.tape.execute('SELECT * FROM observations WHERE seq>? ORDER BY seq LIMIT ?',
                    (int(ledger._get('comparison_cursor')), remaining)).fetchall()
                for row in rows:
                    if time.time() > deadline - 5:
                        break
                    if row['kind'] == 'candidate':
                        refresh_backoff()
                        if candidate_counts[name] or time.time() < max(source_retry, quote_retry, status['retry_at']):
                            break
                        candidate_counts[name] += 1
                    try:
                        with request_budget(deadline - 3, clock=time.time), rpc.budget(max(.001, deadline-3-time.time())):
                            study.process(name, row, rpc=rpc, quote_request=route)
                        status['processed'][name] += 1
                    except Exception as exc:
                        failure(study, ledger, name + ':input:' + str(row['seq']), exc)
                        failed.add(name)
                        break
            if (time.time() >= intake_until or time.time() >= deadline - 5
                    or all(name in failed or candidate_counts[name] or status['processed'][name] >= 500
                           for name in names)):
                break
            sleep(min(.5, max(0, intake_until - time.time())))
        study.publish()
        status['finished_at'] = int(time.time())
        status['duration_seconds'] = time.time() - started
        _atomic(folder / 'status.json', status)
    finally:
        study.close()
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'once', 'status'])
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    folder = args.root / 'runtime/radeon-sizing'
    if args.command == 'status':
        print((folder / 'report.json').read_text(encoding='utf-8'))
        return
    # Init lock lives outside the not-yet-created experiment directory.
    with instance_lock(args.root / 'runtime/radeon-sizing.lock'):
        if args.command == 'init':
            value = initialize(args.root)
            study = Study(args.root)
            try:
                study.publish()
            finally:
                study.close()
        else:
            value = run(args.root)
        print(json.dumps(value, default=str))


if __name__ == '__main__':
    main()
