import copy
from unittest.mock import Mock
import unittest
from sniper.adapters.crius import CriusClient, supplemental_screen
from sniper.models import SafetyEvidence
from test_evm import TOKEN, POOL, NOW, bh


class SupplementalTests(unittest.TestCase):
    def setup_inputs(self):
        screen = SafetyEvidence('eip155:4663/erc20:' + TOKEN, NOW,
            {'liquidity_controls': None, 'deployer_history': None, 'contract_controls': True}, 'fixture')
        payload = {'data': {'token': TOKEN, 'contract': {}, 'tax': {'sell_pct': 0},
                           'honeypot': {'is_honeypot': False}},
                   'meta': {'chain': 'robinhood', 'block': 105, 'block_ts': NOW * 1000}}
        rpc = Mock()
        rpc.call.return_value = {'number': hex(105), 'timestamp': hex(NOW), 'hash': bh(105)}
        return screen, payload, rpc

    def test_observation_does_not_invent_lp_or_deployer_pass(self):
        s, payload, rpc = self.setup_inputs()
        client = CriusClient(transport=lambda *args: payload, clock=lambda: NOW)
        out = supplemental_screen(s, POOL, rpc, client=client)
        self.assertIsNone(out.checks['liquidity_controls'])
        self.assertIsNone(out.checks['deployer_history'])
        self.assertEqual(out.details['supplemental_evidence']['status'], 'OBSERVED')
        self.assertEqual(out.details['supplemental_evidence']['liquidity_status'], 'FREE_API_KEY_REQUIRED')

    def test_known_current_tax_and_changeable_contract_controls_block(self):
        s, payload, rpc = self.setup_inputs()
        payload['data']['contract']['can_change_tax'] = True
        payload['data']['tax']['sell_pct'] = 4
        out = supplemental_screen(s, POOL, rpc, client=CriusClient(transport=lambda *args: payload, clock=lambda: NOW))
        self.assertFalse(out.checks['supplemental_contract_controls'])
        self.assertFalse(out.checks['supplemental_sell_pct'])

    def test_stale_wrong_token_wrong_chain_and_block_are_unavailable(self):
        for change in ('stale', 'token', 'chain', 'block'):
            s, payload, rpc = self.setup_inputs()
            if change == 'stale':
                payload['meta']['block_ts'] = (NOW-301)*1000
                rpc.call.return_value['timestamp'] = hex(NOW-301)
            if change == 'token': payload['data']['token'] = POOL
            if change == 'chain': payload['meta']['chain'] = 'ethereum'
            if change == 'block': rpc.call.return_value['hash'] = 'invalid'
            out = supplemental_screen(s, POOL, rpc, client=CriusClient(transport=lambda *args: payload, clock=lambda: NOW))
            self.assertEqual(out.details['supplemental_evidence']['status'], 'UNAVAILABLE')
            self.assertEqual(out.checks, s.checks)

    def test_key_never_persisted_when_provider_reflects_it(self):
        s, payload, rpc = self.setup_inputs()
        payload['debug'] = 'test-secret'
        client = CriusClient('test-secret', transport=lambda *args: payload, clock=lambda: NOW)
        out = supplemental_screen(s, POOL, rpc, client=client)
        self.assertNotIn('test-secret', str(out.as_dict()))
        self.assertEqual(out.details['supplemental_evidence']['status'], 'UNAVAILABLE')

    def test_liquidity_is_bound_to_selected_pool_and_stays_provider_evidence(self):
        s, payload, rpc = self.setup_inputs()
        lp = {'data': {'pools': [{'pool': POOL, 'summary': {'locked_pct': 100}}]}, 'meta': payload['meta']}
        client = CriusClient('test-key', transport=lambda method,url,*args: lp if url.endswith('liquidity') else payload, clock=lambda: NOW)
        out = supplemental_screen(s, POOL, rpc, client=client)
        self.assertEqual(out.details['supplemental_evidence']['liquidity_status'], 'PROVIDER_OBSERVATION')
        self.assertIsNone(out.checks['liquidity_controls'])
        lp['data']['pools'][0]['pool'] = TOKEN
        out = supplemental_screen(s, POOL, rpc, client=client)
        self.assertEqual(out.details['supplemental_evidence']['liquidity_status'], 'UNAVAILABLE')
