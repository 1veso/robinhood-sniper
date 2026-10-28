"""Bounded v0 wire codec for unsigned preparation; never signing authority.

Lookup contents must come from RPC, not a provider's transaction description.
Decoding a message does not validate the semantics of programs invoked by it.
"""
import base64
from dataclasses import dataclass

from sniper.adapters.solana import BASE58, validate_address


def b58decode(value):
    validate_address(value)
    n = 0
    for c in value:
        n = n * 58 + BASE58.index(c)
    return n.to_bytes(32, 'big')


def b58encode(value):
    n, out = int.from_bytes(value, 'big'), ''
    while n:
        n, remainder = divmod(n, 58)
        out = BASE58[remainder] + out
    return '1' * (len(value) - len(value.lstrip(b'\0'))) + out


def compact(value):
    if type(value) is not int or not 0 <= value <= 65535:
        raise ValueError('Invalid compact-u16')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


@dataclass
class Cursor:
    data: bytes
    offset: int = 0

    def take(self, count):
        if count < 0 or self.offset + count > len(self.data):
            raise ValueError('Truncated wire message')
        result = self.data[self.offset:self.offset + count]
        self.offset += count
        return result

    def u8(self):
        return self.take(1)[0]

    def short(self):
        start, value = self.offset, 0
        for shift in (0, 7, 14):
            byte = self.u8()
            value |= (byte & 127) << shift
            if not byte & 128:
                if self.data[start:self.offset] != compact(value):
                    raise ValueError('Noncanonical compact-u16')
                return value
        raise ValueError('Invalid compact-u16 continuation')


def instruction(value):
    if not isinstance(value, dict):
        raise ValueError('Instruction must be an object')
    program = validate_address(value['programId'])
    accounts = value.get('accounts')
    if not isinstance(accounts, list) or len(accounts) > 255:
        raise ValueError('Invalid instruction accounts')
    normalized = []
    for account in accounts:
        if type(account.get('isSigner')) is not bool or type(account.get('isWritable')) is not bool:
            raise ValueError('Account privileges must be booleans')
        normalized.append({'pubkey': validate_address(account['pubkey']),
            'isSigner': account['isSigner'], 'isWritable': account['isWritable']})
    encoded = value['data']
    if not isinstance(encoded, str) or len(encoded) > 1644:
        raise ValueError('Instruction data too large')
    data = base64.b64decode(encoded, validate=True)
    return {'programId': program, 'accounts': normalized, 'data': base64.b64encode(data).decode()}


def table_addresses(account):
    """Decode active Solana ALT state at the returned context slot."""
    value = account.get('value')
    if not isinstance(value, dict) or value.get('owner') != 'AddressLookupTab1e1111111111111111111111111' or value.get('executable') is not False:
        raise ValueError('Lookup table owner/state mismatch')
    encoded = value['data']
    if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != 'base64' or len(encoded[0]) > 11000:
        raise ValueError('Invalid lookup encoding')
    raw = base64.b64decode(encoded[0], validate=True)
    if len(raw) < 56 or (len(raw) - 56) % 32 or len(raw) > 56 + 256 * 32:
        raise ValueError('Invalid lookup size')
    if int.from_bytes(raw[:4], 'little') != 1 or int.from_bytes(raw[4:12], 'little') != 2**64 - 1:
        raise ValueError('Uninitialized or deactivated lookup table')
    slot = account['context']['slot']
    if type(slot) is not int or slot <= int.from_bytes(raw[12:20], 'little'):
        raise ValueError('Lookup table extension is not yet active')
    if raw[21] not in (0, 1):
        raise ValueError('Invalid lookup authority option')
    return [b58encode(raw[i:i + 32]) for i in range(56, len(raw), 32)]


