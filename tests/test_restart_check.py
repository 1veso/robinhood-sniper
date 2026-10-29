import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import restart_check


class RestartChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        def write(path, value):
            p = self.root/path; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(value))
        self.write = write
        for path in ('runtime/active-exploration.json','runtime/exploration/trial.json'):
            write(path, {'account_id':'paper'})
        self.db('runtime/exploration/ledger.sqlite3', {'account_id':'paper','execution_mode':'paper'})
        for name in ('rug_desk','neon_bot'):
            identity={'account_id':name,'mode':'paper'}
            write(name+'/runtime/account.json',identity)
            write(name+'/runtime/paid-ai.json',{'enabled':False})
            write(name+'/runtime/execution-cost-policy.json',{'model':'fixture'})
            self.db(name+'/runtime/book.sqlite3', {'identity':json.dumps(identity)})
        write('runtime/jev-review/settings.json',{'paid_authorized':False})
        (self.root/'rug_desk/config.py').write_text('# fixture')
        for name in ('telegram-bot-token.txt','quote-rpc-url.txt','observation-rpc-url.txt'):
            (self.root/'runtime'/name).write_text('https://fixture.invalid/PRIVATE_KEY')

    def db(self, path, metadata):
        with closing(sqlite3.connect(self.root/path)) as db, db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO metadata VALUES(?,?)',metadata.items())

    def tearDown(self): self.tmp.cleanup()

    def test_read_only_local_check_and_protected_policy_hashes(self):
        before=(self.root/'runtime/exploration/ledger.sqlite3').read_bytes()
        with patch.object(restart_check.urllib.request,'urlopen',side_effect=AssertionError('network')):
            result=restart_check.inspect(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual((self.root/'runtime/exploration/ledger.sqlite3').read_bytes(),before)
        self.assertNotIn('PRIVATE_KEY',json.dumps(result))

    def test_paid_or_account_mismatch_blocks_even_probe_calls(self):
        self.write('neon_bot/runtime/paid-ai.json',{'enabled':True})
        self.write('runtime/active-exploration.json',{'account_id':'wrong'})
        with patch.object(restart_check.urllib.request,'urlopen') as network:
            result=restart_check.inspect(self.root,network=True)
        network.assert_not_called()
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks']['paper_identity']['ok'])

    def test_network_errors_are_fail_closed_and_do_not_leak_keys(self):
        with patch.object(restart_check.urllib.request,'urlopen',side_effect=URLError('PRIVATE_KEY')):
            result=restart_check.inspect(self.root,network=True)
        self.assertFalse(result['ok'])
        self.assertNotIn('PRIVATE_KEY',json.dumps(result))
        self.assertEqual(result['orders'],0)


if __name__=='__main__': unittest.main()
