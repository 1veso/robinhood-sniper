from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sniper.ledger import Ledger
from sniper.market_alerts import record_market_signal, notify_market
from sniper.models import WalletConfirmation
from sniper.telegram import DeliveryError
from test_engine import candidate, quote, NOW


class MarketAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.ledger = Ledger(self.folder / 'ledger.sqlite3', D(1000))
        with self.ledger.transaction():
            self.ledger._set('engine_mode', 'exploratory_paper_v1')
        (self.folder / 'telegram-bot-token.txt').write_text('fixture')
        (self.folder / 'telegram-recipient.json').write_text(json.dumps({'bot_id': 1, 'chat_id': 2}))
        self.messages = []
        messages = self.messages
        class Client:
            def __init__(self, token): pass
            def call(self, method): return {'id': 1}
            def send(self, chat_id, message):
                messages.append(message)
                return 42
        self.Client = Client

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def observation(self, event='one', pool='pool-one', now=NOW):
        asset = 'eip155:4663/erc20:0x' + 'ab'*20
        c = candidate(event_id=event, asset=asset, now=now)
        confirmations = [WalletConfirmation('wallet-a', 'unreviewed-a', 'tx-a', now, D(10), True, False),
                         WalletConfirmation('wallet-b', 'unreviewed-b', 'tx-b', now, D(10), True, False)]
        c = replace(c, confirmations=confirmations, metadata={'profile': 'two-wallet-30k', 'proof_event': event+':proof'},
                    safety=replace(c.safety, checks={'rug_screen_available': True, 'liquidity_controls': None}))
        self.ledger.record_event(event+':proof', 'exploratory_wallet_buy', {
            'pool': pool, 'token_address': asset.split(':')[-1], 'wallet': 'wallet-a', 'tx_hash': 'tx-a'}, now)
        q = quote(asset=asset, pool_id=pool, now=now)
        return c, q

    def record(self, c, q, now=NOW):
        return record_market_signal(self.ledger, c, q, {'verdict': 'SKIP', 'reason': 'max_positions'}, now)

    def notify(self, now=NOW, client=None):
        return notify_market(self.ledger, self.folder, self.folder, now=now, client_factory=client or self.Client)

    def test_real_observation_not_a_fill_and_restart_deduplicates(self):
        c,q = self.observation()
        before = self.ledger.snapshot()
        self.assertTrue(self.record(c,q))
        self.assertEqual(self.notify()['sent'], 1)
        self.assertEqual(self.notify()['sent'], 0)
        self.assertEqual(self.ledger.snapshot(), before)
        self.assertEqual(self.ledger.conn.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
        self.assertIn('MARKET WATCH', self.messages[0])
        self.assertIn('liquidity_controls', self.messages[0])
        self.assertIn('not a buy instruction', self.messages[0])

    def test_missing_coverage_bad_safety_quorum_liquidity_and_pool_mismatch(self):
        c,q = self.observation()
        cases = [(replace(c, confirmations=c.confirmations[:1]),q),
                 (replace(c, confirmations=[c.confirmations[0],replace(c.confirmations[1],tx_hash='tx-a')]),q),
                 (replace(c, safety=replace(c.safety, checks={})),q),
                 (replace(c, safety=replace(c.safety, checks={'rug_screen_available':True,'sellability':False})),q),
                 (c,replace(q,liquidity_usd=D(29999))), (c,replace(q,pool_id='wrong-pool')),
                 (c,replace(q,observed_at=NOW-901)), (c,None)]
        for bad,price in cases:
            with self.subTest(candidate=bad,quote=price):
                self.assertFalse(self.record(bad,price))
        self.assertEqual(self.notify()['sent'],0)

    def test_expired_and_reorg_halted_signals_are_suppressed(self):
        c,q=self.observation(); self.record(c,q)
        class Offline(self.Client):
            def send(self,*args): raise DeliveryError('offline')
        self.notify(client=Offline)
        self.assertEqual(self.notify(now=NOW+901)['outbox']['suppressed'],1)
        c,q=self.observation('two','pool-two',NOW+902); self.record(c,q,NOW+902)
        with self.ledger.transaction(): self.ledger._set('entry_halt','source_reorganization_requires_review')
        self.assertEqual(self.notify(now=NOW+902)['sent'],0)
        self.assertEqual(self.messages,[])

    def test_pool_cooldown_hourly_limit_and_trade_journal_independence(self):
        for i in range(4):
            c,q=self.observation(str(i),'pool-'+str(i)); self.record(c,q)
        for i in range(3): self.assertEqual(self.notify()['sent'],1)
        self.assertTrue(self.notify()['rate_limited'])
        self.assertEqual(len(self.messages),3)
        c,q=self.observation('repeat','pool-0'); self.record(c,q)
        self.notify()
        db=sqlite3.connect(self.folder/'market-notifications.sqlite3')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],4)
        db.close()
        self.assertFalse((self.folder/'notifications.sqlite3').exists())

    def test_lost_journal_and_wrong_bot_fail_closed(self):
        c,q=self.observation(); self.record(c,q)
        class Wrong(self.Client):
            def call(self,*args): return {'id':3}
        with self.assertRaisesRegex(ValueError,'paired bot'): self.notify(client=Wrong)
        (self.folder/'market-notifications.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError,'journal missing'): self.notify()

    def test_acceptance_accounts_never_send_strategy_market_alerts(self):
        with self.ledger.transaction(): self.ledger._set('engine_mode','execution_validation_v1')
        c,q=self.observation()
        self.assertFalse(self.record(c,q))
        self.assertEqual(self.notify()['sent'],0)
