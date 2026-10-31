import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sniper.research_budget import BlockscoutBudget, BudgetUnavailable

URL='https://api.blockscout.com/v2/api?chain_id=4663&module=account&action=txlist'
DAY='2026-10-31'


class ResearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'cache';self.root.mkdir()
        self.key='fixture-only';self.path=self.root/'blockscout-usage.json'
        self.policy={'schema':'blockscout_free_capacity_v1','operator_authorized':True,
            'paid_allowed':False,'key_sha256':hashlib.sha256(self.key.encode()).hexdigest(),
            'request_cost_credits':20,'daily_request_limit':4000,'daily_credit_limit':80000,
            'verified_rps':5,'verified_remaining_credits':80000,'verified_day':DAY,
            'verification_reference':'isolated fixture'}
        self.clock=patch.object(BlockscoutBudget,'day',return_value=DAY);self.clock.start();self.addCleanup(self.clock.stop)

    def authorize(self):
        (self.root.parent/'blockscout-free-capacity.json').write_text(json.dumps(self.policy))

    def state(self, count, **extra):
        self.path.write_text(json.dumps({'day':DAY,'requests':count,**extra}))

    def read(self):return json.loads(self.path.read_text())

    def test_without_authorization_original_daily_cap_remains(self):
        self.state(1000)
        with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,self.key).reserve(URL)
        self.assertEqual(self.read()['requests'],1000)

    def test_activation_preserves_used_count_and_reserves_before_response(self):
        self.authorize();self.state(1001)
        b=BlockscoutBudget(self.root,self.key);b.reserve(URL)
        self.assertEqual(self.read()['requests'],1002)
        self.assertEqual(self.read()['reserved_credits'],20040)
        b.observe({'x-credits-remaining':'79960','x-ratelimit-limit':'5'})
        self.assertEqual(self.read()['provider_credits_remaining'],79960)

    def test_local_ceiling_blocks_without_dispatch_or_counter_reset(self):
        self.authorize();self.state(3999)
        b=BlockscoutBudget(self.root,self.key);b.reserve(URL)
        self.assertEqual(self.read()['reserved_credits'],80000)
        with self.assertRaises(BudgetUnavailable):b.reserve(URL)
        self.assertEqual(self.read()['requests'],4000)

    def test_other_consumers_can_reduce_available_capacity(self):
        self.authorize();self.state(1001)
        b=BlockscoutBudget(self.root,self.key);b.reserve(URL)
        b.observe({'x-credits-remaining':'0','x-ratelimit-limit':'5'})
        with self.assertRaisesRegex(BudgetUnavailable,'no overage'):b.reserve(URL)

    def test_missing_or_changed_plan_headers_halt_without_refunding_request(self):
        for headers in ({},{'x-credits-remaining':'90000','x-ratelimit-limit':'15'}):
            self.authorize();self.state(1001)
            b=BlockscoutBudget(self.root,self.key);b.reserve(URL)
            with self.assertRaises(BudgetUnavailable):b.observe(headers)
            with self.assertRaises(BudgetUnavailable):b.reserve(URL)
            self.assertEqual(self.read()['requests'],1002)

    def test_unknown_response_is_durable_across_process_and_day(self):
        self.authorize();self.state(1001)
        b=BlockscoutBudget(self.root,self.key);b.reserve(URL);b.unknown()
        with patch.object(BlockscoutBudget,'day',return_value='2026-11-01'):
            with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,self.key).reserve(URL)
        self.assertEqual(self.read()['requests'],1002)

    def test_normal_utc_reset_starts_a_new_count(self):
        self.authorize();self.state(4000,provider_credits_remaining=0)
        with patch.object(BlockscoutBudget,'day',return_value='2026-11-01'):
            b=BlockscoutBudget(self.root,self.key);b.reserve(URL)
            b.observe({'x-credits-remaining':'99980','x-ratelimit-limit':'5'})
        self.assertEqual(self.read()['requests'],1)
        self.assertEqual(self.read()['day'],'2026-11-01')

    def test_credential_change_or_wrong_endpoint_cannot_use_raised_allowance(self):
        self.authorize();self.state(1001)
        with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,'changed')
        for url in (URL.replace('4663','1'),URL.replace('txlist','balance'),URL.replace('https:','http:'),URL.replace('api.blockscout.com','other.example')):
            with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,self.key).reserve(url)
        self.assertEqual(self.read()['requests'],1001)

    def test_malformed_usage_and_policy_fail_closed(self):
        self.authorize();self.state(True)
        with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,self.key).reserve(URL)
        self.policy['daily_request_limit']=5000;self.authorize()
        with self.assertRaises(BudgetUnavailable):BlockscoutBudget(self.root,self.key)

    def test_real_cache_integration_reserves_once_and_records_provider_capacity(self):
        from sniper.adapters.wallet_history import ResearchCache
        self.authorize();self.state(1001)
        response=io.BytesIO(json.dumps({'status':'1','result':[]}).encode())
        response.headers={'x-credits-remaining':'79960','x-ratelimit-limit':'5'}
        cache=ResearchCache(self.root,None,blockscout_key=self.key)
        with patch('urllib.request.urlopen',return_value=response) as http:
            self.assertEqual(cache.get(URL),{'status':'1','result':[]})
            self.assertEqual(cache.get(URL),{'status':'1','result':[]})
        self.assertEqual(http.call_count,1)
        self.assertEqual(self.read()['requests'],1002)
        self.assertEqual(self.read()['provider_credits_remaining'],79960)

    def test_timeout_cannot_blindly_retry_under_expanded_allowance(self):
        from sniper.adapters.wallet_history import ResearchCache, ResearchPending
        self.authorize();self.state(1001)
        with patch('urllib.request.urlopen',side_effect=TimeoutError('fixture')) as http:
            for _ in range(2):
                cache=ResearchCache(self.root,None,blockscout_key=self.key)
                with self.assertRaises(ResearchPending):cache.get(URL)
        self.assertEqual(http.call_count,1)
        self.assertEqual(self.read()['requests'],1002)
