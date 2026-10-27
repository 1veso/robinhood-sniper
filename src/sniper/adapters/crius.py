"""Supplemental provider observations with checked chain, token and block scope.

These observations can reject an entry; they never turn missing LP ownership or
deployer-history proof into a pass. No paid fresh simulation is requested.
"""
from decimal import Decimal
import json
import time

from .evm import address, quantity, hash32
from .solana import request_json, SourceError, decimal_value
from sniper.models import SafetyEvidence


class CriusClient:
    def __init__(self, key='', *, transport=request_json, clock=time.time):
        if any(c.isspace() for c in key):
            raise ValueError('Invalid Crius credential format')
        self.key, self.transport, self.clock = key, transport, clock

    def get(self, token, endpoint):
        if endpoint not in ('security', 'liquidity'):
            raise ValueError('Unsupported evidence endpoint')
        headers = {'Accept': 'application/json', 'User-Agent': 'SNIPER-Research/0.1'}
        if self.key:
            headers['x-api-key'] = self.key
        result = self.transport('GET', 'https://api.crius.sh/v1/robinhood/token/' + address(token) + '/' + endpoint,
                                None, headers, 8)
        if self.key and self.key in json.dumps(result):
            raise SourceError('Credential material in provider response; discarded')
        return result

    def verify(self, payload, token, rpc, *, require_token=True):
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict) or not isinstance(payload.get('meta'), dict):
            raise SourceError('Invalid supplemental evidence envelope')
        data, meta = payload['data'], payload['meta']
        if require_token and address(data.get('token')) != address(token):
            raise SourceError('Supplemental token mismatch')
        if meta.get('chain') not in (None, 'robinhood', 4663, '4663'):
            raise SourceError('Supplemental chain mismatch')
        block, stamp = meta.get('block'), meta.get('block_ts')
        if type(block) is not int or block < 0 or type(stamp) is not int:
            raise SourceError('Supplemental block identity missing')
        rpc.verify_chain()
        header = rpc.call('eth_getBlockByNumber', [hex(block), False])
        if quantity(header['number']) != block or quantity(header['timestamp']) != stamp // 1000:
            raise SourceError('Supplemental block does not match RPC')
        if not 0 <= int(self.clock()) - stamp // 1000 <= 300:
            raise SourceError('Supplemental evidence stale or future')
        anchor = hash32(header['hash'])
        if meta.get('block_hash') is not None and hash32(meta['block_hash']) != anchor:
            raise SourceError('Supplemental anchor mismatch')
        return data, {'block': block, 'block_hash': anchor, 'block_timestamp': stamp // 1000,
                      'scope': 'RPC confirms block identity/time, not correctness of provider-derived classifications'}


def supplemental_screen(screen, pool, rpc, *, client):
    token = address(screen.asset_id.split(':')[-1])
    details, checks = dict(screen.details), dict(screen.checks)
    observation = {'provider': 'Crius', 'status': 'UNAVAILABLE', 'independently_verified_lp_control': False,
                   'deployer_history_complete': False}
    try:
        payload = client.get(token, 'security')
        data, anchor = client.verify(payload, token, rpc)
        contract = data.get('contract') or {}
        honeypot, tax = data.get('honeypot') or {}, data.get('tax') or {}
        danger = any(contract.get(k) is True for k in ('blacklist', 'mintable', 'pausable', 'can_change_limits',
                     'can_change_tax', 'is_proxy', 'upgradeable', 'self_destruct', 'trading_switch'))
        if danger:
            checks['supplemental_contract_controls'] = False
        if any(honeypot.get(k) is True for k in ('is_honeypot', 'selective')):
            checks['supplemental_honeypot'] = False
        for side in ('buy_pct', 'sell_pct', 'transfer_pct'):
            if tax.get(side) is not None and decimal_value(tax[side], side, maximum=Decimal(100)) > 0:
                checks['supplemental_' + side] = False
        observation.update(status='OBSERVED', anchor=anchor, raw_security=payload,
                           liquidity_scope='Token-wide summary does not establish controls for the selected pool')
        realized = tax.get('realized') or {}
        observation['warnings'] = []
        if realized.get('max_sell_pct_seen') is not None and decimal_value(realized['max_sell_pct_seen'], 'historical tax', maximum=Decimal(100)) > 0:
            observation['warnings'].append('Provider reports historical positive sell tax; transaction-level corroboration remains required')
        if client.key:
            try:
                liquidity = client.get(token, 'liquidity')
                lp, lp_anchor = client.verify(liquidity, token, rpc, require_token=False)
                pools = lp.get('pools')
                if not isinstance(pools, list):
                    raise SourceError('Liquidity pools missing')
                matched = [p for p in pools if isinstance(p, dict) and str(p.get('pool', '')).lower() == address(pool)]
                if len(matched) != 1:
                    raise SourceError('Selected pool liquidity evidence missing or ambiguous')
                observation.update(liquidity_status='PROVIDER_OBSERVATION', selected_pool=matched[0],
                                   liquidity_anchor=lp_anchor, raw_liquidity=liquidity)
            except Exception as exc:
                observation.update(liquidity_status='UNAVAILABLE', liquidity_error=type(exc).__name__)
        else:
            observation['liquidity_status'] = 'FREE_API_KEY_REQUIRED'
    except Exception as exc:
        observation['error'] = type(exc).__name__
    details['supplemental_evidence'] = observation
    return SafetyEvidence(screen.asset_id, screen.observed_at, checks, screen.source, details)


def supplement_from_runtime(screen, pool, rpc, runtime):
    key_path = runtime / 'crius-api-key.txt'
    settings_path = runtime / 'supplemental-evidence.json'
    enabled = key_path.exists()
    if settings_path.exists():
        enabled = enabled or json.loads(settings_path.read_text(encoding='utf-8')).get('crius') is True
    if not enabled:
        return screen
    key = key_path.read_text(encoding='utf-8-sig').strip() if key_path.exists() else ''
    with rpc.budget(20):
        return supplemental_screen(screen, pool, rpc, client=CriusClient(key))
