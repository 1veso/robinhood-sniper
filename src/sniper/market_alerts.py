"""Observed-market notifications. These never authorize or create a trade.

Called under the exploratory worker lock. A separate outbox keeps observation
traffic from delaying fill notices. All messages refer to dated observations.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import time

from .telegram import AlertStore, TelegramClient

MAX_AGE = 900
POOL_COOLDOWN = 6 * 3600
HOURLY_LIMIT = 3


def record_market_signal(ledger, candidate, quote, result, now):
    """Retain a two-recipient observation backed by the worker's saved proof."""
    if (ledger._get('engine_mode') != 'exploratory_paper_v1'
            or candidate.metadata.get('profile') != 'two-wallet-30k'
            or ledger._get('entry_halt') or quote is None
            or quote.asset_id != candidate.asset_id or not quote.executable
            or quote.liquidity_usd < Decimal(30000)
            or not 0 <= now - quote.observed_at <= MAX_AGE
            or not 0 <= now - candidate.safety.observed_at <= MAX_AGE
            or candidate.safety.checks.get('rug_screen_available') is not True
            or any(v is False for v in candidate.safety.checks.values())):
        return False
    confirmations = [c for c in candidate.confirmations if c.verified_swap
                     and c.spent_usd >= Decimal(10) and 0 <= now - c.observed_at <= MAX_AGE]
    if (len({c.wallet.lower() for c in confirmations}) < 2
            or len({c.tx_hash.lower() for c in confirmations}) < 2):
        return False
    row = ledger.conn.execute("SELECT payload FROM events WHERE event_id=? AND kind='exploratory_wallet_buy'",
                              (candidate.metadata.get('proof_event'),)).fetchone()
    if row is None:
        return False
    proof = json.loads(row['payload'])
    if (proof.get('pool') != quote.pool_id
            or candidate.asset_id != 'eip155:4663/erc20:' + proof.get('token_address', '')
            or not any(c.wallet == proof.get('wallet') and c.tx_hash == proof.get('tx_hash') for c in confirmations)):
        return False
    unknown = ', '.join(k for k, v in candidate.safety.checks.items() if v is None) or 'none among reported fields'
    moment = datetime.fromtimestamp(quote.observed_at, timezone.utc).isoformat()
    message = (f"SNIPER | MARKET WATCH — Robinhood Chain\n\n"
        f"Token: {candidate.symbol}\nAsset: {candidate.asset_id}\nPool: {quote.pool_id}\n"
        f"Observed price: ${quote.price_usd}\nObserved liquidity: ${quote.liquidity_usd:,.2f}\n"
        f"Buying recipients: {len({c.wallet.lower() for c in confirmations})} in distinct transactions within 15 minutes\n"
        f"GoPlus response received; no known failed checks at observation. Unknown checks: {unknown}.\n"
        f"Paper decision: {result['verdict']} ({result.get('reason', 'entry')})\nObserved: {moment}\n\n"
        "Observation only, not a buy instruction or safety guarantee. Wallet profitability and controller independence are unverified. "
        "Bounded market sample; prices and routes can change. Any paper fill is reported separately.")
    return ledger.record_event('market:' + candidate.event_id, 'market_signal', {
        'message': message, 'account_id': ledger._get('account_id'), 'pool_id': quote.pool_id,
        'asset_id': candidate.asset_id, 'observed_at': quote.observed_at}, now)


def notify_market(ledger, folder, runtime, *, now=None, client_factory=TelegramClient):
    now = int(time.time()) if now is None else now
    token_file, recipient_file = runtime / 'telegram-bot-token.txt', runtime / 'telegram-recipient.json'
    if not token_file.exists() or not recipient_file.exists():
        return {'configured': False, 'sent': 0}
    if ledger._get('engine_mode') != 'exploratory_paper_v1':
        return {'configured': False, 'sent': 0, 'reason': 'not_strategy_account'}
    path = folder / 'market-notifications.sqlite3'
    expected = ledger._get('market_notification_journal')
    if expected and not path.is_file():
        raise ValueError('Market notification journal missing; restore to prevent duplicate alerts')
    outbox = AlertStore(path)
    try:
        if expected and expected != outbox.identity:
            raise ValueError('Market notification journal identity changed')
        with ledger.transaction():
            ledger._set('market_notification_journal', outbox.identity)
        account = ledger._get('account_id')
        rows = ledger.conn.execute("SELECT * FROM events WHERE kind IN ('market_signal','market_status') AND observed_at>=? ORDER BY observed_at,event_id",
                                   (now - MAX_AGE,)).fetchall()
        for row in rows:
            payload = json.loads(row['payload'])
            if payload.get('account_id') != account:
                continue
            key = account + ':' + row['event_id']
            if outbox.db.execute('SELECT 1 FROM outbox WHERE token=?', (key,)).fetchone():
                continue
            pool = payload.get('pool_id')
            if pool and any(json.loads(r['payload']).get('pool_id') == pool for r in outbox.db.execute(
                    "SELECT payload FROM outbox WHERE created_at>? AND state!='suppressed'", (now - POOL_COOLDOWN,))):
                continue
            outbox.enqueue(key, {**payload, 'event_id': row['event_id'], 'kind': row['kind']}, row['observed_at'])

        def valid(payload):
            if payload.get('account_id') != account or not 0 <= now - payload.get('observed_at', 0) <= MAX_AGE:
                return False
            if not ledger.conn.execute('SELECT 1 FROM events WHERE event_id=? AND kind=?',
                                       (payload.get('event_id'), payload.get('kind'))).fetchone():
                return False
            if payload.get('kind') == 'market_signal':
                token = payload['asset_id'].split(':')[-1]
                if ledger._get('entry_halt') or ledger._get('safety_rejection:' + token):
                    return False
            return True

        # Expire even rate-limited notices, keeping a durable suppression record.
        for row in outbox.db.execute("SELECT token,payload FROM outbox WHERE state='pending'").fetchall():
            if not valid(json.loads(row['payload'])):
                with outbox.db:
                    outbox.db.execute("UPDATE outbox SET state='suppressed',last_error='Stale or invalidated market observation' WHERE token=?", (row['token'],))
        recent = outbox.db.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('sent','uncertain') AND next_attempt>?",
                                   (now - 3600,)).fetchone()[0]
        if not outbox.counts().get('pending') or recent >= HOURLY_LIMIT:
            return {'configured': True, 'sent': 0, 'outbox': outbox.counts(), 'rate_limited': recent >= HOURLY_LIMIT}
        recipient = json.loads(recipient_file.read_text(encoding='utf-8'))
        client = client_factory(token_file.read_text(encoding='utf-8-sig').strip())
        me = client.call('getMe')
        if not isinstance(me, dict) or me.get('id') != recipient.get('bot_id'):
            raise ValueError('Telegram bot differs from paired bot')
        outbox.bind(recipient['bot_id'], recipient['chat_id'])
        # next_attempt doubles as the attempt timestamp for this independent
        # queue; AlertStore overwrites it with the retry deadline on failure.
        with outbox.db:
            outbox.db.execute("UPDATE outbox SET next_attempt=? WHERE state='pending' AND next_attempt<=?", (now, now))
        sent = outbox.deliver(client, recipient['chat_id'], valid, now=now)
        return {'configured': True, 'sent': sent, 'outbox': outbox.counts()}
    finally:
        outbox.close()
