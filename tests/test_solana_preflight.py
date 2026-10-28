import base64
import copy
import unittest
from unittest.mock import Mock

from sniper.solana_wire import compact, Cursor, b58encode, table_addresses, compile_unsigned, decode_unsigned
from sniper.solana_preflight import prepare, validate_quote, build_instructions, USDC, WSOL, COMPUTE, JUPITER, ATA, SYSTEM, TOKEN


def key(n):
    return b58encode(bytes([n]) * 32)


WALLET, POOL, TABLE, BLOCK = key(1), key(2), key(3), key(4)


def account(k, signer=False, writable=False):
    return {'pubkey': k, 'isSigner': signer, 'isWritable': writable}


def ix(program, accounts, data):
    return {'programId': program, 'accounts': accounts, 'data': base64.b64encode(data).decode()}


def build():
    return {'inputMint': USDC, 'outputMint': WSOL, 'swapMode': 'ExactIn', 'inAmount': '25000000',
        'outAmount': '245000000', 'otherAmountThreshold': '242550000', 'slippageBps': 100,
        'priceImpactPct': '.0001', 'transactionVersion': 0,
        'routePlan': [{'percent': 100, 'bps': 10000, 'swapInfo': {'ammKey': POOL,
            'inputMint': USDC, 'outputMint': WSOL, 'inAmount': '25000000', 'outAmount': '245000000'}}],
        'computeBudgetInstructions': [ix(COMPUTE, [], b'\3' + (100).to_bytes(8, 'little'))],
        'setupInstructions': [ix(ATA, [account(WALLET, True, True), account(key(7), writable=True), account(WALLET),
                                      account(WSOL), account(SYSTEM), account(TOKEN)], b'\1')],
        'swapInstruction': ix(JUPITER, [account(WALLET, True), account(POOL, writable=True), account(key(7), writable=True), account(USDC), account(WSOL)], b'opaque-unreviewed-cpi'),
        'cleanupInstruction': None, 'otherInstructions': [], 'tipInstruction': None,
        'addressesByLookupTableAddress': {TABLE: [POOL, key(7), USDC, WSOL]}}


def alt(addresses):
    from sniper.solana_wire import b58decode
    data = (1).to_bytes(4, 'little') + (2**64-1).to_bytes(8, 'little') + (5).to_bytes(8, 'little') + bytes(36)
    data += b''.join(b58decode(k) for k in addresses)
    return {'context': {'slot': 10}, 'value': {'owner': 'AddressLookupTab1e1111111111111111111111111', 'executable': False,
        'data': [base64.b64encode(data).decode(), 'base64']}}


class WireTests(unittest.TestCase):
    def test_compact_canonical_boundaries_and_overflow(self):
        for n in (0, 127, 128, 16383, 16384, 65535):
            self.assertEqual(Cursor(compact(n)).short(), n)
        for value in (b'\x80\x00', b'\xff\xff\x07', b'\xff\xff\x80', b'\x80'):
            with self.assertRaises(ValueError): Cursor(value).short()

    def test_v0_roundtrip_preserves_accounts_and_global_privileges(self):
        r = build(); ins, _ = build_instructions(r, WALLET, USDC, WSOL)
        encoded, decoded = compile_unsigned(ins, WALLET, BLOCK, r['addressesByLookupTableAddress'])
        self.assertEqual(decoded['payer'], WALLET)
        self.assertEqual(decoded['blockhash'], BLOCK)
        self.assertEqual(decoded['lookup_tables'], [TABLE])
        self.assertEqual(decoded['instructions'][2]['accounts'][2], account(WALLET, True, True))
        self.assertEqual(decode_unsigned(encoded, r['addressesByLookupTableAddress'])['message'], decoded['message'])

    def test_signatures_trailing_data_unknown_version_missing_lookup_rejected(self):
        r = build(); ins, _ = build_instructions(r, WALLET, USDC, WSOL)
        encoded, _ = compile_unsigned(ins, WALLET, BLOCK, r['addressesByLookupTableAddress'])
        original = base64.b64decode(encoded)
        for wire in (original+b'\0', original[:1]+b'\1'+original[2:], original[:65]+b'\x81'+original[66:]):
            with self.assertRaises(ValueError): decode_unsigned(base64.b64encode(wire).decode(), r['addressesByLookupTableAddress'])
        with self.assertRaises(ValueError): decode_unsigned(encoded, {})

    def test_program_privilege_extra_signer_and_oversized_transactions_rejected(self):
        for a in (account(key(8), True), account(JUPITER, writable=True)):
            with self.assertRaises(ValueError): compile_unsigned([ix(JUPITER, [a], b'a')], WALLET, BLOCK, {})
        with self.assertRaises(ValueError):
            compile_unsigned([ix(JUPITER, [account(key(n)) for n in range(10, 60)], b'a')], WALLET, BLOCK, {})

    def test_alt_owner_activation_state_and_size(self):
        sample = alt([POOL]); self.assertEqual(table_addresses(sample), [POOL])
        for mutate in (lambda a:a['value'].update(owner=TOKEN), lambda a:a['context'].update(slot=5),
                       lambda a:a['value'].update(executable=True), lambda a:a['value'].update(data=['AAAA','base64'])):
            a = copy.deepcopy(sample); mutate(a)
            with self.assertRaises(ValueError): table_addresses(a)


