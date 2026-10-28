"""Prepare quotes/simulations and reconcile real receipts. Never signs or broadcasts."""
import argparse
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from rug_desk.book import Book, atomic, encode, process_lock
from rug_desk.market import Market, check_limits
from rug_desk.report import summary
from sniper.adapters.solana import validate_address
from sniper.adapters.solana_execution import SolanaExecutionReader, reconcile_receipt
from sniper.execution_costs import dec


def local_reader(runtime):
    runtime = Path(runtime)
    # Backend credentials take precedence. An invalid server key must never
    # silently fall back to a different key and produce a misleading check.
    key_path = runtime / 'jupiter-server-api-key.txt'
    if not key_path.exists():
        key_path = runtime / 'jupiter-api-key.txt'
    rpc_path = runtime / 'solana-rpc-url.txt'
    key = key_path.read_text(encoding='utf-8-sig').strip() if key_path.exists() else None
    if key_path.exists() and (not key or '\n' in key or '\r' in key):
        raise ValueError('Jupiter credential must contain exactly one nonempty line')
    rpc = rpc_path.read_text(encoding='utf-8-sig').strip() if rpc_path.exists() else 'https://api.mainnet-beta.solana.com'
    return SolanaExecutionReader(api_key=key, rpc_url=rpc)


def verify_preparation_screen(side, safety, pool, quote, now):
    # Safety deterioration must stop new exposure, not prevent an exit quote.
    if side == 'buy':
        if safety.get('failure') or safety.get('pool') != pool:
            raise ValueError('Fresh safety evidence for the exact entry pool is required')
        failure = check_limits(quote, now)
        if failure:
            raise ValueError(failure)
    elif side != 'sell':
        raise ValueError('Invalid preparation side')


