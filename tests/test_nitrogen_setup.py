from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from sniper.nitrogen.book import Book
from sniper.nitrogen.config import load,encode,digest
from sniper.nitrogen.setup import configure_wallets

ROOT=Path(__file__).resolve().parents[1]
WALLET='AnCMde5PQzP7hVQmh5TeeCiUgGAqTY8kK9d9o91nEs3Q'


class InitialWalletSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'nitrogen_bot').mkdir()
        self.cfg=load(ROOT)
        self.cfg['tracked_wallets']=[]
        (self.root/'nitrogen_bot/config.json').write_text(encode(self.cfg))
        self.book=Book(self.root,self.cfg,initialize=True);self.addCleanup(self.book.close)

    def test_initial_configuration_keeps_identity_cash_and_all_other_policy_values(self):
        identity=self.book.identity;cash=self.book.get('cash');created=self.book.get('created_at')
        result=configure_wallets(self.book,[WALLET],'Test authorization','Recorded public candidate source')
        expected=deepcopy(self.cfg);expected['tracked_wallets']=[WALLET]
        self.assertEqual(self.book.cfg,expected);self.assertEqual(load(self.root),expected)
        self.assertEqual(self.book.identity,identity);self.assertEqual(self.book.get('cash'),cash)
        self.assertEqual(self.book.get('created_at'),created)
        self.assertEqual(self.book.get('config_hash'),digest(expected))
        self.assertFalse(result['performance_qualified'])
        self.assertFalse(configure_wallets(self.book,[WALLET],'Test authorization','Same source')['changed'])

    def test_activity_or_missing_authorization_blocks_reconfiguration(self):
        before=(self.root/'nitrogen_bot/config.json').read_bytes()
        with self.assertRaises(ValueError):configure_wallets(self.book,[WALLET],'','Source')
        self.book.db.execute('INSERT INTO api_requests VALUES(?,?)',('fixture','{}'))
        with self.assertRaises(ValueError):configure_wallets(self.book,[WALLET],'Test','Source')
        self.assertEqual((self.root/'nitrogen_bot/config.json').read_bytes(),before)
        self.assertEqual(self.book.cfg,self.cfg)


if __name__=='__main__':unittest.main()
