"""Restricted, deterministic ERC20/V3 execution. No authority is enabled by import.

Only exactInputSingle is supported. SwapRouter02 uses exactly one nested swap
inside its deadline multicall; arbitrary multicalls, Permit2, arbitrary calls,
native transfers and wrapping are unsupported. Runtime-code pins are SHA-256 of bytecode, explicitly
not the EVM's Keccak codeHash. Signing uses the optional eth-account package.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from .adapters.evm import (RpcClient, RpcError, CHAIN_ID, FACTORY, WETH, USDG,
                           TRANSFER_TOPIC, SWAP_TOPIC, address, hash32, quantity,
                           _words, _address_word, decode_swap)
from .adapters.v3_route import QUOTER

IMPLEMENTATION_SLOT='0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
BEACON_SLOT='0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50'

class ExecutionRejected(ValueError):
    pass


class SubmissionUnknown(RpcError):
    """Exact signed identity must be reconciled; this is never failure evidence."""


def uint(value, name, maximum=2**256-1, *, zero=False):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        raise ExecutionRejected('Invalid ' + name)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def code_sha256(code):
    if not isinstance(code, str) or not code.startswith('0x') or len(code) % 2:
        raise ExecutionRejected('Malformed runtime bytecode')
    try:
        raw = bytes.fromhex(code[2:])
    except ValueError:
        raise ExecutionRejected('Malformed runtime bytecode') from None
    if not raw:
        raise ExecutionRejected('Missing runtime bytecode')
    return '0x' + hashlib.sha256(raw).hexdigest()


def _word(value):
    return f'{uint(value, "ABI integer", zero=True):064x}'


def _addr(value):
    return address(value)[2:].zfill(64)


def swap_calldata(intent, wallet, router_abi='uniswap_v3_swaprouter01'):
    """Build the complete allowlisted message, never concatenate provider calls."""
    prefix = _addr(intent['token_in'])+_addr(intent['token_out'])+_word(intent['fee'])+_addr(wallet)
    suffix = _word(intent['amount_in_raw'])+_word(intent['min_output_raw'])+_word(0)
    if router_abi=='uniswap_v3_swaprouter01':
        return '0x414bf389'+prefix+_word(intent['deadline'])+suffix
    if router_abi!='uniswap_v3_swaprouter02':
        raise ExecutionRejected('Unsupported router ABI')
    # SwapRouter02 omits the swap deadline. Enforce it on-chain using exactly
    # one call in multicall(uint256,bytes[]). The dynamic array's element offset
    # starts after its length word; its sole value is the 228-byte swap payload.
    inner='04e45aaf'+prefix+suffix
    padded=inner+'0'*((-len(inner))%64)
    return '0x5ae401dc'+_word(intent['deadline'])+_word(64)+_word(1)+_word(32)+_word(len(inner)//2)+padded


def approval_calldata(router, amount):
    return '0x095ea7b3' + _addr(router) + _word(amount)


def simulated_output(data, router_abi):
    """Strictly decode the locally built swap's actual return ABI."""
    if router_abi=='uniswap_v3_swaprouter01':
        return int(_words(data,1)[0],16)
    if router_abi!='uniswap_v3_swaprouter02':
        raise ExecutionRejected('Unsupported router result ABI')
    # multicall returns one bytes[] value; its sole bytes item is the uint256
    # returned by exactInputSingle. Reject padding/extra calls/noncanonical offsets.
    words=[int(w,16) for w in _words(data,5)]
    if words[:4]!=[32,1,32,32]:
        raise ExecutionRejected('Unexpected Router02 multicall result layout')
    return words[4]


class ExecutionRpcClient(RpcClient):
    """A separate RPC capability; the existing observation client stays read-only."""
    READ_METHODS = RpcClient.READ_METHODS | {'eth_getBalance', 'eth_estimateGas', 'eth_getStorageAt'}

    def __init__(self, *args, broadcast_enabled=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.broadcast_enabled = broadcast_enabled is True

    def send_raw(self, raw):
        if not self.broadcast_enabled:
            raise ExecutionRejected('RPC broadcast capability is disabled')
        if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) > 32768:
            raise ExecutionRejected('Invalid signed transaction encoding')
        # No transport retries here. The journal decides whether to rebroadcast
        # precisely these bytes after reconciling their already-known hash.
        self._id += 1
        payload = {'jsonrpc': '2.0', 'id': self._id,
                   'method': 'eth_sendRawTransaction', 'params': [raw]}
        try:
            response = (self.transport or self._http)(payload)
        except Exception:
            raise SubmissionUnknown('Submission response unavailable; reconcile signed transaction identity') from None
        if (not isinstance(response, dict) or response.get('id') != self._id
                or response.get('jsonrpc') != '2.0' or 'error' in response
                or not isinstance(response.get('result'), str)):
            raise SubmissionUnknown('Submission acknowledgement ambiguous; retain signed transaction identity')
        return hash32(response['result'])


