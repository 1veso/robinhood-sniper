from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from sniper.signer_vault import protect,load_signer
from test_solana_executor import fixture,KEY
from test_execution import policy

@unittest.skipUnless(os.name=='nt','Windows DPAPI acceptance only')
class SignerVaultTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'signer.dpapi.json'
    def tearDown(self):self.temp.cleanup()
    def test_sol_roundtrip_policy_binding_tamper_and_exclusive_creation(self):
        p=fixture()[2];result=protect(self.path,bytes(KEY),p,acceptance=True)
        self.assertEqual(result['signing_calls'],0);self.assertEqual(str(load_signer(self.path,p).pubkey()),str(KEY.pubkey()))
        self.assertNotIn(str(KEY),self.path.read_text())
        with self.assertRaises(FileExistsError):protect(self.path,bytes(KEY),p,acceptance=True)
        changed=deepcopy(p);changed['capital_raw']+=1
        with self.assertRaises(ValueError):load_signer(self.path,changed)
        raw=json.loads(self.path.read_text());raw['ciphertext']=raw['ciphertext'][:-4]+'AAAA';self.path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):load_signer(self.path,p)
    def test_evm_roundtrip_and_wrong_wallet(self):
        from eth_account import Account
        secret=bytes([13])*32;p=policy(wallet=Account.from_key(secret).address.lower())
        protect(self.path,secret,p,acceptance=True);self.assertEqual(load_signer(self.path,p),secret)
        with self.assertRaises(ValueError):protect(self.path.parent/'bad.json',secret,policy(),acceptance=True)
    def test_live_requires_separate_explicit_provisioning(self):
        p=fixture()[2];p['acceptance_mode']=False
        with self.assertRaises(ValueError):protect(self.path,bytes(KEY),p)
        self.assertFalse(self.path.exists())
