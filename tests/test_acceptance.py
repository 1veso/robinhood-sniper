from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from acceptance import validate_cycle, ExecutionValidationEngine
from sniper.exploration import ExploratoryEngine
from sniper.ledger import Ledger
from sniper.models import SafetyEvidence
from sniper.policy import RiskPolicy
from test_engine import quote, NOW
from test_evm import TOKEN, POOL


class AcceptanceTests(unittest.TestCase):
    def run_validation(self, runtime, *, fail_exit=False, now=NOW):
        clock = [now]
        class Routes:
            last_evidence = {'source': 'offline fixture'}
            def quote(self, token, pool, **kwargs):
                if kwargs.get('sell_quantity') and fail_exit:
                    raise TimeoutError('offline outage')
                clock[0] += 2
                return quote('eip155:4663/erc20:' + token, clock[0], pool_id=pool)
        def screen(token, pool):
            checks = {k: True for k in RiskPolicy().required_safety_checks}
            checks['rug_screen_available'] = True
            return SafetyEvidence('eip155:4663/erc20:' + token, clock[0], checks, 'offline fixture')
        return validate_cycle(runtime, TOKEN, POOL, 'TEST', route=Routes(), screen=screen,
            clock=lambda: clock[0], notify=lambda *args: {'outbox': {'sent': 2}})

    def test_round_trip_reconciles_cash_and_restart_never_adds_a_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            result = self.run_validation(runtime)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual([f['action'] for f in result['fills']], ['OPEN', 'CLOSE'])
            self.assertEqual(result['fills'][1]['reason'], 'time_stop')
            self.assertEqual(D(result['snapshot']['cash']), D(1000) + sum(D(f['cash_delta']) for f in result['fills']))
            again = self.run_validation(runtime, now=NOW+20)
            self.assertEqual(again['fills'], result['fills'])
            self.assertFalse(result['strategy_result'])

    def test_exit_outage_keeps_holding_and_restart_closes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            result = self.run_validation(runtime, fail_exit=True)
            self.assertEqual(result['status'], 'PENDING')
            self.assertEqual(len(result['fills']), 1)
            self.assertEqual(result['snapshot']['open_positions'], 1)
            recovered = self.run_validation(runtime, now=NOW+20)
            self.assertEqual(recovered['status'], 'COMPLETE')

    def test_validation_engine_cannot_open_trial_account(self):
        ledger = Ledger(':memory:', D(1000))
        try:
            policy = RiskPolicy()
            ExploratoryEngine(ledger, policy)
            with self.assertRaisesRegex(ValueError, 'evaluation mode'):
                ExecutionValidationEngine(ledger, policy)
        finally:
            ledger.close()
