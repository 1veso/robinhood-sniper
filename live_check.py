"""Repeatable free live-preparation checks. Never activates trading or reads a signer."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from rug_desk.book import atomic, encode
from prepare_live import local_reader
from sniper.adapters.solana import validate_address
from sniper.adapters.solana_execution import USDC, WSOL
from sniper.solana_preflight import prepare


def public_wallet(app, chain):
    filename = 'solana-trading-address.txt' if chain == 'solana' else 'evm-trading-address.txt'
    value = (app / 'runtime' / filename).read_text(encoding='utf-8-sig').strip()
    if '\n' in value or '\r' in value:
        raise ValueError('Public address must contain one line')
    value = validate_address(value, 'solana' if chain == 'solana' else 'evm')
    if chain == 'evm' and int(value, 16) == 0:
        raise ValueError('Zero wallet is not usable')
    return value


def failure(exc):
    return {'status': 'BLOCKED', 'error_type': type(exc).__name__, 'http_status': getattr(exc, 'code', None)}


def solana_check(app, destination):
    wallet, reader = public_wallet(app, 'solana'), local_reader(app / 'runtime')
    result = {'observed_at': int(time.time()), 'wallet_sha256': hashlib.sha256(wallet.encode()).hexdigest(),
              'mode': 'UNSIGNED_SETUP', 'live_execution_ready': False, 'probes': [],
              'jupiter_key_source': 'jupiter-server-api-key.txt' if (app/'runtime/jupiter-server-api-key.txt').exists() else 'jupiter-api-key.txt'}
    amount = 25000000  # Setup liquidity pair only; never a strategy order or account top-up.
    for label, source, target in [('usdc_to_wsol', USDC, WSOL), ('wsol_to_usdc', WSOL, USDC)]:
        try:
            # A label observed in the public setup route; not an approved strategy
            # pool or a change to the strategy's allowed exchanges.
            evidence, artifact = prepare(reader, wallet, source, target, amount, dexes='TesseraV')
            atomic(destination / (label + '-unsigned.json'), encode(artifact))
            result['probes'].append({'probe': label, **evidence})
            amount = int(evidence['quote']['quoted_output_raw'])
        except Exception as exc:
            result['probes'].append({'probe': label, **failure(exc)})
            if label == 'usdc_to_wsol':
                break  # Do not invent reverse input from a failed buy quote.
        time.sleep(1.1)
    return result


def evm_check(app):
    """Read current chain/deployment/wallet/finality. No execution account created."""
    from sniper.adapters.evm import RpcClient, address, quantity, hash32, USDG, WETH
    from sniper.execution_evm import code_sha256, IMPLEMENTATION_SLOT, BEACON_SLOT
    wallet = public_wallet(app, 'evm')
    policy = json.loads((app / 'config/executor-policy.example.json').read_text())
    class Reader(RpcClient):
        READ_METHODS = RpcClient.READ_METHODS | {'eth_getStorageAt', 'eth_getBalance'}
    rpc = Reader((app / 'runtime/quote-rpc-url.txt').read_text(encoding='utf-8-sig').strip(), timeout=12, max_retries=1)
    rpc.verify_chain(4663)
    if hash32(rpc.call('eth_getBlockByNumber', ['0x0', False])['hash']) != policy['genesis_hash']:
        raise ValueError('Wrong genesis')
    started = int(time.time())
    header = rpc.call('eth_getBlockByNumber', ['latest', False])
    tag, anchor = header['number'], hash32(header['hash'])
    contracts = {}
    for label in ('router', 'factory', 'quoter', 'base_token'):
        actual = code_sha256(rpc.call('eth_getCode', [policy[label], tag]))
        contracts[label] = {'code_sha256': actual, 'matches_manifest': actual == policy[label + '_code_sha256']}
    proxies = {}
    for name, target in [('USDG', USDG), ('WETH', WETH)]:
        impl = address('0x' + rpc.call('eth_getStorageAt', [target, IMPLEMENTATION_SLOT, tag])[-40:])
        beacon = quantity(rpc.call('eth_getStorageAt', [target, BEACON_SLOT, tag]))
        code = code_sha256(rpc.call('eth_getCode', [impl, tag]))
        expected = policy.get('base_token_proxy') if target == policy['base_token'] else None
        proxies[name] = {'implementation': impl, 'implementation_code_sha256': code, 'beacon_zero': beacon == 0,
            'matches_selected_manifest': expected is not None and expected['implementation'] == impl and expected['implementation_code_sha256'] == code}
    finalized = rpc.call('eth_getBlockByNumber', ['finalized', False])
    if not finalized:
        raise ValueError('Finalized block unavailable')
    latest_number, final_number = quantity(tag), quantity(finalized['number'])
    latest_time, final_time = quantity(header['timestamp']), quantity(finalized['timestamp'])
    final_canonical = rpc.call('eth_getBlockByNumber', [finalized['number'], False])
    canonical = hash32(final_canonical['hash']) == hash32(finalized['hash']) and hash32(rpc.call('eth_getBlockByNumber', [tag, False])['hash']) == anchor
    if not canonical or final_number > latest_number or final_time > latest_time or not 0 <= started - latest_time <= 60:
        raise ValueError('Noncanonical or stale finality snapshot')
    native = quantity(rpc.call('eth_getBalance', [wallet, tag]))
    balances = {}
    for name, token in [('USDG', USDG), ('WETH', WETH)]:
        balances[name] = str(quantity(rpc.call('eth_call', [{'to': token, 'data': '0x70a08231' + wallet[2:].zfill(64)}, tag])))
    nonce, pending = [quantity(rpc.call('eth_getTransactionCount', [wallet, x])) for x in ('latest', 'pending')]
    empty = rpc.call('eth_getCode', [wallet, tag]) == '0x'
    if hash32(rpc.call('eth_getBlockByNumber', [tag, False])['hash']) != anchor:
        raise ValueError('Wallet observation anchor changed')
    return {'observed_at': started, 'finished_at': int(time.time()), 'wallet_sha256': hashlib.sha256(wallet.encode()).hexdigest(),
        'manifest_sha256': hashlib.sha256((app / 'config/executor-policy.example.json').read_bytes()).hexdigest(),
        'chain_id': 4663, 'genesis_verified': True, 'mode': 'READ_ONLY_SETUP', 'contracts': contracts, 'proxies': proxies,
        'latest_block': latest_number, 'finalized_block': final_number, 'canonical_verified': True,
        'finality_lag_blocks': latest_number-final_number, 'finality_lag_seconds': latest_time-final_time,
        'finality_scope': 'Single canonical snapshot; lag is not transaction inclusion or guaranteed exit latency',
        'wallet_native_wei': str(native), 'wallet_settlement_balances_raw': balances,
        'empty_wallet_code': empty, 'nonce': nonce, 'pending_nonce': pending, 'pending_detected': pending != nonce,
        'wallet_specific_swap_simulation_validated': False, 'signer_loaded': False,
        'live_execution_ready': False, 'deployment_authorized': False,
        'signing_calls': 0, 'broadcast_calls': 0, 'fills_created': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    parser.add_argument('chain', choices=('solana', 'evm'))
    args = parser.parse_args()
    app = args.app_root.resolve()
    folder = app / 'runtime/live-preparation' / ('free-preflight-' + str(time.time_ns()))
    try:
        result = solana_check(app, folder) if args.chain == 'solana' else evm_check(app)
    except Exception as exc:
        result = {'observed_at': int(time.time()), **failure(exc), 'live_execution_ready': False}
    result.update(chain=args.chain, real_money_enabled=False, paid_ai_calls=0)
    atomic(folder / 'result.json', encode(result))
    atomic(app / 'runtime/live-preparation' / (args.chain + '-preflight.json'), encode({**result, 'evidence_directory': str(folder)}))
    # Artifacts can contain public transaction accounts; console output omits
    # keys, saved public wallet addresses and raw provider errors.
    print(encode({'chain': args.chain, 'evidence_file': str(folder / 'result.json'), 'result': result}))


if __name__ == '__main__': main()
