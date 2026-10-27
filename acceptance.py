"""Isolated live-quote paper round trip; validates execution plumbing, not a strategy."""
from dataclasses import replace
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sys
import sqlite3
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from sniper.adapters.evm import RpcClient, DEFAULT_RPC, address
from sniper.adapters.goplus import GoPlusClient
from sniper.adapters.crius import supplement_from_runtime
from sniper.adapters.solana import DexScreenerClient
from sniper.adapters.v3_route import V3RouteClient
from sniper.exploration import ExploratoryEngine, pool_adjusted_screen, observe_creator_balance, observed_pool_screen
from sniper.confirmation_book import ConfirmationBook
from sniper.exploratory_alerts import notify_fills
from sniper.ledger import Ledger
from sniper.models import Candidate, json_safe
from sniper.policy import RiskPolicy
from sniper.reporting import write_html
from sniper.rug_screen import enhance_rug_screen
from supervise import atomic_json, instance_lock


class ExecutionValidationEngine(ExploratoryEngine):
    engine_mode = 'execution_validation_v1'
    strategy_name = 'operator_execution_validation'
    confirmation_label = 'operator_test_triggers'

    def _confirmations(self, candidate, now):
        # This account has an explicit operator test trigger. Never manufacture
        # wallets or represent this as a wallet-strategy qualification test.
        return 1, None

    def decision_context(self, candidate):
        return {**super().decision_context(candidate), 'strategy_result': False,
                'trigger': 'operator_requested_execution_validation'}


def validate_cycle(runtime, token, pool, symbol, *, route, screen, clock=time.time,
                   notify=notify_fills):
    token, pool = address(token), address(pool)
    folder = runtime / 'acceptance'
    folder.mkdir(exist_ok=True)
    policy = replace(RiskPolicy.from_file(ROOT / 'config/exploration-v2.toml'),
                     time_stop_seconds=1, min_independent_wallets=1)
    marker = folder / 'identity.json'
    identity = json.loads(marker.read_text()) if marker.exists() else None
    ledger = Ledger(folder / 'ledger.sqlite3', policy.starting_cash_usd, require_existing=identity is not None)
    try:
        engine = ExecutionValidationEngine(ledger, policy)
        expected = {'account_id': ledger._get('account_id'), 'token': token, 'pool': pool,
                    'purpose': 'ISOLATED EXECUTION VALIDATION; not week-trial performance'}
        if identity is not None and identity != expected:
            raise ValueError('Acceptance account identity or market changed')
        if identity is None:
            atomic_json(marker, expected)
        result = {'mode': 'EXECUTION_VALIDATION', 'real_money_enabled': False,
                  'strategy_result': False, 'purpose': expected['purpose'], 'errors': []}
        asset = 'eip155:4663/erc20:' + token
        try:
            if not ledger.conn.execute('SELECT 1 FROM fills LIMIT 1').fetchone():
                safety = screen(token, pool)
                q = route.quote(token, pool, max_notional=policy.ticket_usd)
                ledger.record_event('validation-entry-route', 'validation_route', route.last_evidence, int(clock()))
                candidate = Candidate('validation-entry:' + str(q.observed_at), asset, symbol, q.observed_at,
                    [], safety, engine.strategy_name, {'decision_at': q.observed_at,
                    'trigger': 'operator_requested_execution_validation', 'strategy_result': False})
                result['entry'] = engine.consider(candidate, q, int(clock()))
            for position in ledger.snapshot()['positions']:
                q = route.quote(token, pool, max_notional=policy.ticket_usd, sell_quantity=position['qty'])
                ledger.record_event('validation-exit-route:' + str(q.observed_at), 'validation_route', route.last_evidence, int(clock()))
                result['exits'] = engine.watch({asset: q}, int(clock()))
        except Exception as exc:
            result['errors'].append(type(exc).__name__ + ': source unavailable or invalid; retry preserves account')
        result['snapshot'] = engine.report(int(clock()))
        result['fills'] = [dict(r) for r in ledger.conn.execute('SELECT * FROM fills ORDER BY id')]
        try:
            result['telegram'] = notify(ledger, folder, runtime)
        except Exception as exc:
            result['errors'].append('Telegram: ' + type(exc).__name__)
        complete = (len(result['fills']) == 2 and result['snapshot']['closed_positions'] == 1
                    and result['snapshot']['open_positions'] == 0)
        delivered = result.get('telegram', {}).get('outbox', {}).get('sent') == 2
        result['status'] = 'COMPLETE' if complete and delivered else 'AWAITING_ALERT_DELIVERY' if complete else 'PENDING'
        result['updated_at'] = int(clock())
        result = json_safe(result)
        atomic_json(folder / 'status.json', result)
        ledger.export_csv(folder / 'trades.csv')
        write_html(result, folder / 'report.html', data_mode='ISOLATED EXECUTION VALIDATION / LIVE QUOTES / NOT STRATEGY PERFORMANCE')
        return result
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--token', required=True)
    parser.add_argument('--pool', required=True)
    parser.add_argument('--symbol', required=True)
    args = parser.parse_args()
    endpoint_file = args.runtime / 'observation-rpc-url.txt'
    endpoint = endpoint_file.read_text(encoding='utf-8-sig').strip() if endpoint_file.exists() else DEFAULT_RPC
    rpc = RpcClient(endpoint, timeout=8, max_retries=1)
    route = V3RouteClient(rpc, DexScreenerClient(chain_id='robinhood'))
    def screen(token, pool):
        evidence = pool_adjusted_screen(GoPlusClient().screen(token), pool)
        source = args.runtime / 'exploration/ledger.sqlite3'
        if source.exists():
            conn = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                for row in conn.execute("SELECT payload FROM events WHERE kind='exploratory_wallet_buy' ORDER BY observed_at DESC LIMIT 1000"):
                    proof = json.loads(row[0])
                    if proof.get('token_address') == token and proof.get('pool') == pool:
                        ConfirmationBook.confirm([proof], rpc)
                        evidence = observed_pool_screen(evidence, proof)
                        break
            finally:
                conn.close()
        evidence = observe_creator_balance(evidence, rpc)
        return enhance_rug_screen(supplement_from_runtime(evidence, pool, rpc, args.runtime))
    with instance_lock(args.runtime / 'acceptance.lock'):
        result = validate_cycle(args.runtime, args.token, args.pool, args.symbol, route=route, screen=screen)
    print(json.dumps({k: result[k] for k in ('status', 'fills', 'telegram', 'errors') if k in result}, indent=2))
    return 0 if result['status'] == 'COMPLETE' else 2


if __name__ == '__main__':
    raise SystemExit(main())