def decode_unsigned(encoded, tables):
    if not isinstance(encoded, str) or len(encoded) > 1644:
        raise ValueError('Wire transaction exceeds packet bound')
    wire = base64.b64decode(encoded, validate=True)
    if not 1 <= len(wire) <= 1232:
        raise ValueError('Wire transaction exceeds packet bound')
    c = Cursor(wire)
    signatures = c.short()
    if signatures != 1 or c.take(64) != bytes(64):
        raise ValueError('Preparation requires exactly one zero signature')
    message_start = c.offset
    if c.u8() != 128:
        raise ValueError('Only v0 messages supported')
    required, readonly_signed, readonly_unsigned = c.u8(), c.u8(), c.u8()
    count = c.short()
    if required != 1 or readonly_signed != 0 or not 1 <= count <= 256 or readonly_unsigned > count - required:
        raise ValueError('Invalid message header')
    static = [b58encode(c.take(32)) for _ in range(count)]
    blockhash = b58encode(c.take(32))
    instructions = []
    instruction_count = c.short()
    if not 1 <= instruction_count <= 32:
        raise ValueError('Invalid instruction count')
    for _ in range(instruction_count):
        program = c.u8()
        indices = list(c.take(c.short()))
        data = c.take(c.short())
        if program >= count:
            raise ValueError('Program must be a static account')
        instructions.append((program, indices, data))
    lookup_count = c.short()
    if lookup_count > 8:
        raise ValueError('Too many lookup tables')
    writable, readonly, used = [], [], set()
    for _ in range(lookup_count):
        address = b58encode(c.take(32))
        if address in used or address not in tables:
            raise ValueError('Missing or duplicate lookup table')
        used.add(address)
        ws, rs = list(c.take(c.short())), list(c.take(c.short()))
        if len(set(ws + rs)) != len(ws + rs) or any(i >= len(tables[address]) for i in ws + rs):
            raise ValueError('Duplicate or out-of-range lookup index')
        writable.extend(tables[address][i] for i in ws)
        readonly.extend(tables[address][i] for i in rs)
    if c.offset != len(wire):
        raise ValueError('Trailing transaction bytes')
    keys = static + writable + readonly
    if len(keys) > 256 or len(keys) != len(set(keys)):
        raise ValueError('Duplicate or excessive resolved keys')
    def account(index):
        if index >= len(keys):
            raise ValueError('Instruction index out of range')
        return {'pubkey': keys[index], 'isSigner': index == 0,
                'isWritable': index < count - readonly_unsigned or count <= index < count + len(writable)}
    result = []
    for program, indices, data in instructions:
        if account(program)['isWritable'] or account(program)['isSigner']:
            raise ValueError('Executable program has unsafe privileges')
        result.append({'programId': keys[program], 'accounts': [account(i) for i in indices],
                       'data': base64.b64encode(data).decode()})
    return {'payer': keys[0], 'blockhash': blockhash, 'instructions': result,
            'message': wire[message_start:], 'keys': keys, 'lookup_tables': sorted(used)}


def compile_unsigned(instructions, payer, blockhash, tables):
    """Compile a deterministic one-signer v0 message, then decode it again."""
    validate_address(payer)
    b58decode(blockhash)
    if not 1 <= len(instructions) <= 32 or len(tables) > 8:
        raise ValueError('Instruction/lookup bound exceeded')
    instructions = [instruction(i) for i in instructions]
    privileges, programs = {payer: (True, True)}, {i['programId'] for i in instructions}
    for item in instructions:
        for a in item['accounts']:
            if a['isSigner'] and a['pubkey'] != payer:
                raise ValueError('Additional signer is not permitted')
            s, w = privileges.get(a['pubkey'], (False, False))
            privileges[a['pubkey']] = s or a['isSigner'], w or a['isWritable']
        privileges.setdefault(item['programId'], (False, False))
    if any(privileges[p] != (False, False) for p in programs):
        raise ValueError('Program cannot be writable or sign')
    loaded, lookups = set(), []
    for table, addresses in tables.items():
        validate_address(table)
        if len(addresses) > 256:
            raise ValueError('Excessive lookup size')
        for value in addresses:
            validate_address(value)
        ws, rs = [], []
        for index, key in enumerate(addresses):
            if key in privileges and key != payer and key not in programs and key not in loaded:
                (ws if privileges[key][1] else rs).append(index)
                loaded.add(key)
        if ws or rs:
            lookups.append((table, ws, rs))
    static = [payer] + [k for k in privileges if k != payer and k not in loaded and privileges[k][1]]
    readonly = [k for k in privileges if k != payer and k not in loaded and not privileges[k][1]]
    static += readonly
    keys = static + [tables[t][i] for t, ws, _ in lookups for i in ws] + [tables[t][i] for t, _, rs in lookups for i in rs]
    if len(keys) > 256 or len(static) > 255 or len(readonly) > 255:
        raise ValueError('Too many transaction accounts')
    indices = {key: i for i, key in enumerate(keys)}
    message = bytes([128, 1, 0, len(readonly)]) + compact(len(static)) + b''.join(b58decode(k) for k in static) + b58decode(blockhash)
    message += compact(len(instructions))
    for item in instructions:
        data = base64.b64decode(item['data'], validate=True)
        message += bytes([indices[item['programId']]]) + compact(len(item['accounts'])) + bytes(indices[a['pubkey']] for a in item['accounts']) + compact(len(data)) + data
    message += compact(len(lookups))
    for table, ws, rs in lookups:
        message += b58decode(table) + compact(len(ws)) + bytes(ws) + compact(len(rs)) + bytes(rs)
    encoded = base64.b64encode(b'\1' + bytes(64) + message).decode()
    decoded = decode_unsigned(encoded, tables)
    for original, actual in zip(instructions, decoded['instructions']):
        expected = {**original, 'accounts': [{**a, 'isSigner': privileges[a['pubkey']][0], 'isWritable': privileges[a['pubkey']][1]} for a in original['accounts']]}
        if actual != expected:
            raise ValueError('Compiled instruction round-trip mismatch')
    return encoded, decoded
