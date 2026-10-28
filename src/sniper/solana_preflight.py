"""Unsigned Metis build and real-account simulation. No signer or submit method.

Structural validation is intentionally distinct from semantic route approval.
In particular, this module never promotes opaque Jupiter CPI instructions to a
signable transaction merely because their JSON description or simulation agrees.
"""
import base64
import hashlib
import time
from decimal import Decimal
from urllib.parse import urlencode

from sniper.adapters.solana import validate_address
from sniper.adapters.solana_execution import USDC, WSOL, integer
from sniper.solana_wire import instruction, compile_unsigned, table_addresses, b58encode

COMPUTE = 'ComputeBudget111111111111111111111111111111'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
SYSTEM = '11111111111111111111111111111111'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
JUPITER = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'


def validate_quote(raw, source, destination, amount, *, pool=None):
    for value in (source, destination):
        validate_address(value)
    if source == destination:
        raise ValueError('Distinct assets required')
    if (raw.get('inputMint'), raw.get('outputMint'), raw.get('swapMode')) != (source, destination, 'ExactIn'):
        raise ValueError('Build quote asset/mode mismatch')
    output = integer(raw.get('outAmount'), positive=True)
    minimum = integer(raw.get('otherAmountThreshold'), positive=True)
    slip = integer(raw.get('slippageBps'))
    if integer(raw.get('inAmount'), positive=True) != integer(amount, positive=True) or slip > 100:
        raise ValueError('Build amount/slippage mismatch')
    if not output * (10000 - slip) // 10000 <= minimum <= output:
        raise ValueError('Build minimum output mismatch')
    impact = Decimal(str(raw.get('priceImpactPct')))
    if not impact.is_finite() or abs(impact) > Decimal('.03'):
        raise ValueError('Build impact unavailable or above preparation ceiling')
    routes = raw.get('routePlan')
    if not isinstance(routes, list) or not 1 <= len(routes) <= 2:
        raise ValueError('Preparation supports at most two unsplit legs')
    cursor, previous_amount = source, integer(amount, positive=True)
    pools = []
    for leg in routes:
        info = leg['swapInfo']
        if leg.get('bps') != 10000 or leg.get('percent') != 100:
            raise ValueError('Split routes require separate review')
        if info.get('inputMint') != cursor or integer(info.get('inAmount'), positive=True) != previous_amount:
            raise ValueError('Route continuity mismatch')
        cursor = validate_address(info['outputMint'])
        previous_amount = integer(info['outAmount'], positive=True)
        pools.append(validate_address(info['ammKey']))
        if cursor != destination and cursor not in (USDC, WSOL):
            raise ValueError('Unsupported intermediate asset')
    if cursor != destination or previous_amount != output or len(set(pools)) != len(pools):
        raise ValueError('Incomplete or cyclic route')
    if pool is not None:
        validate_address(pool)
        target = destination if source == USDC else source
        target_legs = [x['swapInfo'] for x in routes if target in (x['swapInfo']['inputMint'], x['swapInfo']['outputMint'])]
        if len(target_legs) != 1 or target_legs[0]['ammKey'] != pool:
            raise ValueError('Route uses another target pool')
    return {'input_mint': source, 'output_mint': destination, 'amount_raw': str(amount),
            'quoted_output_raw': str(output), 'minimum_output_raw': str(minimum),
            'slippage_bps': slip, 'price_impact_ratio': str(impact), 'route_pools': pools,
            'screened_pool_metadata_matches': pool is not None}


