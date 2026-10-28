import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rug_desk.book import Book
from rug_desk.model_gate import binding,status
from rug_desk import config

class ModelGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'rug_desk'
        self.book=Book(self.root,initialize=True);self.book.close()
        (self.root.parent/'runtime').mkdir();(self.root.parent/'runtime/xai-api-key.txt').write_text('OFFLINE-TEST')
        self.bound=binding(self.root);self.now=1793190000
    def tearDown(self):self.temp.cleanup()
    def write(self,name,value):(self.root/'runtime'/name).write_text(json.dumps(value))
    def authorize_fixture(self):
        self.write('paid-ai.json',{'enabled':True,'operator_authorized':True,'approval_reference':'offline-test','binding':self.bound})
        for name in ['inference-test.json','x-search-test.json']:
            self.write(name,{'validated':True,'binding':self.bound,'model':config.MODEL,'tested_at':self.now})
    def test_disabled_does_not_read_credential(self):
        with patch('rug_desk.model_gate.binding',side_effect=AssertionError('credential read')):
            self.assertFalse(status(self.root,self.now)['enabled'])
    def test_missing_and_failed_proofs_fail(self):
        self.authorize_fixture();self.assertTrue(status(self.root,self.now)['enabled'])
        self.write('x-search-test.json',{'validated':False})
        self.assertFalse(status(self.root,self.now)['enabled'])
    def test_key_account_configuration_and_proof_expiry_revoke(self):
        self.authorize_fixture();self.assertFalse(status(self.root,self.now+8*86400)['enabled'])
        (self.root.parent/'runtime/xai-api-key.txt').write_text('ROTATED-TEST')
        self.assertFalse(status(self.root,self.now)['enabled'])
    def test_malformed_authority_is_closed(self):
        self.write('paid-ai.json',[]);self.assertFalse(status(self.root,self.now)['enabled'])
