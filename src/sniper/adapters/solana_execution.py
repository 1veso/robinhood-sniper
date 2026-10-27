"""Read-only Solana execution preparation and finalized-receipt reconciliation.

No signing or broadcasting method exists here. Quotes and simulations are not fills.
"""
from decimal import Decimal as D
import base64
import hashlib
import re
from urllib.parse import urlencode, urlsplit
from sniper.adapters.solana import request_json, validate_address
from sniper.execution_costs import dec

USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
WSOL = 'So11111111111111111111111111111111111111112'
MAINNET_GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'


def transaction_message(encoded):
    raw = base64.b64decode(encoded, validate=True)
    if not 1 <= len(raw) <= 1232:
        raise ValueError('Invalid transaction size')
    count, shift, offset = 0, 0, 0
    while True:
        if offset >= min(len(raw), 3):
            raise ValueError('Invalid signature vector')
        byte = raw[offset]
        count |= (byte & 127) << shift
        offset += 1
        if not byte & 128:
            break
        shift += 7
    if not 1 <= count <= 12 or offset + count * 64 >= len(raw):
        raise ValueError('Incomplete signatures/message')
    return raw[offset:offset + 64], raw[offset + count * 64:]


def signature_bytes(signature):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    value = 0
    for character in signature:
        value = value * 58 + alphabet.index(character)
    raw = b'\0' * (len(signature) - len(signature.lstrip('1'))) + value.to_bytes((value.bit_length() + 7) // 8, 'big')
    if len(raw) != 64:
        raise ValueError('Invalid signature length')
    return raw


def integer(value, *, positive=False):
    if type(value) is int:
        out = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
        out = int(value)
    else:
        raise ValueError('Invalid raw-unit integer')
    if out < int(positive) or out > 2**64 - 1:
        raise ValueError('Raw-unit value out of range')
    return out


def order_evidence(raw, *, mint, pool, side, amount_raw, wallet, now, max_slippage_bps=100):
    for key in (mint, pool, wallet):
        validate_address(key)
    if side not in ('buy', 'sell') or mint in (USDC, WSOL):
        raise ValueError('This handoff supports a token/USDC settlement pair')
    source, destination = (USDC, mint) if side == 'buy' else (mint, USDC)
    if not isinstance(raw, dict) or raw.get('error') or raw.get('errorMessage') or raw.get('errorCode') not in (None, 0):
        raise ValueError('Provider could not prepare the requested order')
    if (raw.get('inputMint'), raw.get('outputMint'), raw.get('swapMode'), raw.get('taker')) != (source, destination, 'ExactIn', wallet):
        raise ValueError('Quote asset, side, exact-input mode or wallet mismatch')
    amount, output, minimum = (integer(raw.get(k), positive=True) for k in ('inAmount', 'outAmount', 'otherAmountThreshold'))
    slip = integer(raw.get('slippageBps'))
    if amount != integer(amount_raw, positive=True) or slip > max_slippage_bps or not 0 < minimum <= output:
        raise ValueError('Quote amount/slippage limits mismatch')
    if minimum < output * (10000 - max_slippage_bps) // 10000:
        raise ValueError('Minimum output is below the requested slippage bound')
    routes = raw.get('routePlan')
    if not isinstance(routes, list) or not routes:
        raise ValueError('Missing executable route evidence')
    target_legs = []
    for leg in routes:
        info = leg['swapInfo']
        a, b = validate_address(info['inputMint']), validate_address(info['outputMint'])
        integer(info['inAmount'], positive=True)
        integer(info['outAmount'], positive=True)
        if mint in (a, b):
            if info.get('ammKey') != pool or (side == 'buy' and b != mint) or (side == 'sell' and a != mint):
                raise ValueError('Target token route differs from the screened pool')
            target_legs.append(info)
        elif {a, b} != {USDC, WSOL}:
            raise ValueError('Unreviewed intermediate asset')
    if not target_legs:
        raise ValueError('Screened pool absent from route')
    target_total = sum(integer(x['outAmount' if side == 'buy' else 'inAmount']) for x in target_legs)
    if target_total < (minimum if side == 'buy' else amount):
        raise ValueError('Incomplete target pool amount coverage')
    fee_keys = ('signatureFeeLamports', 'prioritizationFeeLamports', 'rentFeeLamports')
    fees = {k: integer(raw.get(k)) for k in fee_keys}
    if fees['prioritizationFeeLamports'] > 100000 or fees['signatureFeeLamports'] > 10000 or fees['rentFeeLamports'] > 10000000:
        raise ValueError('Quote fee estimate exceeds preparation limits')
    for amount_key, payer_key in zip(fee_keys, ('signatureFeePayer', 'prioritizationFeePayer', 'rentFeePayer')):
        if fees[amount_key] and raw.get(payer_key) != wallet:
            raise ValueError('Sponsored/gasless fee semantics require separate review')
    if raw.get('gasless') is not False:
        raise ValueError('Gasless fee recoup is not enabled in this handoff')
    fee_bps = integer(raw.get('feeBps'))
    if fee_bps > 10000 or not isinstance(raw.get('platformFee'), dict):
        raise ValueError('Unknown platform fee accounting')
    platform_amount = integer(raw['platformFee'].get('amount'))
    if integer(raw['platformFee'].get('feeBps')) > fee_bps or (platform_amount and raw.get('feeMint') not in (source, destination)):
        raise ValueError('Unresolved platform fee denomination or total')
    impact = D(str(raw.get('priceImpact')))
    if not impact.is_finite() or abs(impact) > 100:
        raise ValueError('Invalid quote impact')
    transaction = raw.get('transaction')
    transaction_hash, message_hash = None, None
    if transaction:
        decoded = base64.b64decode(transaction, validate=True)
        if not 1 <= len(decoded) <= 1232 or raw.get('transactionVersion') != 0:
            raise ValueError('Unsupported assembled transaction')
        transaction_hash = hashlib.sha256(decoded).hexdigest()
        _, message = transaction_message(transaction)
        message_hash = hashlib.sha256(message).hexdigest()
    return {'schema': 'solana_intent_v1', 'mint': mint, 'pool': pool, 'side': side, 'wallet': wallet,
        'input_mint': source, 'output_mint': destination, 'amount_raw': str(amount), 'quoted_output_raw': str(output),
        'minimum_output_raw': str(minimum), 'slippage_bps': slip, 'observed_at': now, 'expires_at': now + 30,
        'fee_estimates_lamports': fees, 'total_swap_fee_bps': fee_bps, 'platform_fee': raw['platformFee'],
        'fees_already_embedded_in_amounts': True, 'price_impact_percentage_points': str(impact),
        'route_plan': routes, 'transaction_sha256': transaction_hash, 'transaction_available': bool(transaction),
        'message_sha256': message_hash,
        'transaction_instruction_reviewed': False, 'request_id': raw.get('requestId'),
        'real_money_enabled': False, 'status': 'PREPARATION_ONLY',
        'limitations': 'Provider quote is not a fill. Independent instruction review, simulation, current balance checks and explicit signing approval remain required.'}


class SolanaExecutionReader:
    ALLOWED_RPC = {'getGenesisHash', 'getBalance', 'getTransaction', 'getSignatureStatuses', 'simulateTransaction', 'getBlockHeight'}

    def __init__(self, *, api_key=None, rpc_url='https://api.mainnet-beta.solana.com', transport=request_json):
        parsed = urlsplit(rpc_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('An HTTPS Solana RPC URL is required')
        self.api_key, self.rpc_url, self.transport = api_key, rpc_url, transport

    def rpc(self, method, params=None):
        if method not in self.ALLOWED_RPC:
            raise ValueError('Signing/broadcasting is not supported by this reader')
        payload = self.transport('POST', self.rpc_url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}, {'Content-Type': 'application/json'}, 20)
        if not isinstance(payload, dict) or payload.get('id') != 1 or payload.get('error') or 'result' not in payload:
            raise ValueError('Solana RPC response unavailable or invalid')
        return payload['result']

    def verify_chain(self):
        if self.rpc('getGenesisHash') != MAINNET_GENESIS:
            raise ValueError('RPC is not Solana mainnet')

    def public_quote_probe(self):
        """Keyless quote capability check only; no wallet or assembled transaction."""
        params = {'inputMint': WSOL, 'outputMint': USDC, 'amount': '1000000000', 'slippageBps': 100}
        headers = {'x-api-key': self.api_key} if self.api_key else {'Accept': 'application/json'}
        raw = self.transport('GET', 'https://api.jup.ag/swap/v2/order?' + urlencode(params), None, headers, 20)
        if not isinstance(raw, dict) or (raw.get('inputMint'), raw.get('outputMint')) != (WSOL, USDC):
            raise ValueError('Public route probe returned invalid assets')
        output = integer(raw.get('outAmount'), positive=True)
        return {'quote_received': True, 'output_usdc_raw': str(output), 'wallet_specific_costs_verified': False,
                'transaction_requested': False, 'real_money_enabled': False}

    def order(self, *, mint, pool, side, amount_raw, wallet, now):
        if not self.api_key or '\n' in self.api_key or '\r' in self.api_key:
            raise ValueError('Jupiter API key required')
        for address in (mint, pool, wallet):
            validate_address(address)
        if side not in ('buy', 'sell'):
            raise ValueError('Invalid side')
        source, destination = (USDC, mint) if side == 'buy' else (mint, USDC)
        params = {'inputMint': source, 'outputMint': destination, 'amount': str(integer(amount_raw, positive=True)),
            'taker': wallet, 'swapMode': 'ExactIn', 'slippageBps': 100,
            'excludeRouters': 'jupiterz,dflow,okx', 'maxSupportedTransactionVersion': '0',
            'priorityFeeLamports': 100000, 'broadcastFeeType': 'maxCap'}
        raw = self.transport('GET', 'https://api.jup.ag/swap/v2/order?' + urlencode(params), None, {'x-api-key': self.api_key}, 20)
        return order_evidence(raw, mint=mint, pool=pool, side=side, amount_raw=amount_raw, wallet=wallet, now=now), raw

    def simulate(self, raw, evidence, now):
        if not evidence['transaction_available'] or now > evidence['expires_at']:
            raise ValueError('Fresh assembled transaction required')
        encoded = raw['transaction']
        if hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest() != evidence['transaction_sha256']:
            raise ValueError('Assembled transaction changed')
        self.verify_chain()
        result = self.rpc('simulateTransaction', [encoded, {'encoding': 'base64', 'sigVerify': False,
            'replaceRecentBlockhash': True, 'commitment': 'confirmed'}])
        value = result.get('value') if isinstance(result, dict) else None
        if not isinstance(value, dict) or 'err' not in value:
            raise ValueError('Simulation result incomplete')
        return {'simulation_succeeded': value['err'] is None, 'error': value['err'],
            'units_consumed': value.get('unitsConsumed'), 'slot': result.get('context', {}).get('slot'),
            'signatures_verified': False, 'broadcast': False, 'instruction_reviewed': False}

    def finalized_receipt(self, signature):
        if not isinstance(signature, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,90}', signature):
            raise ValueError('Invalid transaction signature')
        self.verify_chain()
        status = self.rpc('getSignatureStatuses', [[signature], {'searchTransactionHistory': True}])
        records = status.get('value') if isinstance(status, dict) else None
        if not isinstance(records, list) or len(records) != 1 or not records[0] or records[0].get('confirmationStatus') != 'finalized':
            raise ValueError('Transaction not finalized; no fill may be recorded')
        receipt = self.rpc('getTransaction', [signature, {'encoding': 'jsonParsed', 'commitment': 'finalized', 'maxSupportedTransactionVersion': 0}])
        if not receipt or receipt.get('slot') != records[0].get('slot'):
            raise ValueError('Finalized receipt missing or inconsistent')
        binary = self.rpc('getTransaction', [signature, {'encoding': 'base64', 'commitment': 'finalized', 'maxSupportedTransactionVersion': 0}])
        if (not binary or binary.get('slot') != receipt['slot'] or binary.get('blockTime') != receipt.get('blockTime')
                or binary.get('meta', {}).get('fee') != receipt.get('meta', {}).get('fee')
                or not isinstance(binary.get('transaction'), list) or binary['transaction'][1] != 'base64'):
            raise ValueError('Finalized binary and parsed receipt mismatch')
        receipt['_wire_transaction'] = binary['transaction'][0]
        return receipt


def reconcile_receipt(receipt, intent, signature):
    """Actual raw-unit cash flows. No USD price or quote is substituted for a fill."""
    tx, meta = receipt['transaction'], receipt['meta']
    if not isinstance(meta, dict) or 'err' not in meta or tx.get('signatures', [None])[0] != signature:
        raise ValueError('Receipt signature or execution status missing')
    first_signature, message = transaction_message(receipt['_wire_transaction'])
    if first_signature != signature_bytes(signature) or hashlib.sha256(message).hexdigest() != intent.get('message_sha256'):
        raise ValueError('Finalized transaction differs from the prepared message')
    wallet, mint, pool = intent['wallet'], intent['mint'], intent['pool']
    keys = tx['message']['accountKeys']
    if not isinstance(keys, list) or not keys or not all(isinstance(k, dict) for k in keys):
        raise ValueError('Parsed account keys required')
    if keys[0].get('pubkey') != wallet or keys[0].get('signer') is not True:
        raise ValueError('Unexpected fee payer/signer')
    if not any(k.get('pubkey') == pool for k in keys):
        raise ValueError('Screened pool is absent from confirmed transaction')
    pre, post = meta['preBalances'], meta['postBalances']
    if len(pre) != len(keys) or len(post) != len(keys):
        raise ValueError('Incomplete lamport balance coverage')
    pre, post = [integer(x) for x in pre], [integer(x) for x in post]
    fee = integer(meta['fee'])  # Already includes priority fee; do not add it again.
    native_change = post[0] - pre[0]
    if meta['err'] is not None:
        if native_change != -fee:
            raise ValueError('Failed transaction has unexplained payer balance changes')
        return {'status': 'FAILED', 'signature': signature, 'slot': receipt['slot'], 'block_time': receipt['blockTime'],
                'network_fee_lamports': fee, 'swap_recorded': False, 'native_expense_lamports': fee,
                'token_quantity_raw': '0', 'usdc_change_raw': '0'}
    balances, owned_indices = {}, set()
    for direction, field in ((-1, 'preTokenBalances'), (1, 'postTokenBalances')):
        rows = meta.get(field)
        if not isinstance(rows, list):
            raise ValueError('Token balance evidence missing')
        seen = set()
        for row in rows:
            index = integer(row['accountIndex'])
            if index >= len(keys) or index in seen:
                raise ValueError('Duplicate or invalid token account index')
            seen.add(index)
            if row.get('owner') != wallet:
                continue
            owned_indices.add(index)
            asset = validate_address(row['mint'])
            amount = integer(row['uiTokenAmount']['amount'])
            decimals = integer(row['uiTokenAmount']['decimals'])
            if decimals > 18:
                raise ValueError('Unsupported token precision')
            total, previous_decimals = balances.get(asset, (0, decimals))
            if previous_decimals != decimals:
                raise ValueError('Token decimals changed')
            balances[asset] = (total + direction * amount, decimals)
    if any(delta for asset, (delta, _) in balances.items() if asset not in (mint, USDC)):
        raise ValueError('Unexpected wallet token movement requires manual reconciliation')
    if mint not in balances or USDC not in balances or balances[USDC][1] != 6:
        raise ValueError('Target token and USDC balance changes required')
    quantity, decimals = balances[mint]
    cash, _ = balances[USDC]
    if (intent['side'] == 'buy' and not (quantity > 0 and cash < 0)) or (intent['side'] == 'sell' and not (quantity < 0 and cash > 0)):
        raise ValueError('Confirmed balance changes disagree with intended side')
    rent = sum(post[i] - pre[i] for i in owned_indices)
    residual = -native_change - fee - rent
    if residual < 0:
        raise ValueError('Unexplained native income cannot be recorded as trading profit')
    actual_input, actual_output = (-cash, quantity) if intent['side'] == 'buy' else (-quantity, cash)
    violations = []
    if actual_input != integer(intent['amount_raw']):
        violations.append('input_amount_differs_from_intent')
    if actual_output < integer(intent['minimum_output_raw']):
        violations.append('output_below_approved_minimum')
    if residual:
        violations.append('unclassified_native_outflow_requires_review')
    expected = integer(intent['quoted_output_raw'], positive=True)
    return {'status': 'CONFIRMED_FILL', 'signature': signature, 'slot': receipt['slot'], 'block_time': receipt['blockTime'],
        'side': intent['side'], 'mint': mint, 'pool': pool, 'wallet': wallet,
        'token_quantity_raw': str(abs(quantity)), 'token_decimals': decimals, 'usdc_change_raw': str(cash),
        'network_fee_lamports': fee, 'rent_capital_change_lamports': rent,
        'unclassified_native_outflow_lamports': residual, 'native_expense_lamports': fee + residual,
        'realized_output_shortfall_bps': str((D(expected) - actual_output) / expected * 10000),
        'fees_already_embedded_in_token_cashflows': True, 'limit_violations': violations,
        'quote_is_not_fill': True, 'usd_valuation_verified': False}
