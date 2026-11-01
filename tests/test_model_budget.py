import json
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from sniper.advisor import DailyBudget, AdvisoryError
from sniper.model_budget import shared_grok_limit
from sniper.model_budget import jev_uncapped


class InstallationBudgetTests(unittest.TestCase):
    def test_explicit_uncapped_jev_preserves_accounting_and_duplicate_protection(self):
        self.assertFalse(jev_uncapped(self.root))
        auth=self.root/'runtime/jev-spending.json'
        auth.write_text(json.dumps({'version':1,'operator_authorized':True,'daily_limit_usd':None,
            'approval_reference':'Explicit test authorization'}))
        self.assertTrue(jev_uncapped(self.root))
        path=self.root/'runtime/jev.sqlite3'
        with closing(DailyBudget(path,D('.1'),uncapped=jev_uncapped(self.root))) as budget:
            budget.reserve('one',D('2'),1793530000)
        with closing(DailyBudget(path,D('.1'),uncapped=jev_uncapped(self.root))) as budget:
            budget.reserve('two',D('2'),1793530000)
            with self.assertRaises(AdvisoryError):budget.reserve('one',D('2'),1793530000)
            self.assertEqual(budget.db.execute('SELECT COUNT(*) FROM advisory_reservations').fetchone()[0],2)
        auth.write_text('{}')
        with self.assertRaises(ValueError):jev_uncapped(self.root)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'runtime').mkdir()

    def save(self, limit):
        (self.root/'runtime/model-budgets.json').write_text(json.dumps({
            'version': 1, 'grok_daily_usd': limit,
            'approved_at': 1793530000, 'approval_reference': 'test operator approval'}))

    def test_raise_preserves_same_day_reservations_and_applies_to_new_connections(self):
        ledger = self.root/'runtime/budget.sqlite3'
        with closing(DailyBudget(ledger, shared_grok_limit(self.root))) as budget:
            budget.reserve('before', D('1'), 1793530000)
            with self.assertRaises(AdvisoryError): budget.reserve('blocked', D('.5'), 1793530000)
        self.save('5')
        with closing(DailyBudget(ledger, shared_grok_limit(self.root))) as budget:
            budget.reserve('after', D('4'), 1793530000)
        with closing(DailyBudget(ledger, shared_grok_limit(self.root))) as other:
            with self.assertRaises(AdvisoryError): other.reserve('over', D('.01'), 1793530000)
            self.assertEqual(other.db.execute('select count(*) from advisory_reservations').fetchone()[0], 2)

    def test_invalid_override_cannot_silently_use_another_limit(self):
        self.assertEqual(shared_grok_limit(self.root), D('1'))
        for value in ('NaN', 'Infinity', '-1', 'invalid', 5, True):
            self.save(value)
            with self.assertRaises(ValueError): shared_grok_limit(self.root)
        (self.root/'runtime/model-budgets.json').write_text('{}')
        with self.assertRaises(ValueError): shared_grok_limit(self.root)


if __name__ == '__main__': unittest.main()
