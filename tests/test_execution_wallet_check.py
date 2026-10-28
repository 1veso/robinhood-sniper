import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from sniper.execution_wallet_check import validate_wallet
from sniper.execution import validate_policy
from test_execution import policy,WALLET,TOKEN,POOL


class ExecutionWalletCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'runtime').mkdir();(self.root/'runtime/evm-trading-address.txt').write_text(WALLET)
        self.p=policy(deployment_authorized=True,acceptance_mode=False,execution_target='automatic',
            user_approval_reference='OFFLINE-ONLY',independent_review_reference='OFFLINE-ONLY',
            observation_source_id='fixture',signer_provider='windows_dpapi',signer_path='NEVER-READ',
            finality_review_reference='fixture',max_finality_lag_seconds=1200,require_finalized=True,
            required_live_safety_checks=['rug_screen_available'],base_decimals=6,
            validation_pair={'mint':TOKEN,'pool':POOL,'buy_input_raw':5,'sell_input_raw':100})
        self.rpc=Mock()
        self.rpc.call.side_effect=lambda method,args: '0x' if method=='eth_getCode' else ('0x1000' if method=='eth_getBalance' else '0x0')
        self.a=Mock();self.a._anchor.return_value={'block_number':100}
        self.a._balance.return_value=1000;self.a._uint.return_value=6
        self.a.check_finality_latency.return_value={'lag_seconds':900}
        self.a.quote_swap.side_effect=lambda source,target,pool,amount:{'token_in':source,'token_out':target,'pool':pool,'amount_in_raw':str(amount)}
    def tearDown(self):self.temp.cleanup()
    def run_check(self):
        with patch('sniper.execution_wallet_check.EvmExecutionAdapter',return_value=self.a):
            return validate_wallet(self.root,self.p,rpc=self.rpc,clock=lambda:1000)
    def test_no_authority_stops_before_rpc_or_signer(self):
        self.p['deployment_authorized']=False
        with self.assertRaises(ValueError):self.run_check()
        self.assertFalse(self.rpc.call.called);self.assertFalse(self.a.prepare.called)
    def test_approval_prerequisite_is_not_a_successful_swap(self):
        self.a.prepare.return_value={'requires_approval':True}
        result=self.run_check()
        self.assertFalse(result['validated']);self.assertEqual(len(result['probes']),2)
        self.assertEqual((result['signing_calls'],result['broadcast_calls']),(0,0))
        self.assertFalse(self.a.sign.called);self.assertFalse(self.a.broadcast.called)
    def test_both_actual_simulations_required(self):
        self.a.prepare.side_effect=[{'anchor':{'block_number':100},'max_fee_wei':'5','simulation_result':'0x10'},ValueError('missing target balance')]
        self.assertFalse(self.run_check()['validated'])
        self.a.prepare.side_effect=None
        self.a.prepare.return_value={'anchor':{'block_number':100},'max_fee_wei':'5','simulation_result':'0x10'}
        result=self.run_check();self.assertTrue(result['validated'])
        self.assertEqual(result['policy_hash'],validate_policy(self.p)['policy_hash'])
        self.assertFalse((self.root/'runtime/executor').exists())
    def test_unfunded_or_different_wallet_fails(self):
        self.a._balance.return_value=0
        with self.assertRaisesRegex(ValueError,'below allocation'):self.run_check()
        (self.root/'runtime/evm-trading-address.txt').write_text(TOKEN)
        with self.assertRaisesRegex(ValueError,'differs'):self.run_check()