def _crypto():
    try:
        from eth_account import Account
        from eth_utils import keccak, to_checksum_address
        import rlp
    except ImportError:
        raise ExecutionRejected('Optional executor dependencies are not installed') from None
    return Account, keccak, to_checksum_address, rlp


def decode_signed(raw):
    """Decode only canonical EIP-155 legacy transactions; recover the signer."""
    Account, keccak, _, rlp = _crypto()
    try:
        blob = bytes.fromhex(raw.removeprefix('0x'))
        values = rlp.decode(blob, strict=True)
        if len(values) != 9 or any(not isinstance(v, bytes) for v in values):
            raise ValueError()
        for index in (0, 1, 2, 4, 6, 7, 8):
            if values[index][:1] == b'\0':
                raise ValueError()
        if len(values[3]) != 20:
            raise ValueError()
        nonce, gas_price, gas, value, v = (int.from_bytes(values[i], 'big') for i in (0,1,2,4,6))
        if v < 35:
            raise ValueError()
        return {'transaction': {'chainId': (v-35)//2, 'nonce': nonce,
                    'gasPrice': gas_price, 'gas': gas, 'to': '0x'+values[3].hex(),
                    'value': value, 'data': '0x'+values[5].hex()},
                'wallet': address(Account.recover_transaction(blob)),
                'tx_hash': '0x'+keccak(blob).hex()}
    except Exception as exc:
        if isinstance(exc, ExecutionRejected):
            raise
        raise ExecutionRejected('Signed transaction is invalid or unsupported') from None


class RestrictedEvmSigner:
    """Policy-bound local signer. A durable reservation callback is mandatory.

    This is an in-process capability, not an OS/HSM security boundary. Only the
    executor process should receive its key_loader; research and model tools
    must never receive it. A separately approved OS identity/secret store is a
    deployment prerequisite. No default key path or environment key is read.
    """
    def __init__(self, adapter, key_loader, authorize):
        if not callable(key_loader) or not callable(authorize):
            raise ExecutionRejected('Signer requires a secret loader and journal authorization')
        self.adapter, self.key_loader, self.authorize = adapter, key_loader, authorize

    @classmethod
    def encrypted_keystore(cls, adapter, path, password_loader, authorize):
        path = Path(path)
        def load():
            Account, _, _, _ = _crypto()
            # The caller supplies a private OS secret-loader; no passphrase is
            # put in arguments, logs, reports, policy or source files here.
            try:
                return Account.decrypt(path.read_text(encoding='utf-8'), password_loader())
            except Exception:
                raise ExecutionRejected('Signing key could not be unlocked') from None
        return cls(adapter, load, authorize)

    def sign(self, prepared):
        self.adapter.validate_prepared(prepared, fresh=True)
        if self.adapter.policy.get('deployment_authorized') is not True:
            raise ExecutionRejected('Initial live deployment has not been authorized')
        if self.authorize(deepcopy(prepared)) is not True:
            raise ExecutionRejected('Durable order reservation denied signing')
        Account, _, checksum, _ = _crypto()
        secret = None
        try:
            secret = self.key_loader()
            account = Account.from_key(secret)
            if address(account.address) != self.adapter.wallet:
                raise ExecutionRejected('Signing identity does not match approved wallet')
            # Unlocking can be slow. A lease, halt or deadline can change while
            # the secret store responds; stale authority must not sign afterward.
            self.adapter.validate_prepared(prepared)
            if self.authorize(deepcopy(prepared)) is not True:
                raise ExecutionRejected('Durable reservation expired while unlocking signer')
            tx = deepcopy(prepared['tx'])
            tx['to'] = checksum(tx['to'])
            signed = account.sign_transaction(tx)
            raw = '0x' + bytes(signed.raw_transaction).hex()
            decoded = decode_signed(raw)
            if decoded['transaction'] != prepared['tx'] or decoded['wallet'] != self.adapter.wallet:
                raise ExecutionRejected('Signer returned a different transaction')
            return {'raw_transaction': raw, 'tx_hash': decoded['tx_hash'],
                    'nonce': prepared['nonce'], 'prepared': deepcopy(prepared)}
        except ExecutionRejected:
            raise
        except Exception:
            raise ExecutionRejected('Signing failed without an authorized transaction') from None
        finally:
            secret = None  # Python cannot promise memory zeroization.