class PreparationTests(unittest.TestCase):
    def reader(self, raw=None, success=False):
        r = Mock(api_key='fixture-not-secret'); r.transport.return_value = raw or build()
        responses = {'getAccountInfo': alt([POOL,key(7),USDC,WSOL]),
            'getLatestBlockhash': {'context': {'slot': 20}, 'value': {'blockhash': BLOCK, 'lastValidBlockHeight': 100}},
            'getFeeForMessage': {'value': 5140}, 'getBalance': {'value': 0},
            'simulateTransaction': {'context': {'slot': 20}, 'value': {'err': None if success else 'AccountNotFound', 'unitsConsumed': 0}}}
        r.rpc.side_effect = lambda method, params: copy.deepcopy(responses[method])
        return r

    def test_unsigned_real_account_request_never_confuses_simulation_with_live(self):
        for success in (False, True):
            r = self.reader(success=success)
            result, artifact = prepare(r, WALLET, USDC, WSOL, 25000000, clock=lambda: 100)
            self.assertEqual(result['simulation']['succeeded'], success)
            self.assertFalse(result['signable']); self.assertFalse(result['live_execution_ready'])
            self.assertFalse(result['structural_checks']['instruction_semantics_validated'])
            self.assertEqual(result['fills_created'], 0)
            self.assertFalse(result['native_balance_covers_network_fee'])
            methods = [c.args[0] for c in r.rpc.call_args_list]
            self.assertNotIn('sendTransaction', methods)
            sim = next(c.args[1][1] for c in r.rpc.call_args_list if c.args[0] == 'simulateTransaction')
            self.assertFalse(sim['sigVerify']); self.assertFalse(sim['replaceRecentBlockhash'])
            self.assertNotIn('accountsOverrides', sim)
            self.assertIn('/swap/v2/build?', r.transport.call_args.args[1])
            self.assertIn('wrapAndUnwrapSol=false', r.transport.call_args.args[1])
            self.assertEqual(base64.b64decode(artifact['transaction'])[1:65], bytes(64))

    def test_quote_wrong_pool_amount_minimum_routes_or_nan_rejected(self):
        changes = [{'inAmount':'1'}, {'otherAmountThreshold':'1'}, {'priceImpactPct':'NaN'},
                   {'slippageBps':101}, {'routePlan':[]}, {'outputMint':key(9)}]
        for update in changes:
            r = build(); r.update(update)
            with self.assertRaises(ValueError): validate_quote(r,USDC,WSOL,25000000)
        with self.assertRaises(ValueError): validate_quote(build(),USDC,WSOL,25000000,pool=key(8))
        r=build(); r['routePlan'][0]['bps']=9999
        with self.assertRaises(ValueError): validate_quote(r,USDC,WSOL,25000000)

    def test_unknown_instruction_extra_transfer_and_wrong_ata_owner_rejected(self):
        for field, value in [('tipInstruction',{}),('cleanupInstruction',{}),('otherInstructions',[{}])]:
            r=build(); r[field]=value or {'programId':SYSTEM}
            with self.assertRaises(ValueError): build_instructions(r,WALLET,USDC,WSOL)
        r=build(); r['setupInstructions'][0]['accounts'][2]['pubkey']=key(9)
        with self.assertRaises(ValueError): build_instructions(r,WALLET,USDC,WSOL)
        r=build(); r['computeBudgetInstructions'][0]=ix(COMPUTE,[],b'\3'+(1000000000).to_bytes(8,'little'))
        with self.assertRaises(ValueError): build_instructions(r,WALLET,USDC,WSOL)

    def test_lookup_substitution_and_expiry_fail_before_simulation(self):
        raw=build(); raw['addressesByLookupTableAddress'][TABLE][0]=key(9)
        r=self.reader(raw)
        with self.assertRaises(ValueError): prepare(r,WALLET,USDC,WSOL,25000000,clock=lambda:100)
        self.assertNotIn('simulateTransaction',[c.args[0] for c in r.rpc.call_args_list])
        r=self.reader(); clock=iter([100,131])
        with self.assertRaises(ValueError): prepare(r,WALLET,USDC,WSOL,25000000,clock=lambda:next(clock))
        self.assertNotIn('simulateTransaction',[c.args[0] for c in r.rpc.call_args_list])

    def test_null_fee_and_conflicting_chain_fail_closed(self):
        r=self.reader(); original=r.rpc.side_effect
        r.rpc.side_effect=lambda m,p:{'value':None} if m=='getFeeForMessage' else original(m,p)
        with self.assertRaises(ValueError): prepare(r,WALLET,USDC,WSOL,25000000,clock=lambda:100)
        r=self.reader(); r.verify_chain.side_effect=ValueError('wrong chain')
        with self.assertRaises(ValueError): prepare(r,WALLET,USDC,WSOL,25000000,clock=lambda:100)
        r.transport.assert_not_called()


if __name__ == '__main__': unittest.main()
