"""Durable Telegram notices for actual ledgered PAPER fills, never trade signals."""
from datetime import datetime, timezone
from decimal import Decimal
import json

from .telegram import AlertStore, TelegramClient


def notify_fills(ledger, folder, runtime, *, client_factory=TelegramClient):
    token_file, recipient_file = runtime / 'telegram-bot-token.txt', runtime / 'telegram-recipient.json'
    if not token_file.exists() or not recipient_file.exists():
        return {'configured': False, 'sent': 0}
    mode = ledger._get('engine_mode')
    if mode not in ('exploratory_paper_v1', 'execution_validation_v1'):
        raise ValueError('Exploratory notifications require their own paper account')
    label = 'EXECUTION TEST — PAPER' if mode == 'execution_validation_v1' else 'EXPLORATORY PAPER'
    purpose = ('Isolated execution test with a one-second time exit. Excluded from strategy/week results. '
               if mode == 'execution_validation_v1' else '')
    path = folder / 'notifications.sqlite3'
    expected = ledger._get('notification_journal')
    if expected and not path.is_file():
        raise ValueError('Notification journal missing; restore it to prevent duplicate messages')
    outbox = AlertStore(path)
    try:
        if expected and expected != outbox.identity:
            raise ValueError('Notification journal identity changed')
        with ledger.transaction():
            ledger._set('notification_journal', outbox.identity)
        account = ledger._get('account_id')
        for row in ledger.conn.execute('SELECT * FROM fills ORDER BY id'):
            fill = dict(row)
            moment = datetime.fromtimestamp(fill['observed_at'], timezone.utc).isoformat()
            message = (f"SNIPER | {label} {fill['action']}\n\n{purpose}"
                f"Token: {fill['symbol']}\nAsset: {fill['asset_id']}\n"
                f"Notional: ${Decimal(fill['gross_usd']):.4f}\n"
                f"Quantity: {fill['quantity']}\nModeled price: ${fill['fill_price']}\n"
                f"Modeled costs: ${Decimal(fill['friction_usd']) + Decimal(fill['gas_usd']):.4f}\n"
                f"Realized P&L on this fill after modeled costs: ${Decimal(fill['pnl']):+.4f}\n"
                f"Reason: {fill['reason']}\nTime: {moment}\nPaper fill: {fill['id']}\n\n"
                "Simulated fill from live market data. No real-money order. Wallet skill, controller independence and some safety checks remain unestablished. Separate from the strict strategy.")
            outbox.enqueue(account + ':' + str(fill['id']), {'message': message, 'account_id': account,
                'order_id': fill['order_id'], 'created_at': fill['observed_at']}, fill['observed_at'])
        counts = outbox.counts()
        if not counts.get('pending'):
            return {'configured': True, 'sent': 0, 'outbox': counts}
        recipient = json.loads(recipient_file.read_text(encoding='utf-8'))
        client = client_factory(token_file.read_text(encoding='utf-8-sig').strip())
        me = client.call('getMe')
        if not isinstance(me, dict) or me.get('id') != recipient.get('bot_id'):
            raise ValueError('Telegram bot differs from the paired bot')
        outbox.bind(recipient['bot_id'], recipient['chat_id'])
        def valid(payload):
            return payload.get('account_id') == account and ledger.conn.execute('SELECT 1 FROM fills WHERE order_id=?', (payload.get('order_id'),)).fetchone() is not None
        sent = outbox.deliver(client, recipient['chat_id'], valid)
        return {'configured': True, 'sent': sent, 'outbox': outbox.counts()}
    finally:
        outbox.close()