def build_instructions(raw, wallet, source, destination):
    validate_address(wallet)
    if type(raw.get('transactionVersion')) is not int or raw['transactionVersion'] != 0 or raw.get('otherInstructions') or raw.get('tipInstruction') or raw.get('cleanupInstruction'):
        raise ValueError('Unsupported version, additional operation, tip or cleanup')
    budgets, setup = raw.get('computeBudgetInstructions'), raw.get('setupInstructions')
    if not isinstance(budgets, list) or len(budgets) != 1 or not isinstance(setup, list) or len(setup) > 3:
        raise ValueError('Missing or excessive instruction groups')
    budget = instruction(budgets[0])
    data = base64.b64decode(budget['data'])
    if budget['programId'] != COMPUTE or budget['accounts'] or len(data) != 9 or data[0] != 3:
        raise ValueError('Expected bounded compute price instruction')
    price = int.from_bytes(data[1:], 'little')
    limit = 1400000
    priority = (price * limit + 999999) // 1000000
    if priority > 100000:
        raise ValueError('Priority fee exceeds preparation ceiling')
    seen = set()
    setups = []
    for item in setup:
        ix = instruction(item)
        accounts = ix['accounts']
        if ix['programId'] != ATA or ix['data'] != 'AQ==' or len(accounts) != 6:
            raise ValueError('Only idempotent SPL ATA setup is supported')
        expected = [(wallet, True, True), (accounts[1]['pubkey'], False, True), (wallet, False, False),
                    (accounts[3]['pubkey'], False, False), (SYSTEM, False, False), (TOKEN, False, False)]
        if [(a['pubkey'], a['isSigner'], a['isWritable']) for a in accounts] != expected:
            raise ValueError('Unexpected ATA owner, payer or privileges')
        if accounts[3]['pubkey'] not in (source, destination, WSOL) or accounts[1]['pubkey'] in seen:
            raise ValueError('Unexpected or repeated ATA setup')
        seen.add(accounts[1]['pubkey'])
        setups.append(ix)
    swap = instruction(raw['swapInstruction'])
    if swap['programId'] != JUPITER:
        raise ValueError('Unexpected top-level swap program')
    if any(a['isSigner'] and a['pubkey'] != wallet for a in swap['accounts']) or not any(a['pubkey'] == wallet and a['isSigner'] for a in swap['accounts']):
        raise ValueError('Unexpected swap signer')
    # This is an inventory, not interpretation of the opaque CPI route bytes.
    cu_limit = {'programId': COMPUTE, 'accounts': [],
        'data': base64.b64encode(b'\x02' + limit.to_bytes(4, 'little')).decode()}
    return [cu_limit, budget, *setups, swap], {'compute_units_ceiling': limit,
        'priority_fee_ceiling_lamports': priority, 'ata_creations_requested': len(setups),
        'swap_discriminator_hex': base64.b64decode(swap['data'])[:8].hex(),
        'instruction_semantics_validated': False,
        'semantic_blockers': ['Jupiter route and each downstream AMM account/instruction require an independently reviewed validator',
                              'Program deployment/upgrade identities and token account semantics are not approved']}