class EvmExecutionAdapter:
    is_test_only = False

    def __init__(self, policy, rpc, signer=None, *, clock=time.time, authorize_broadcast=None):
        self.policy, self.rpc, self.signer, self.clock = deepcopy(policy), rpc, signer, clock
        self.authorize_broadcast = authorize_broadcast
        if type(policy.get('chain_id')) is not int or policy['chain_id'] != CHAIN_ID:
            raise ExecutionRejected('Execution policy must bind Robinhood chain 4663')
        self.wallet = address(policy['wallet'])
        self.router = address(policy['router'])
        self.factory = address(policy.get('factory', FACTORY))
        self.quoter = address(policy.get('quoter', QUOTER))
        self.base = address(policy['base_token'])
        if self.base not in (USDG, WETH) or self.factory != FACTORY:
            raise ExecutionRejected('Unsupported base token or factory deployment')
        if policy.get('router_abi') not in ('uniswap_v3_swaprouter01','uniswap_v3_swaprouter02'):
            raise ExecutionRejected('Only reviewed V3 exact-input router profiles are supported')
        for key in ('account_id','policy_hash'):
            if not isinstance(policy.get(key),str) or not policy[key]:
                raise ExecutionRejected('Explicit account and policy identity required')
        hash32(policy['genesis_hash'])
        for key in ('router_code_sha256','factory_code_sha256','quoter_code_sha256','base_token_code_sha256'):
            hash32(policy[key])
        for key in ('max_order_raw','max_tx_gas_wei','max_gas_limit','max_gas_price_wei'):
            uint(policy[key], key)
        if type(policy.get('max_slippage_bps')) is not int or not 1 <= policy['max_slippage_bps'] <= 1000:
            raise ExecutionRejected('Slippage policy must be explicitly bounded')
        if type(policy.get('max_quote_age_seconds')) is not int or not 1 <= policy['max_quote_age_seconds'] <= 60:
            raise ExecutionRejected('Quote age policy must be explicitly bounded')
        if type(policy.get('confirmations')) is not int or not 1 <= policy['confirmations'] <= 1000:
            raise ExecutionRejected('Confirmation policy must be explicitly bounded')
        if policy.get('require_finalized', True) is not True:
            raise ExecutionRejected('This executor requires finalized receipts')

    def _call(self, to, data, block):
        return self.rpc.call('eth_call', [{'to': address(to), 'data': data}, block])

    def _uint(self, to, data, block):
        return int(_words(self._call(to, data, block), 1)[0], 16)

    def _balance(self, token, block):
        return self._uint(token, '0x70a08231'+_addr(self.wallet), block)

    def _allowance(self, token, block):
        return self._uint(token, '0xdd62ed3e'+_addr(self.wallet)+_addr(self.router), block)

    def _proxy_slots(self, target, tag):
        return tuple(_address_word(_words(self.rpc.call('eth_getStorageAt',[target,slot,tag]),1)[0])
                     for slot in (IMPLEMENTATION_SLOT,BEACON_SLOT))

    def _verify_base_proxy(self, tag):
        implementation,beacon=self._proxy_slots(self.base,tag)
        zero='0x'+'0'*40
        expected=self.policy.get('base_token_proxy')
        if beacon!=zero:
            raise ExecutionRejected('Beacon-based base-token proxies are unsupported')
        if implementation==zero:
            if expected is not None:
                raise ExecutionRejected('Approved base implementation slot changed')
            return
        if (not isinstance(expected,dict) or expected.get('kind')!='eip1967'
                or address(expected['implementation'])!=implementation):
            raise ExecutionRejected('Base token implementation is not independently pinned')
        if code_sha256(self.rpc.call('eth_getCode',[implementation,tag]))!=hash32(expected['implementation_code_sha256']):
            raise ExecutionRejected('Approved base implementation bytecode changed')
        if self._proxy_slots(implementation,tag)!=(zero,zero):
            raise ExecutionRejected('Nested base proxy is unsupported')

    def _anchor(self):
        if quantity(self.rpc.call('eth_chainId')) != CHAIN_ID:
            raise ExecutionRejected('RPC chain identity differs from deployment')
        genesis = self.rpc.call('eth_getBlockByNumber', ['0x0', False])
        if hash32(genesis['hash']) != self.policy['genesis_hash']:
            raise ExecutionRejected('RPC genesis differs from approved deployment')
        header = self.rpc.call('eth_getBlockByNumber', ['latest', False])
        block = quantity(header['number'])
        if not 0 <= int(self.clock()) - quantity(header['timestamp']) <= self.policy['max_quote_age_seconds']:
            raise ExecutionRejected('Canonical execution block is stale')
        block_hash = hash32(header['hash'])
        tag = hex(block)
        if self.rpc.call('eth_getCode', [self.wallet, tag]) != '0x':
            raise ExecutionRejected('Smart/delegated signing accounts are unsupported')
        for target, key in ((self.router,'router_code_sha256'),(self.factory,'factory_code_sha256'),
                            (self.quoter,'quoter_code_sha256'),(self.base,'base_token_code_sha256')):
            if code_sha256(self.rpc.call('eth_getCode', [target, tag])) != self.policy[key]:
                raise ExecutionRejected('Reviewed deployment bytecode changed')
        for target in (self.router, self.quoter):
            if (_address_word(_words(self._call(target,'0xc45a0155',tag),1)[0]) != self.factory
                    or _address_word(_words(self._call(target,'0x4aa4a4fc',tag),1)[0]) != WETH):
                raise ExecutionRejected('Router/quoter factory or wrapped native token mismatch')
        self._verify_base_proxy(tag)
        return {'block_number':block,'block_hash':block_hash,'block_timestamp':quantity(header['timestamp'])}

    def _recheck_anchor(self, anchor):
        header = self.rpc.call('eth_getBlockByNumber', [hex(anchor['block_number']),False])
        if hash32(header['hash']) != anchor['block_hash']:
            raise ExecutionRejected('Execution anchor reorganized')

    def check_finality_latency(self):
        """Entry/deployment gate only. Never disable reconciliation or exits."""
        maximum = self.policy.get('max_finality_lag_seconds')
        if type(maximum) is not int or not 1 <= maximum <= 3600:
            raise ExecutionRejected('Reviewed finality-latency limit is required')
        if quantity(self.rpc.call('eth_chainId')) != CHAIN_ID:
            raise ExecutionRejected('Wrong chain for finality observation')
        if hash32(self.rpc.call('eth_getBlockByNumber', ['0x0', False])['hash']) != self.policy['genesis_hash']:
            raise ExecutionRejected('Wrong genesis for finality observation')
        latest = self.rpc.call('eth_getBlockByNumber', ['latest', False])
        finalized = self.rpc.call('eth_getBlockByNumber', ['finalized', False])
        if not latest or not finalized:
            raise ExecutionRejected('Finality observation unavailable')
        newest, settled = quantity(latest['number']), quantity(finalized['number'])
        latest_time, settled_time = quantity(latest['timestamp']), quantity(finalized['timestamp'])
        if newest < settled or latest_time < settled_time or not 0 <= int(self.clock()) - latest_time <= 30:
            raise ExecutionRejected('Invalid or stale finality snapshot')
        for header in (latest, finalized):
            canonical_header = self.rpc.call('eth_getBlockByNumber', [header['number'], False])
            if not canonical_header or hash32(canonical_header['hash']) != hash32(header['hash']):
                raise ExecutionRejected('Finality snapshot changed')
        lag = latest_time - settled_time
        if lag > maximum:
            raise ExecutionRejected('Finality latency exceeds the reviewed entry limit')
        return {'observed_at': int(self.clock()), 'lag_seconds': lag, 'lag_blocks': newest - settled,
                'maximum_seconds': maximum, 'scope': 'Canonical snapshot, not a guaranteed exit time'}

    def _route(self, token_in, token_out, pool, anchor):
        token_in, token_out, pool = address(token_in), address(token_out), address(pool)
        if token_in == token_out or self.base not in (token_in, token_out):
            raise ExecutionRejected('Only one approved-base/token hop is supported')
        tag = hex(anchor['block_number'])
        token0 = _address_word(_words(self._call(pool,'0x0dfe1681',tag),1)[0])
        token1 = _address_word(_words(self._call(pool,'0xd21220a7',tag),1)[0])
        fee = self._uint(pool,'0xddca3f43',tag)
        if {token0,token1} != {token_in,token_out} or not 0 < fee < 1_000_000:
            raise ExecutionRejected('Pinned pool token pair/fee mismatch')
        lookup = '0x1698ee82'+_addr(token0)+_addr(token1)+_word(fee)
        if _address_word(_words(self._call(self.factory,lookup,tag),1)[0]) != pool:
            raise ExecutionRejected('Pool is not registered in the reviewed factory')
        fingerprints = {t:code_sha256(self.rpc.call('eth_getCode',[t,tag])) for t in (token_in,token_out,pool)}
        other=token_out if token_in==self.base else token_in
        if self._proxy_slots(other,tag)!=('0x'+'0'*40,'0x'+'0'*40):
            raise ExecutionRejected('Unreviewed target-token implementation/beacon proxy')
        return fee, fingerprints

    def quote_swap(self, token_in, token_out, pool, amount_in_raw):
        token_in, token_out, pool = address(token_in), address(token_out), address(pool)
        amount = uint(amount_in_raw,'input amount')
        anchor = self._anchor()
        fee, fingerprints = self._route(token_in,token_out,pool,anchor)
        data = '0xc6a5026a'+_addr(token_in)+_addr(token_out)+_word(amount)+_word(fee)+_word(0)
        out = int(_words(self._call(self.quoter,data,hex(anchor['block_number'])),4)[0],16)
        if out <= 0:
            raise ExecutionRejected('Quote has no executable output')
        minimum = out*(10000-self.policy['max_slippage_bps'])//10000
        if minimum <= 0:
            raise ExecutionRejected('Minimum output rounds to zero')
        self._recheck_anchor(anchor)
        now = int(self.clock())
        return {'token_in':token_in,'token_out':token_out,'pool':pool,'amount_in_raw':str(amount),
                'quoted_output_raw':str(out),'min_output_raw':str(minimum),'quote_at':now,
                'deadline':now+self.policy['max_quote_age_seconds'],'fee':fee,
                'token_code_sha256':fingerprints,**anchor}

    def _intent(self, intent, *, allow_expired=False):
        result = deepcopy(intent)
        if not isinstance(result.get('id'), str) or not result['id']:
            raise ExecutionRejected('Stable order identity is required')
        for key in ('account_id','policy_hash'):
            if result.get(key) != self.policy.get(key):
                raise ExecutionRejected('Order account or policy binding mismatch')
        result['kind'] = result.get('kind','swap')
        if result['kind'] not in ('swap','approval','approval_reset') or result.get('side') not in ('buy','sell'):
            raise ExecutionRejected('Unsupported order kind or side')
        result['token_in'],result['token_out'],result['pool'] = (address(result[k]) for k in ('token_in','token_out','pool'))
        if result['side']=='buy' and result['token_in'] != self.base:
            raise ExecutionRejected('Entry input must be approved base asset')
        if result['side']=='sell' and result['token_out'] != self.base:
            raise ExecutionRejected('Exit output must be approved base asset')
        amount = uint(result['amount_in_raw'],'input amount')
        if result['side']=='buy' and amount > int(self.policy['max_order_raw']):
            raise ExecutionRejected('Entry exceeds approved per-order amount')
        result['amount_in_raw'] = str(amount)
        minimum = uint(result['min_output_raw'],'minimum output')
        quoted = uint(result['quoted_output_raw'],'quoted output')
        if minimum > quoted or minimum*10000 < quoted*(10000-self.policy['max_slippage_bps']):
            raise ExecutionRejected('Minimum output breaches slippage policy')
        for key in ('quote_at','deadline','fee'):
            result[key] = uint(result[key],key)
        now = int(self.clock())
        if not allow_expired and not 0 <= now-result['quote_at'] <= self.policy['max_quote_age_seconds']:
            raise ExecutionRejected('Order quote has expired')
        if (not result['quote_at'] < result['deadline'] <= result['quote_at']+self.policy['max_quote_age_seconds']
                or (not allow_expired and result['deadline'] <= now)):
            raise ExecutionRejected('Order deadline is expired or outside policy')
        return result

    def prepare(self, intent):
        intent = self._intent(intent)
        anchor = self._anchor()
        tag = hex(anchor['block_number'])
        fee, fingerprints = self._route(intent['token_in'],intent['token_out'],intent['pool'],anchor)
        if fee != intent['fee'] or fingerprints != intent['token_code_sha256']:
            raise ExecutionRejected('Quoted pool fee or token bytecode changed')
        latest = quantity(self.rpc.call('eth_getTransactionCount',[self.wallet,'latest']))
        pending = quantity(self.rpc.call('eth_getTransactionCount',[self.wallet,'pending']))
        if latest != pending or ('expected_nonce' in intent and uint(intent['expected_nonce'],'nonce',zero=True)!=pending):
            raise ExecutionRejected('Wallet nonce is busy or changed; reconcile existing orders')
        if self._balance(intent['token_in'],tag) < int(intent['amount_in_raw']):
            raise ExecutionRejected('Actual wallet token balance is insufficient')
        allowance = self._allowance(intent['token_in'],tag)
        kind = intent['kind']
        if kind=='swap' and allowance != int(intent['amount_in_raw']):
            approval = deepcopy(intent)
            reset = allowance != 0
            approval.update(id=intent['id']+(':reset' if reset else ':approval'),
                kind='approval_reset' if reset else 'approval', parent_intent_id=intent['id'],
                approval_amount_raw='0' if reset else intent['amount_in_raw'])
            return {'requires_approval':True,'approval_intent':approval,'observed_allowance_raw':str(allowance)}
        if kind=='approval_reset':
            if allowance == 0 or str(intent.get('approval_amount_raw')) != '0':
                raise ExecutionRejected('Approval reset does not match current allowance')
            to,data = intent['token_in'],approval_calldata(self.router,0)
        elif kind=='approval':
            if allowance != 0 or intent.get('approval_amount_raw') != intent['amount_in_raw']:
                raise ExecutionRejected('Exact approval requires zero prior allowance')
            to,data = intent['token_in'],approval_calldata(self.router,int(intent['amount_in_raw']))
        else:
            current = self.quote_swap(intent['token_in'],intent['token_out'],intent['pool'],intent['amount_in_raw'])
            if (int(current['quoted_output_raw']) < int(intent['min_output_raw'])
                    or int(intent['min_output_raw']) < int(current['min_output_raw'])):
                raise ExecutionRejected('Signed output limit differs from fresh route slippage bounds')
            to,data = self.router,swap_calldata(intent,self.wallet,self.policy['router_abi'])
        gas_price = quantity(self.rpc.call('eth_gasPrice'))
        if not 0 < gas_price <= int(self.policy['max_gas_price_wei']):
            raise ExecutionRejected('Gas price exceeds execution policy')
        simulation = {'from':self.wallet,'to':to,'data':data,'value':'0x0','gasPrice':hex(gas_price)}
        outcome = self.rpc.call('eth_call',[simulation,tag])
        if kind=='swap' and simulated_output(outcome,self.policy['router_abi']) < int(intent['min_output_raw']):
            raise ExecutionRejected('Wallet-specific simulation does not meet output floor')
        if kind!='swap' and outcome not in ('0x','0x'+'0'*63+'1'):
            raise ExecutionRejected('Approval simulation returned false or unsupported data')
        estimate = quantity(self.rpc.call('eth_estimateGas',[simulation,tag]))
        gas = (estimate*125+99)//100
        maximum_fee = gas*gas_price
        if not 21000 <= gas <= int(self.policy['max_gas_limit']) or maximum_fee > int(self.policy['max_tx_gas_wei']):
            raise ExecutionRejected('Transaction gas exceeds approved bound')
        if quantity(self.rpc.call('eth_getBalance',[self.wallet,tag])) < maximum_fee:
            raise ExecutionRejected('Actual wallet cannot pay bounded network expense')
        tx = {'chainId':CHAIN_ID,'nonce':pending,'gasPrice':gas_price,'gas':gas,'to':to,'value':0,'data':data}
        prepared = {'order_id':intent['id'],'policy_hash':intent['policy_hash'],'intent':intent,
                    'nonce':pending,'expires_at':intent['deadline'],'max_fee_wei':str(maximum_fee),
                    'tx':tx,'anchor':anchor,'simulated_at':int(self.clock()),
                    'simulation_result':outcome,'token_code_sha256':fingerprints}
        self._recheck_anchor(anchor)
        self.validate_prepared(prepared)
        return prepared

    def validate_prepared(self, prepared, *, fresh=False, allow_expired=False):
        intent = self._intent(prepared['intent'], allow_expired=allow_expired)
        if prepared.get('order_id') != intent['id'] or prepared.get('policy_hash') != self.policy.get('policy_hash'):
            raise ExecutionRejected('Prepared order identity mismatch')
        tx = prepared['tx']
        if set(tx) != {'chainId','nonce','gasPrice','gas','to','value','data'}:
            raise ExecutionRejected('Extra or missing transaction fields are forbidden')
        if tx['chainId'] != CHAIN_ID or tx['value'] != 0 or type(tx['value']) is not int:
            raise ExecutionRejected('Wrong chain or native-value transfer')
        if uint(tx['nonce'],'nonce',zero=True) != prepared['nonce']:
            raise ExecutionRejected('Prepared nonce mismatch')
        gas,price = uint(tx['gas'],'gas'),uint(tx['gasPrice'],'gas price')
        if (gas > int(self.policy['max_gas_limit']) or price > int(self.policy['max_gas_price_wei'])
                or gas*price > int(self.policy['max_tx_gas_wei']) or str(gas*price) != prepared['max_fee_wei']):
            raise ExecutionRejected('Prepared fee bounds changed')
        if prepared.get('expires_at') != intent['deadline']:
            raise ExecutionRejected('Prepared expiry changed')
        kind = intent['kind']
        if kind=='swap':
            expected_to,expected_data = self.router,swap_calldata(intent,self.wallet,self.policy['router_abi'])
        else:
            exact = 0 if kind=='approval_reset' else int(intent['amount_in_raw'])
            if str(intent.get('approval_amount_raw')) != str(exact):
                raise ExecutionRejected('Approval amount is not exact')
            expected_to,expected_data = intent['token_in'],approval_calldata(self.router,exact)
        if tx['to'] != expected_to or tx['data'] != expected_data:
            raise ExecutionRejected('Unapproved contract, recipient, amount or calldata')
        if fresh:
            check = self.prepare(intent)
            if check.get('requires_approval'):
                raise ExecutionRejected('Approval state changed before signing')
            # A lower gas price/new block may be valid; a higher required fee or
            # nonce shift needs a newly journaled prepared transaction instead.
            if (check['nonce'] != prepared['nonce'] or check['tx']['gas'] > gas
                    or check['tx']['gasPrice'] > price or check['token_code_sha256'] != prepared['token_code_sha256']):
                raise ExecutionRejected('Execution state changed; reprepare before signing')
        return True

    def sign(self, prepared):
        if self.signer is None:
            raise ExecutionRejected('No restricted signer has been configured')
        return self.signer.sign(prepared)

    def _validate_signed(self, signed, *, allow_expired=False):
        prepared = signed['prepared']
        self.validate_prepared(prepared, allow_expired=allow_expired)
        decoded = decode_signed(signed['raw_transaction'])
        if (decoded['transaction'] != prepared['tx'] or decoded['wallet'] != self.wallet
                or decoded['tx_hash'] != hash32(signed['tx_hash']) or signed['nonce'] != prepared['nonce']):
            raise ExecutionRejected('Persisted signed transaction identity mismatch')
        return decoded

    def broadcast(self, signed):
        if self.policy.get('deployment_authorized') is not True:
            raise ExecutionRejected('Initial live deployment has not been authorized')
        self._validate_signed(signed)
        anchor=self._anchor()
        intent=signed['prepared']['intent']
        fee,fingerprints=self._route(intent['token_in'],intent['token_out'],intent['pool'],anchor)
        if fee!=intent['fee'] or fingerprints!=signed['prepared']['token_code_sha256']:
            raise ExecutionRejected('Deployment or token identity changed before broadcast')
        latest=quantity(self.rpc.call('eth_getTransactionCount',[self.wallet,'latest']))
        pending=quantity(self.rpc.call('eth_getTransactionCount',[self.wallet,'pending']))
        if latest!=signed['nonce'] or pending not in (latest,latest+1):
            raise ExecutionRejected('Wallet nonce changed; reconcile without replacement')
        if pending==latest+1 and self.rpc.call('eth_getTransactionByHash',[signed['tx_hash']]) is None:
            raise ExecutionRejected('Unidentified pending nonce; do not replace another transaction')
        self._recheck_anchor(anchor)
        # Network checks may outlive the lease or a user halt. Recheck the
        # deadline and the durable exact-byte reservation at the final boundary;
        # the callback must not retain a database lock across this network write.
        self.validate_prepared(signed['prepared'])
        if (not callable(self.authorize_broadcast)
                or self.authorize_broadcast(deepcopy(signed)) is not True):
            raise ExecutionRejected('Current durable reservation denies broadcast')
        acknowledged = self.rpc.send_raw(signed['raw_transaction'])
        if acknowledged != signed['tx_hash']:
            raise SubmissionUnknown('RPC acknowledged a different hash; original transaction remains unresolved')
        return acknowledged

    def reconcile(self, order):
        """A missing receipt is pending forever until positively resolved.

        Exact wallet state deltas are checked against this receipt's transfers.
        Concurrent external wallet activity, fee-on-transfer/rebasing assets or
        unclassified native fees fail closed as unknown, never fictional fills.
        """
        signed = order['signed']
        self._validate_signed(signed, allow_expired=True)
        prepared,intent = signed['prepared'],signed['prepared']['intent']
        tx_hash = hash32(signed['tx_hash'])
        if quantity(self.rpc.call('eth_chainId')) != CHAIN_ID:
            return {'status':'unknown','reason':'chain_identity_changed'}
        genesis = self.rpc.call('eth_getBlockByNumber',['0x0',False])
        if hash32(genesis['hash']) != self.policy['genesis_hash']:
            return {'status':'unknown','reason':'genesis_identity_changed'}
        receipt = self.rpc.call('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:
            return {'status':'pending','reason':'receipt_unavailable_preserve_signed_identity'}
        block = quantity(receipt['blockNumber'])
        block_hash = hash32(receipt['blockHash'])
        if hash32(receipt['transactionHash']) != tx_hash:
            return {'status':'unknown','reason':'receipt_identity_mismatch'}
        header = self.rpc.call('eth_getBlockByNumber',[hex(block),False])
        if hash32(header['hash']) != block_hash:
            return {'status':'reorged','reason':'receipt_block_not_canonical'}
        latest = quantity(self.rpc.call('eth_blockNumber'))
        if latest-block+1 < self.policy['confirmations']:
            return {'status':'pending','reason':'confirmation_depth','block_number':block,'block_hash':block_hash}
        if self.policy.get('require_finalized', True):
            finalized = self.rpc.call('eth_getBlockByNumber',['finalized',False])
            if not finalized or quantity(finalized['number']) < block:
                return {'status':'pending','reason':'finality_not_reached'}
        tx = self.rpc.call('eth_getTransactionByHash',[tx_hash])
        expected = prepared['tx']
        if (not tx or address(tx['from']) != self.wallet or address(tx['to']) != expected['to']
                or tx['input'].lower() != expected['data'] or quantity(tx['value']) != 0
                or quantity(tx['nonce']) != expected['nonce'] or hash32(tx['hash']) != tx_hash
                or hash32(tx['blockHash']) != block_hash or quantity(tx['blockNumber']) != block
                or quantity(tx.get('chainId',hex(CHAIN_ID))) != CHAIN_ID):
            return {'status':'unknown','reason':'mined_transaction_differs_from_signed_message'}
        status = quantity(receipt['status'])
        gas = quantity(receipt['gasUsed'])
        price = quantity(receipt['effectiveGasPrice'])
        fee = gas*price
        summary = {'tx_hash':tx_hash,'block_hash':block_hash,'block_number':block,
                   'observed_at':quantity(header['timestamp']),'status':status,
                   'gas_used':gas,'effective_gas_price_wei':str(price),'fee_wei':str(fee),
                   'canonical':True,'finalized':self.policy.get('require_finalized',True) is True,
                   'finality':'finalized' if self.policy.get('require_finalized',True) else 'confirmed'}
        if status not in (0,1) or gas > expected['gas'] or price > expected['gasPrice'] or fee > int(prepared['max_fee_wei']):
            return {'status':'unknown','reason':'receipt_fee_or_status_outside_policy','receipt':summary}
        before,after = hex(block-1),hex(block)
        balances = {t:(self._balance(t,before),self._balance(t,after)) for t in (intent['token_in'],intent['token_out'])}
        native_before = quantity(self.rpc.call('eth_getBalance',[self.wallet,before]))
        native_after = quantity(self.rpc.call('eth_getBalance',[self.wallet,after]))
        if native_before-native_after != fee:
            return {'status':'unknown','reason':'unclassified_native_movements_or_fee_model','receipt':summary}
        delta_in = balances[intent['token_in']][0]-balances[intent['token_in']][1]
        delta_out = balances[intent['token_out']][1]-balances[intent['token_out']][0]
        if status == 0:
            if delta_in or delta_out or receipt.get('logs'):
                return {'status':'unknown','reason':'reverted_receipt_has_unexplained_movements','receipt':summary}
            self._recheck_anchor({'block_number':block,'block_hash':block_hash})
            return {'status':'reverted','receipt':summary,'fill':{'input_raw':'0','output_raw':'0','fee_wei':str(fee),
                    'token_in':intent['token_in'],'token_out':intent['token_out'],'wallet':self.wallet,'pnl_complete':False}}
        if intent['kind'] != 'swap':
            exact = 0 if intent['kind']=='approval_reset' else int(intent['amount_in_raw'])
            if delta_in or delta_out or self._allowance(intent['token_in'],after) != exact:
                return {'status':'unknown','reason':'approval_state_or_balance_mismatch','receipt':summary}
            self._recheck_anchor({'block_number':block,'block_hash':block_hash})
            return {'status':'confirmed','receipt':summary,'fill':{'input_raw':'0','output_raw':'0','fee_wei':str(fee),
                    'token_in':intent['token_in'],'token_out':intent['token_out'],'wallet':self.wallet,'kind':intent['kind'],'pnl_complete':False}}
        transfers = {intent['token_in']:0,intent['token_out']:0}
        swaps=[]
        direct_input=direct_output=0
        for log in receipt.get('logs',[]):
            if (hash32(log['transactionHash']) != tx_hash or hash32(log['blockHash']) != block_hash
                    or quantity(log['blockNumber']) != block or log.get('removed') is True):
                return {'status':'unknown','reason':'receipt_log_identity_mismatch','receipt':summary}
            contract = address(log['address'])
            topics = log.get('topics',[])
            if contract==intent['pool'] and topics and topics[0]==SWAP_TOPIC:
                try:
                    swaps.append(decode_swap(log))
                except (KeyError,TypeError,ValueError):
                    return {'status':'unknown','reason':'malformed_pinned_pool_swap','receipt':summary}
            if contract not in transfers or len(topics)!=3 or topics[0]!=TRANSFER_TOPIC:
                continue
            source,destination = _address_word(topics[1][2:]),_address_word(topics[2][2:])
            amount = int(_words(log['data'],1)[0],16)
            if source==self.wallet:transfers[contract]-=amount
            if destination==self.wallet:transfers[contract]+=amount
            if contract==intent['token_in'] and source==self.wallet and destination==intent['pool']:
                direct_input+=amount
            if contract==intent['token_out'] and source==intent['pool'] and destination==self.wallet:
                direct_output+=amount
        if (len(swaps)!=1 or delta_in != int(intent['amount_in_raw']) or delta_out < int(intent['min_output_raw'])
                or direct_input!=delta_in or direct_output!=delta_out
                or transfers[intent['token_in']] != -delta_in or transfers[intent['token_out']] != delta_out):
            return {'status':'unknown','reason':'unsupported_or_unexplained_token_movements','receipt':summary}
        swap=swaps[0]
        token0=_address_word(_words(self._call(intent['pool'],'0x0dfe1681',after),1)[0])
        input_key,output_key=('amount0_raw','amount1_raw') if token0==intent['token_in'] else ('amount1_raw','amount0_raw')
        if (swap['sender']!=self.router or swap['recipient']!=self.wallet
                or int(swap[input_key])!=delta_in or int(swap[output_key])!=-delta_out):
            return {'status':'unknown','reason':'pinned_swap_does_not_match_wallet_flows','receipt':summary}
        self._recheck_anchor({'block_number':block,'block_hash':block_hash})
        return {'status':'confirmed','receipt':summary,'fill':{'input_raw':str(delta_in),'output_raw':str(delta_out),
                'fee_wei':str(fee),'token_in':intent['token_in'],'token_out':intent['token_out'],
                'wallet':self.wallet,'pnl_complete':False,'accounting_scope':'exact raw token/native deltas; no invented USD conversion'}}
