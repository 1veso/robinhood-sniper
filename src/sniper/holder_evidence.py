"""Finalized, supply-reconciled holder enumeration; never controller evidence."""
from .adapters.solana import validate_address
from .solana_cpmm import TOKEN

TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
MAX_ACCOUNTS = 4096


def parsed(account, program, kind):
    if not isinstance(account, dict) or account.get('owner') != program or account.get('executable') is not False:
        raise ValueError('Token account owner or executable flag differs')
    data = account.get('data', {}).get('parsed', {})
    if data.get('type') != kind or not isinstance(data.get('info'), dict):
        raise ValueError('Complete parsed token account required')
    return data['info']


def raw_integer(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError('Unsigned raw token quantity required')
    number = int(value)
    if str(number) != value or not 0 <= number < 2**64:
        raise ValueError('Raw token quantity outside u64')
    return number


def collect(rpc, mint, clock):
    from .crew_evidence import digest, MAX_AGE
    from .adapters.solana_execution import MAINNET_GENESIS
    validate_address(mint)
    if rpc('getGenesisHash', []) != MAINNET_GENESIS:
        raise ValueError('Mainnet required')
    first = rpc('getAccountInfo', [mint, {'encoding': 'jsonParsed', 'commitment': 'finalized'}])
    account = first['value']; program = account.get('owner') if isinstance(account, dict) else None
    if program not in (TOKEN, TOKEN_2022):
        raise ValueError('Unsupported mint program')
    info = parsed(account, program, 'mint')
    supply = raw_integer(info['supply']); decimals = info['decimals']
    if not supply or type(decimals) is not int or not 0 <= decimals <= 18:
        raise ValueError('Invalid initialized mint supply/precision')
    if info.get('isInitialized') is not True:
        raise ValueError('Uninitialized mint')
    # Token-2022 accounts may have extensions: filtering by 165 bytes would
    # silently omit them. Parsed account type and exact mint are checked below.
    filters = [{'memcmp': {'offset': 0, 'bytes': mint}}]
    if program == TOKEN:
        filters.insert(0, {'dataSize': 165})
    response = rpc('getProgramAccounts', [program, {'encoding': 'jsonParsed',
        'commitment': 'finalized', 'withContext': True,
        'minContextSlot': first['context']['slot'], 'filters': filters}])
    slot, rows = response['context']['slot'], response['value']
    if type(slot) is not int or slot < first['context']['slot'] or not isinstance(rows, list) or not 0 < len(rows) <= MAX_ACCOUNTS:
        raise ValueError('Complete holder enumeration outside supported bound')
    covered, owners, seen, total = [], set(), set(), 0
    for row in rows:
        address = validate_address(row['pubkey'])
        item = parsed(row['account'], program, 'account')
        owner = validate_address(item['owner'])
        amount = raw_integer(item['tokenAmount']['amount'])
        if (address in seen or item['mint'] != mint or item['tokenAmount']['decimals'] != decimals
                or item.get('state') not in ('initialized', 'frozen')):
            raise ValueError('Holder snapshot is duplicate or inconsistent')
        # Confidential balances/withheld transfer fees cannot be treated as
        # spendable visible holdings. Supply reconciliation below fails closed.
        seen.add(address); total += amount
        if amount:
            owners.add(owner)
        covered.append({'address': address, 'owner': owner, 'balance_raw': str(amount)})
    after = rpc('getAccountInfo', [mint, {'encoding': 'jsonParsed', 'commitment': 'finalized', 'minContextSlot': slot}])
    if after['context']['slot'] < slot or parsed(after['value'], program, 'mint') != info:
        raise ValueError('Mint changed during holder capture')
    block = rpc('getBlock', [slot, {'commitment': 'finalized', 'transactionDetails': 'none', 'rewards': False}])
    stamp = block.get('blockTime') if isinstance(block, dict) else None
    if type(stamp) is not int or not 0 <= int(clock())-stamp <= MAX_AGE or not block.get('blockhash'):
        raise ValueError('Holder snapshot has no current canonical timestamp')
    context = {'slot': slot, 'block_hash': block['blockhash'], 'block_time': stamp,
        'commitment': 'finalized', 'accounts_sha256': digest(response),
        'minted_supply_raw': str(supply), 'covered_balance_raw': str(total),
        'accounts_examined': len(rows), 'covered_accounts': sorted(covered, key=lambda x:x['address']),
        'positive_owners': sorted(owners), 'token_program': program,
        'enumeration': 'full_program_accounts_v1', 'max_accounts': MAX_ACCOUNTS,
        'holder_definition': 'distinct owners of positive visible token balances; infrastructure included'}
    if total != supply:
        return {'status': 'UNKNOWN', 'reason': 'enumerated_balances_do_not_reconcile_to_supply', 'context': context}
    return {'status': 'VALID', 'value': len(owners), 'observed_at': stamp,
        'received_at': int(clock()), 'source': 'solana_finalized_supply_reconciled_accounts',
        'source_observation_time_known': True, 'independently_verified': True,
        'context': context, 'controller_independence': 'UNKNOWN'}