def prepare(reader, wallet, source, destination, amount, *, pool=None, simulate=True, dexes=None, clock=time.time):
    """Read-only RPC + documented GET /build; return evidence, never an order."""
    wallet = validate_address(wallet)
    validate_address(source)
    validate_address(destination)
    amount = integer(amount, positive=True)
    if not reader.api_key or '\n' in reader.api_key or '\r' in reader.api_key:
        raise ValueError('Valid saved Jupiter server credential required')
    reader.verify_chain()
    started = int(clock())
    params = {'inputMint': source, 'outputMint': destination, 'amount': str(amount), 'taker': wallet,
              'slippageBps': 100, 'wrapAndUnwrapSol': 'false', 'transactionVersion': '0', 'maxAccounts': 64}
    if dexes is not None:
        if not isinstance(dexes, str) or not 1 <= len(dexes) <= 100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,-' for c in dexes):
            raise ValueError('Invalid preparation DEX label')
        params['dexes'] = dexes
    raw = reader.transport('GET', 'https://api.jup.ag/swap/v2/build?' + urlencode(params), None, {'x-api-key': reader.api_key}, 20)
    quote = validate_quote(raw, source, destination, amount, pool=pool)
    if dexes is not None and any(leg['swapInfo'].get('label') not in dexes.split(',') for leg in raw['routePlan']):
        raise ValueError('Provider ignored preparation DEX constraint')
    instructions, audit = build_instructions(raw, wallet, source, destination)
    advertised = raw.get('addressesByLookupTableAddress')
    if not isinstance(advertised, dict) or len(advertised) > 8:
        raise ValueError('Invalid lookup descriptions')
    tables, lookup_proof = {}, []
    for address, described in advertised.items():
        validate_address(address)
        if not isinstance(described, list) or len(described) > 256:
            raise ValueError('Invalid provider lookup description')
        observation = reader.rpc('getAccountInfo', [address, {'encoding': 'base64', 'commitment': 'confirmed'}])
        actual = table_addresses(observation)
        if actual[:len(described)] != described:
            raise ValueError('Provider lookup table differs from chain')
        tables[address] = actual
        lookup_proof.append({'table': address, 'slot': observation['context']['slot'],
            'contents_sha256': hashlib.sha256(''.join(actual).encode()).hexdigest(), 'provider_prefix_matches': True})
    latest = reader.rpc('getLatestBlockhash', [{'commitment': 'confirmed'}])
    blockhash = validate_address(latest['value']['blockhash'])
    height = integer(latest['value']['lastValidBlockHeight'], positive=True)
    wire, decoded = compile_unsigned(instructions, wallet, blockhash, tables)
    if int(clock()) - started > 30:
        raise ValueError('Preparation quote expired during validation')
    message64 = base64.b64encode(decoded['message']).decode()
    fee = reader.rpc('getFeeForMessage', [message64, {'commitment': 'confirmed'}])
    actual_fee = integer(fee.get('value'), positive=True)
    if actual_fee > 10000 + audit['priority_fee_ceiling_lamports']:
        raise ValueError('RPC fee exceeds preparation bound')
    balance = reader.rpc('getBalance', [wallet, {'commitment': 'confirmed'}])
    native = integer(balance['value'])
    result = {'schema': 'solana_unsigned_build_v1', 'observed_at': started,
        'wallet_sha256': hashlib.sha256(wallet.encode()).hexdigest(), 'genesis_verified': True,
        'quote': quote, 'structural_checks': audit, 'lookup_evidence': lookup_proof,
        'rpc_fee_lamports': actual_fee, 'wallet_native_balance_lamports': native,
        'native_balance_covers_network_fee': native >= actual_fee,
        'rent_cost_verified': False, 'swap_fee_cost_verified': False,
        'last_valid_block_height': height, 'blockhash_context_slot': latest['context']['slot'],
        'transaction_sha256': hashlib.sha256(base64.b64decode(wire)).hexdigest(),
        'message_sha256': hashlib.sha256(decoded['message']).hexdigest(),
        'compiled_account_count': len(decoded['keys']), 'transaction_bytes': len(base64.b64decode(wire)),
        'signable': False, 'live_execution_ready': False, 'real_money_enabled': False,
        'signing_calls': 0, 'broadcast_calls': 0, 'fills_created': 0,
        'scope': 'Unsigned real-account setup simulation; no state override, no trade, no strategy approval'}
    result['diagnostic_dex_restriction'] = dexes
    result['simulation'] = {'attempted': False}
    if simulate:
        if int(clock()) - started > 30:
            raise ValueError('Preparation quote expired before simulation')
        observed = reader.rpc('simulateTransaction', [wire, {'encoding': 'base64', 'sigVerify': False,
            'replaceRecentBlockhash': False, 'commitment': 'confirmed', 'minContextSlot': latest['context']['slot']}])
        value = observed.get('value')
        if not isinstance(value, dict) or 'err' not in value:
            raise ValueError('Incomplete simulation result')
        result['simulation'] = {'attempted': True, 'succeeded': value['err'] is None,
            'error': value['err'], 'units_consumed': value.get('unitsConsumed'),
            'slot': observed['context']['slot'], 'signatures_verified': False,
            'state_overridden': False, 'instruction_semantics_validated': False}
    result['finished_at'] = int(clock())
    return result, {'transaction': wire, 'provider_build': raw, 'rpc_lookup_tables': tables}
