import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sniper.switchover import inventory
from sniper.execution_service import validate_deployment
from sniper.solana_wire import b58encode


class SwitchoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        (self.root/'runtime/live-preparation').mkdir(parents=True)
        (self.root/'config').mkdir()
        self.wallet=b58encode(bytes([1])*32)

    def tearDown(self): self.tmp.cleanup()

    def save(self, path, data):
        target=self.root/path; target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(data))
        return target

    def sample(self):
        (self.root/'runtime/solana-trading-address.txt').write_text(self.wallet)
        key=self.root/'runtime/jupiter-server-api-key.txt'; key.write_text('secret-fixture'); os.utime(key,(90,90))
        data={'observed_at':100,'wallet_sha256':hashlib.sha256(self.wallet.encode()).hexdigest(),
            'jupiter_key_source':'jupiter-server-api-key.txt',
            'probes':[{'transaction_sha256':'abc','genesis_verified':True,'simulation':{'succeeded':True}}]*2}
        self.save('runtime/live-preparation/solana-preflight.json',data)
        return data

    def test_positive_setup_cannot_become_execution_authority(self):
        self.sample(); result=inventory(self.root,'solana',101)
        states={c['name']:c['state'] for c in result['checks']}
        self.assertEqual(states['Unsigned buy/sell builds'],'VERIFIED')
        self.assertEqual(states['Wallet-specific simulation'],'VERIFIED_SETUP_ONLY')
        self.assertEqual(states['Solana automatic executor'],'IMPLEMENTED_UNREVIEWED')
        self.assertFalse(result['live_execution_ready']); self.assertFalse(result['automatic_promotion'])
        self.assertFalse(result['per_trade_approval_required'])

    def test_stale_future_wallet_or_key_change_invalidates_evidence(self):
        data=self.sample()
        for now in (99,3701):
            result=inventory(self.root,'solana',now)
            self.assertEqual(result['checks'][1]['state'],'STALE_OR_MISSING')
        (self.root/'runtime/solana-trading-address.txt').write_text(b58encode(bytes([2])*32))
        self.assertEqual(inventory(self.root,'solana',101)['checks'][1]['state'],'STALE_OR_MISSING')
        self.sample(); os.utime(self.root/'runtime/jupiter-server-api-key.txt',(102,102))
        self.assertEqual(inventory(self.root,'solana',103)['checks'][1]['state'],'STALE_OR_MISSING')
        self.sample(); data['jupiter_key_source']='jupiter-api-key.txt'
        self.save('runtime/live-preparation/solana-preflight.json',data)
        self.assertEqual(inventory(self.root,'solana',101)['checks'][1]['state'],'STALE_OR_MISSING')

    def test_inventory_does_not_read_secrets_or_make_missing_ledgers(self):
        self.sample(); real=Path.read_text
        def guarded(path,*a,**kw):
            if 'api-key' in path.name: raise AssertionError('Secret read')
            return real(path,*a,**kw)
        with patch.object(Path,'read_text',guarded):
            result=inventory(self.root,'solana',101)
        text=json.dumps(result)
        self.assertNotIn('secret-fixture',text); self.assertNotIn(self.wallet,text)
        self.assertFalse((self.root/'rug_desk/runtime/book.sqlite3').exists())

    def test_evm_manifest_or_proxy_mismatch_cannot_pass_pins(self):
        wallet='0x'+'1'*40
        (self.root/'runtime/evm-trading-address.txt').write_text(wallet)
        manifest=self.save('config/executor-policy.example.json',{'deployment_authorized':False})
        data={'observed_at':100,'wallet_sha256':hashlib.sha256(wallet.encode()).hexdigest(),
            'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'chain_id':4663,'canonical_verified':True,'genesis_verified':True,
            'contracts':{k:{'matches_manifest':True} for k in ('router','factory','quoter','base_token')},
            'proxies':{'USDG':{'matches_selected_manifest':True,'beacon_zero':True}},'finality_lag_seconds':978}
        self.save('runtime/live-preparation/evm-preflight.json',data)
        def pin():return next(c for c in inventory(self.root,'evm',101)['checks'] if c['name']=='EVM deployment pins')['state']
        self.assertEqual(pin(),'VERIFIED')
        data['proxies']['USDG']['matches_selected_manifest']=False
        self.save('runtime/live-preparation/evm-preflight.json',data); self.assertEqual(pin(),'UNVERIFIED')
        manifest.write_text('{}'); self.assertEqual(pin(),'UNVERIFIED')

    def test_deployment_requires_explicit_latency_review(self):
        policy={'deployment_authorized':True,'acceptance_mode':False,'chain_id':4663,'execution_target':'automatic',
            'minimum_buyers':2,'min_liquidity_usd':'30000','required_live_safety_checks':['rug_screen_available'],
            'capital_raw':1000,'max_order_raw':5,'max_total_exposure_raw':20,'daily_loss_raw':20,
            'max_open_positions':4,'max_entries_per_day':6,'require_finalized':True,
            **{k:'test-review' for k in ('user_approval_reference','independent_review_reference','observation_source_id','keystore_path')}}
        with self.assertRaises(ValueError): validate_deployment(policy)
        policy['finality_review_reference']='offline-fixture'
        for maximum in (None,False,0,3601,'60'):
            policy['max_finality_lag_seconds']=maximum
            with self.assertRaises(ValueError): validate_deployment(policy)
        policy['max_finality_lag_seconds']=60
        self.assertEqual(validate_deployment(policy)['max_finality_lag_seconds'],60)


if __name__=='__main__':unittest.main()
