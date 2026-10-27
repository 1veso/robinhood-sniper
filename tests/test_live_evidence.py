from dataclasses import replace
from decimal import Decimal as D
import unittest
from sniper.adapters.evm import RpcClient, RpcError, FACTORY, WETH, USDG
from sniper.adapters.goplus import normalize, CONTROL_FLAGS, TRADE_FLAGS, combine_review
from sniper.adapters.v3_route import V3RouteClient, QUOTER
from sniper.models import MarketQuote, SafetyEvidence
from sniper.adapters.solana import SourceError

NOW = 1800000000
POOL = '0x' + 'aa' * 20
TOKEN = '0x' + '11' * 20

def word(value):
    return (value[2:] if isinstance(value, str) else f'{value:x}').zfill(64)


class SafetyTests(unittest.TestCase):
    def report(self):
        report = {key: '0' for key in CONTROL_FLAGS + TRADE_FLAGS}
        report.update(is_open_source='1', is_in_dex='1', buy_tax='0', sell_tax='0', transfer_tax='0',
                      creator_percent='0.01', holder_count='100', honeypot_with_same_creator='0',
                      holders=[{'address': '0x' + f'{i + 1:040x}', 'percent': '0.01'} for i in range(10)])
        return {'code': 1, 'result': {TOKEN: report}}

    def test_complete_provider_flags_leave_unproven_controls_unknown(self):
        result = normalize(TOKEN, self.report(), observed_at=NOW)
        self.assertTrue(result.checks['contract_controls'])
        self.assertTrue(result.checks['sellability'])
        self.assertTrue(result.checks['holder_distribution'])
        self.assertIsNone(result.checks['liquidity_controls'])
        self.assertIsNone(result.checks['deployer_history'])

    def test_missing_boolean_and_malformed_flags_never_pass(self):
        for value in (None, True, 0, ''):
            data = self.report()
            data['result'][TOKEN]['is_mintable'] = value
            self.assertIsNone(normalize(TOKEN, data, observed_at=NOW).checks['contract_controls'])
        data = self.report()
        data['result'][TOKEN]['sell_tax'] = '0.01'
        self.assertFalse(normalize(TOKEN, data, observed_at=NOW).checks['sellability'])

    def test_review_cannot_override_failure_or_refresh_old_evidence(self):
        data = self.report()
        data['result'][TOKEN]['is_proxy'] = '1'
        auto = normalize(TOKEN, data, observed_at=NOW)
        review = SafetyEvidence(auto.asset_id, NOW, {key: True for key in auto.checks}, 'test', {'report_pool_id': POOL})
        combined = combine_review(auto, review.as_dict(), now=NOW)
        self.assertFalse(combined.checks['contract_controls'])
        self.assertTrue(combined.checks['liquidity_controls'])
        self.assertEqual(combined.details['report_pool_id'], POOL)
        with self.assertRaises(ValueError):
            combine_review(auto, review.as_dict(), now=NOW + 301)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.reorg = False
        self.bad_factory = False
        self.headers = 0
        self.swaps = []
        self.fail_sell = False
        def transport(request):
            method, params = request['method'], request['params']
            result = None
            if method == 'eth_chainId': result = '0x1237'
            elif method == 'eth_blockNumber': result = hex(1005)
            elif method == 'eth_gasPrice': result = hex(1000000)
            elif method == 'eth_getCode': result = '0x6000'
            elif method == 'eth_getBlockByNumber':
                self.headers += 1
                result = {'number': hex(1000), 'timestamp': hex(NOW),
                          'hash': '0x' + ('bb' if self.reorg and self.headers > 1 else 'aa') * 32}
            elif method == 'eth_call':
                to, data = params[0]['to'], params[0]['data']
                self.assertEqual(params[1], hex(1000))
                selector = data[:10]
                if selector == '0xc45a0155': result = '0x' + word(TOKEN if self.bad_factory else FACTORY)
                elif selector == '0x4aa4a4fc': result = '0x' + word(WETH)
                elif selector == '0x0dfe1681': result = '0x' + word(WETH)
                elif selector == '0xd21220a7': result = '0x' + word(USDG)
                elif selector == '0xddca3f43': result = '0x' + word(100)
                elif selector == '0x1698ee82': result = '0x' + word(POOL)
                elif selector == '0x313ce567': result = '0x' + word(18 if to == WETH else 6)
                elif selector == '0x3850c7bd':
                    sqrt = int((D(2000) / D(10)**12).sqrt() * D(2)**96)
                    result = '0x' + ''.join(word(x) for x in (sqrt, 0, 0, 0, 0, 0, 1))
                elif selector == '0xc6a5026a':
                    source = '0x' + data[10:74][-40:]
                    amount = int(data[138:202], 16)
                    self.swaps.append((source, amount))
                    factor = D(2000) / D(10)**12 if source == WETH else D(10)**12 / D(2000)
                    output = 0 if self.fail_sell and source == WETH else int(D(amount) * factor * D('.9999'))
                    result = '0x' + ''.join(word(x) for x in (output, 1, 0, 50000))
            self.assertIsNotNone(result, (method, params))
            return {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        class Market:
            def quote(self, token, *, now, pool_id=None):
                return MarketQuote(token, pool_id or POOL, now, D(2000) if token == WETH else D(1), D(1000000))
        self.client = V3RouteClient(RpcClient(transport=transport), Market(), clock=lambda: NOW)

    def test_routes_are_pinned_sized_both_ways_and_include_cost_stress(self):
        quote = self.client.quote(WETH, POOL, max_notional='25', sell_quantity=D('.05'))
        self.assertTrue(quote.executable)
        self.assertGreater(quote.buy_cost_bps, 100)
        self.assertGreater(quote.sell_cost_bps, 100)
        self.assertGreater(quote.gas_usd, 0)
        self.assertLess(quote.route_impact_bps, D('.1'))  # The LP fee is charged separately, not mislabeled impact.
        self.assertLessEqual(quote.route_max_notional_usd, D(25))
        self.assertGreaterEqual(quote.route_max_sell_quantity, D('.05'))
        self.assertEqual(len(self.swaps), 2)
        self.assertEqual(self.client.last_evidence['block'], 1000)

    def test_reorg_wrong_quoter_identity_or_failed_sell_never_return_quote(self):
        for failure in ('reorg', 'bad_factory', 'fail_sell'):
            self.setUp()
            setattr(self, failure, True)
            with self.subTest(failure=failure), self.assertRaises(RpcError):
                self.client.quote(WETH, POOL, max_notional='25')

    def test_partial_route_bounds_are_invalid(self):
        with self.assertRaises(ValueError):
            MarketQuote(TOKEN, POOL, NOW, 1, 10000, route_max_notional_usd=25)

    def test_unindexed_pool_uses_pinned_balances_and_still_requires_sell_quote(self):
        original_transport = self.client.rpc.transport
        def transport(request):
            if request['method'] == 'eth_call' and request['params'][0]['data'].startswith('0x70a08231'):
                self.assertEqual(request['params'][1], hex(1000))
                self.assertEqual(request['params'][0]['data'][10:], POOL[2:].zfill(64))
                amount = 100 * 10**18 if request['params'][0]['to'] == WETH else 200000 * 10**6
                return {'jsonrpc':'2.0','id':request['id'],'result':'0x'+word(amount)}
            return original_transport(request)
        self.client.rpc.transport = transport
        original_quote = self.client.market.quote
        def market(token, *, now, pool_id=None):
            if pool_id:
                raise SourceError('no valid matching pool; no substitute quote')
            return original_quote(token, now=now)
        self.client.market.quote = market
        quote = self.client.quote(WETH, POOL, max_notional='25')
        self.assertAlmostEqual(quote.liquidity_usd, D(400000), places=10)
        self.assertIn('pinned ERC20', self.client.last_evidence['liquidity_source'])
        self.fail_sell = True
        with self.assertRaises(RpcError):
            self.client.quote(WETH, POOL, max_notional='25')
