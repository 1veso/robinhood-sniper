from copy import deepcopy
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

from sniper.engine import PaperEngine
from sniper.ledger import Ledger, json_text
from sniper.radeon_exits import BoundRadeonExits, RUNNER, KEY, activate, binding, revision, validate
from test_engine import NOW, candidate, quote
from sniper.policy import RiskPolicy


class ExitEngine(BoundRadeonExits, PaperEngine):
    pass


class RadeonExitTests(unittest.TestCase):
    def setUp(self):
        self.l = Ledger(':memory:', D(1000))
        self.policy = RiskPolicy()
        self.e = ExitEngine(self.l, self.policy)
        self.account = self.l._get('account_id')
        self.manifest = {'account_id': self.account, 'source_id': 'source', 'entry_revision': 'entry',
                         'policy_version': self.policy.version, 'started_at': NOW-100, 'ends_at': NOW+100000}
        self.l._set('trial_manifest', json.dumps(self.manifest, sort_keys=True))
        self.l._set('entry_revision', 'entry')

    def tearDown(self):
        self.l.close()

    def activate(self, **kw):
        args = dict(now=NOW, expected_account=self.account, expected_source='source',
                    expected_entry_revision='entry', review_reference='isolated test')
        args.update(kw)
        return activate(self.l, self.policy, RUNNER, **args)

    def buy(self):
        self.assertEqual(self.e.consider(candidate(), quote(), NOW)['verdict'], 'PAPER_BUY')
        return self.l._positions()[0]

    def tick(self, p, gain, t, **kw):
        q = quote(now=t, price=str(p['entry_price'] * (1+D(gain)/100)), **kw)
        return self.e.watch({q.asset_id:q},t)

    def test_ladder_sells_original_fractions_and_retains_half_runner_across_restart(self):
        self.activate();p=self.buy()
        for i,gain in enumerate((110,310,610),1):
            result=self.tick(p,gain,NOW+i)
            self.assertEqual(result[0]['reason'],'take_profit_'+str(i))
            self.assertEqual(result[0]['quantity'],p['qty0']*D(('.1','.2','.2')[i-1]))
            self.e=ExitEngine(self.l,self.policy)
        remaining=self.l._positions()[0]
        self.assertEqual(remaining['qty'],p['qty0']*D('.5'))
        self.assertEqual(binding(self.l.conn,p['position_id'])['tp_stage'],3)
        result=self.tick(p,390,NOW+4)
        self.assertEqual(result[0]['reason'],'trailing_stop')
        self.assertEqual(self.l._positions(),[])
        fills=self.l.conn.execute('SELECT cash_delta,pnl FROM fills').fetchall()
        self.assertLess(abs(D(self.l._get('cash'))-1000-sum((D(f['cash_delta']) for f in fills),D(0))),D('1e-20'))

    def test_cost_aware_protection_arms_and_survives_restart(self):
        self.activate();p=self.buy()
        self.assertEqual(self.tick(p,45,NOW+1),[])
        self.e=ExitEngine(self.l,self.policy)
        out=self.tick(p,0,NOW+2)
        self.assertEqual(out[0]['reason'],'break_even_protection')
        self.assertLess(out[0]['pnl'],0) # Costs and discrete price gaps are not a guaranteed zero loss.

    def test_unarmed_position_and_hard_stop_precedence(self):
        self.activate();p=self.buy()
        self.assertEqual(self.tick(p,0,NOW+1),[])
        self.tick(p,45,NOW+2)
        self.assertEqual(self.tick(p,-50,NOW+3)[0]['reason'],'stop_loss')

    def test_existing_position_retains_original_exits_and_financial_rows(self):
        p=self.buy()
        before=[tuple(x) for x in self.l.conn.execute('SELECT * FROM positions')]
        self.activate()
        self.assertEqual(before,[tuple(x) for x in self.l.conn.execute('SELECT * FROM positions')])
        self.tick(p,45,NOW+1)
        self.assertEqual(self.tick(p,0,NOW+2),[])
        out=self.tick(p,110,NOW+3)
        self.assertEqual(out[0]['reason'],'take_profit')
        self.assertEqual(out[0]['quantity'],p['qty0']/2)

    def test_existing_partial_does_not_repeat_take_profit(self):
        p=self.buy();self.tick(p,110,NOW+1)
        self.activate(now=NOW+1)
        self.assertEqual(self.tick(p,610,NOW+2),[])
        self.assertEqual(self.tick(p,390,NOW+3)[0]['reason'],'trailing_stop')

    def test_duplicate_quote_cannot_advance_a_second_profit_level(self):
        self.activate();p=self.buy()
        self.tick(p,650,NOW+1)
        self.assertEqual(self.tick(p,650,NOW+1),[])
        q=quote(now=NOW+1,price=str(p['entry_price']*D('7.5')))
        self.assertEqual(self.e.watch({q.asset_id:q},NOW+2),[])
        self.assertEqual(binding(self.l.conn,p['position_id'])['tp_stage'],1)

    def test_missing_stale_wrong_pool_and_undersized_quotes_never_arm_or_sell(self):
        self.activate();p=self.buy()
        self.e.watch({},NOW+1)
        for t,kw in [(NOW+2,{'pool_id':'other'}),(NOW+3,{'route_side':'sell','route_max_sell_quantity':p['qty']/2,
                                                        'route_max_notional_usd':D(100),'route_impact_bps':D(5)})]:
            self.assertEqual(self.tick(p,650,t,**kw)[0]['verdict'],'UNRESOLVED')
        q=quote(now=NOW+1,price=str(p['entry_price']*7))
        self.assertEqual(self.e.watch({q.asset_id:q},NOW+40)[0]['verdict'],'UNRESOLVED')
        self.assertIsNone(binding(self.l.conn,p['position_id'])['armed_at'])
        self.assertEqual(self.l._positions()[0]['qty'],p['qty'])

    def test_failed_sale_does_not_advance_stage(self):
        self.activate();p=self.buy()
        self.assertEqual(self.tick(p,110,NOW+1,gas_usd=D(100))[0]['verdict'],'UNRESOLVED')
        self.assertEqual(binding(self.l.conn,p['position_id'])['tp_stage'],0)

    def test_fill_and_stage_rollback_together(self):
        self.activate();p=self.buy()
        with patch.object(self.l,'record_event',side_effect=RuntimeError('injected journal failure')):
            with self.assertRaises(RuntimeError):self.tick(p,110,NOW+1)
        self.assertEqual(binding(self.l.conn,p['position_id'])['tp_stage'],0)
        self.assertEqual(self.l.conn.execute('SELECT count(*) FROM fills').fetchone()[0],1)

    def test_exits_use_bound_policy_when_active_entry_policy_is_missing(self):
        self.activate();p=self.buy();self.l.conn.execute('DELETE FROM metadata WHERE key=?',(KEY,))
        self.assertEqual(self.tick(p,110,NOW+1)[0]['reason'],'take_profit_1')

    def test_time_exit_remains_and_revisions_are_idempotent(self):
        first=self.activate();self.assertEqual(self.activate(),first)
        p=self.buy()
        self.assertEqual(self.tick(p,0,NOW+21600)[0]['reason'],'time_stop')
        self.assertEqual(self.l.conn.execute("SELECT count(*) FROM events WHERE kind='exit_policy_revision'").fetchone()[0],1)
        self.assertEqual(self.l._get('policy_version'),self.policy.version)
        self.assertEqual(first['ends_at'],self.manifest['ends_at'])

    def test_wrong_identity_and_policy_tampering_fail(self):
        with self.assertRaises(ValueError):self.activate(expected_account='other')
        self.activate();p=self.buy()
        self.l.conn.execute("UPDATE radeon_exit_positions SET revision='bad'")
        with self.assertRaises(ValueError):self.tick(p,110,NOW+1)

    def test_reactivation_checks_existing_evidence_and_position_bindings(self):
        self.activate();p=self.buy()
        self.l.conn.execute('DELETE FROM radeon_exit_positions WHERE position_id=?',(p['position_id'],))
        with self.assertRaisesRegex(ValueError,'binding'):self.activate()

    def test_reactivation_cannot_publish_a_corrupted_manifest(self):
        self.activate()
        manifest=json.loads(self.l._get('trial_manifest'));manifest['exit_revision']='bad'
        self.l._set('trial_manifest',json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'inconsistent'):self.activate()

    def test_active_policy_drift_cannot_rewrite_bound_exits(self):
        self.activate();p=self.buy()
        active=json.loads(self.l._get(KEY));active['policy']['stop_loss_pct']='99'
        self.l._set(KEY,json_text(active))
        self.assertEqual(self.tick(p,-40,NOW+1)[0]['reason'],'stop_loss')

    def test_deleted_binding_cannot_silently_restore_legacy_exits(self):
        self.activate();p=self.buy()
        self.l.conn.execute('DROP TABLE radeon_exit_positions')
        with self.assertRaises(ValueError):self.tick(p,110,NOW+1)

    def test_ladder_validation(self):
        for change in [dict(stop_loss_pct='NaN'),dict(trailing_stop_pct='101'),dict(protect_gain_pct='-1'),
                       dict(take_profits=[{'gain_pct':'100','fraction':'.8'},{'gain_pct':'100','fraction':'.4'}])]:
            with self.assertRaises(ValueError):validate({**deepcopy(RUNNER),**change})
        self.assertEqual(len(revision(RUNNER)),64)