def record_reconciled(book, result, intent, valuation):
    """Frozen, time-matched USD conversion, idempotent chain-signature fill ID."""
    if book.identity['mode'] != 'manual':
        raise ValueError('A separate manual/live holdings ledger is required')
    when = valuation.get('observed_at')
    if type(when) is not int or abs(when - result['block_time']) > 120:
        raise ValueError('USD valuation must be contemporaneous with the actual fill; historical receipts need historical price evidence')
    sol, stable = dec(valuation['sol_usd'], positive=True), dec(valuation['usdc_usd'], positive=True)
    if not isinstance(valuation.get('source'), str) or not valuation['source'].strip():
        raise ValueError('Valuation source required')
    fee = D(result['native_expense_lamports']) / 10**9 * sol
    rent = D(max(0, result.get('rent_capital_change_lamports', 0))) / 10**9 * sol
    costs = {'model': 'finalized_solana_receipt_v1', 'signature': result['signature'],
        'native_network_fee_lamports': result['network_fee_lamports'],
        'explicit_fee_usd': str(fee), 'rent_capital_reserved_usd': str(rent),
        'rent_capital_change_lamports': result.get('rent_capital_change_lamports', 0),
        'receipt': result, 'valuation': valuation,
        'fee_note': 'Swap/transfer fees and slippage are already reflected in confirmed token deltas. meta.fee already includes network priority fees.'}
    fill_id = 'solana:' + result['signature']
    if result['status'] == 'FAILED':
        book.confirmed_expense(fill_id, fee, result['block_time'], costs)
        return
    if result.get('limit_violations'):
        raise ValueError('Receipt has execution-limit violations or unclassified outflows; journal retained for manual reconciliation')
    qty = D(result['token_quantity_raw']) / (10 ** result['token_decimals'])
    cash = abs(D(result['usdc_change_raw'])) / 10**6 * stable
    costs['settlement_cash_usd'] = str(cash)
    price = cash / qty
    position_id = None
    if result['side'] == 'sell':
        # Match closed positions too so replay after a full close stays idempotent.
        rows = [p for p in book.positions(all_positions=True) if p['mint'] == result['mint'] and p['pool'] == result['pool']
                and p['opened_at'] <= result['block_time'] and (p.get('closed_at') is None or p['closed_at'] >= result['block_time'])]
        if len(rows) != 1:
            raise ValueError('Confirmed sale must reconcile with exactly one recorded position')
        position_id = rows[0]['id']
    book.confirmed_fill(fill_id, result['side'], now=result['block_time'], quantity=qty, price=price, fee=fee,
        position_id=position_id, quote={'mint': result['mint'], 'pool': result['pool'], 'price': str(price),
        'liquidity': intent['screened_liquidity_usd']}, execution=costs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('doctor')
    q = sub.add_parser('quote')
    q.add_argument('--mint', required=True)
    q.add_argument('--pool', required=True)
    q.add_argument('--side', choices=('buy', 'sell'), required=True)
    q.add_argument('--amount-raw', required=True)
    q.add_argument('--simulate', action='store_true')
    r = sub.add_parser('reconcile')
    r.add_argument('--intent', type=Path, required=True)
    r.add_argument('--signature', required=True)
    r.add_argument('--valuation-json', type=Path, help='Time-matched sol_usd/usdc_usd/observed_at/source; never use today\'s price for an old fill')
    r.add_argument('--record', action='store_true', help='Record an already-finalized actual fill in a separate ledger')
    args = parser.parse_args()
    app = args.app_root.resolve()
    runtime, desk = app / 'runtime', app / 'rug_desk'
    folder = runtime / 'live-preparation'
    if args.command == 'status':
        book = Book(desk)
        try:
            result = summary(book, int(time.time()))['live_readiness']
            result['local_inputs'] = {name: (runtime / name).is_file() for name in ('jupiter-server-api-key.txt', 'jupiter-api-key.txt', 'solana-trading-address.txt', 'solana-rpc-url.txt')}
            result['jupiter_key_source'] = ('jupiter-server-api-key.txt' if result['local_inputs']['jupiter-server-api-key.txt']
                else 'jupiter-api-key.txt' if result['local_inputs']['jupiter-api-key.txt'] else None)
            result['public_rpc_fallback'] = not result['local_inputs']['solana-rpc-url.txt']
            atomic(folder / 'readiness.json', encode(result))
            print(encode(result))
        finally:
            book.close()
        return
    reader = local_reader(runtime)
    if args.command == 'doctor':
        result = {'solana_mainnet_rpc_verified': False, 'jupiter_key_present': bool(reader.api_key),
                  'jupiter_key_source': 'jupiter-server-api-key.txt' if (runtime / 'jupiter-server-api-key.txt').exists() else 'jupiter-api-key.txt' if reader.api_key else None,
                  'observed_at': int(time.time()),
                  'real_money_enabled': False, 'signing_or_broadcast_requested': False}
        try:
            reader.verify_chain()
            result['solana_mainnet_rpc_verified'] = True
        except Exception as exc:
            result['rpc_error_type'] = type(exc).__name__
        try:
            result['jupiter_quote_probe'] = reader.public_quote_probe()
        except Exception as exc:
            result['quote_error_type'] = type(exc).__name__
            result['quote_http_status'] = getattr(exc, 'code', None)
        atomic(folder / 'doctor.json', encode(result))
        print(encode(result))
        return
    wallet = validate_address((runtime / 'solana-trading-address.txt').read_text(encoding='utf-8-sig').strip())
    if args.command == 'quote':
        from rug_desk.rugcheck import Rugcheck
        try:
            safety = Rugcheck().report(args.mint)
        except Exception:
            safety = {'failure': 'Safety provider unavailable', 'pool': None}
        quote = Market(costs=True).quote(args.mint, args.pool)
        verify_preparation_screen(args.side, safety, args.pool, quote, int(time.time()))
        evidence, raw = reader.order(mint=args.mint, pool=args.pool, side=args.side,
            amount_raw=args.amount_raw, wallet=wallet, now=int(time.time()))
        evidence['screened_liquidity_usd'] = quote['liquidity']
        evidence['market_observation'] = quote
        evidence['safety_observation'] = safety
        evidence['safety_revalidation_required'] = True
        if args.simulate:
            evidence['simulation'] = reader.simulate(raw, evidence, int(time.time()))
        intent_id = uuid.uuid4().hex
        path = folder / 'intents' / (intent_id + '.json')
        atomic(path, encode({'intent_id': intent_id, 'evidence': evidence, 'provider_response': raw}))
        print(encode({'intent_file': str(path), 'evidence': evidence}))
        return
    saved = json.loads(args.intent.read_text(encoding='utf-8'))
    intent = saved['evidence']
    if intent['wallet'] != wallet:
        raise ValueError('Intent belongs to another configured wallet')
    receipt = reader.finalized_receipt(args.signature)
    result = reconcile_receipt(receipt, intent, args.signature)
    proof = {'intent_id': saved['intent_id'], 'result': result, 'raw_receipt': receipt, 'rpc_finalized_verified': True}
    target = folder / 'receipts' / (args.signature + '.json')
    with process_lock(folder / 'receipt-import.lock'):
        if target.exists():
            previous = json.loads(target.read_text(encoding='utf-8'))
            if previous['result'] != result or previous['intent_id'] != saved['intent_id']:
                raise ValueError('Signature already reconciled against conflicting evidence')
        atomic(target, encode(proof))
        if args.record:
            if not args.valuation_json:
                raise ValueError('--record requires explicit time-matched USD valuation evidence')
            valuation = json.loads(args.valuation_json.read_text(encoding='utf-8'))
            book = Book(folder / 'confirmed-account', initialize=True, mode='manual')
            try:
                record_reconciled(book, result, intent, valuation)
                book.export()
            finally:
                book.close()
    print(encode({'receipt_file': str(target), 'recorded': args.record, 'result': result}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(encode({'status': 'BLOCKED', 'error_type': type(exc).__name__,
            'reason': 'Preparation/reconciliation incomplete. No signing or broadcasting occurred; check required local inputs and saved evidence.'}))
        raise SystemExit(1) from None
