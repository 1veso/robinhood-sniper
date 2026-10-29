"""Observed-market notifications. These never authorize or create a trade.

Called under the exploratory worker lock. A separate outbox keeps observation
traffic from delaying fill notices. All messages refer to dated observations.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import time

from .adapters.evm import CHAIN_ID, address
from .entry_strategies import evaluate_signals, rules_hash
from .ledger import json_text
from .models import Candidate, WalletConfirmation
from .telegram import AlertStore, TelegramClient

MAX_AGE = 900
POOL_COOLDOWN = 6 * 3600
HOURLY_LIMIT = 3
PARALLEL_PROFILE = 'parallel-whale-v1'


def _parallel_observation(ledger, candidate, quote, result, now):
    """Read the committed B signal, never an outer candidate's proposed flags.

    Final admission may switch from A to B as current evidence changes. Its
    committed candidate/result are authoritative; the runner's original object
    is only an event/source/revision binding. No provider calls occur here.
    """
    try:
        if quote is None or ledger.conn.in_transaction:
            return None
        revision = ledger._get('entry_revision')
        rules = json.loads(ledger._get('entry_rules'))
        if rules_hash(rules) != revision or candidate.metadata.get('profile') != PARALLEL_PROFILE:
            return None
        row = ledger.conn.execute('SELECT * FROM decisions WHERE event_id=?', (candidate.event_id,)).fetchone()
        if row is None or row['asset_id'] != candidate.asset_id or not 0 <= now - row['observed_at'] <= MAX_AGE:
            return None
        saved = json.loads(row['payload'])
        committed = Candidate.from_dict(saved['candidate'])
        decision = saved['result']
        if (committed.event_id != candidate.event_id or committed.asset_id != candidate.asset_id
                or committed.metadata.get('profile') != PARALLEL_PROFILE
                or saved.get('observed_at') != row['observed_at']
                or decision.get('event_id') != candidate.event_id or decision.get('asset_id') != candidate.asset_id
                or decision.get('verdict') != row['verdict'] or decision.get('reason') != row['reason']
                or json_text(decision) != json_text(result) or json_text(saved.get('quote')) != json_text(quote.as_dict())
                or decision.get('entry_revision') != revision
                or 'two_wallet_v3' not in decision.get('qualified_strategies', [])):
            return None
        for field in ('source_id', 'proof_event', 'entry_revision'):
            if not committed.metadata.get(field) or committed.metadata.get(field) != candidate.metadata.get(field):
                return None
        if committed.metadata['entry_revision'] != revision:
            return None
        trial = json.loads(ledger._get('trial_manifest'))
        if committed.metadata['source_id'] != trial.get('source_id'):
            return None
        snapshots = committed.metadata.get('strategy_signals', [])
        matches = [s for s in snapshots if isinstance(s, dict) and s.get('strategy_id') == 'two_wallet_v3']
        if len(matches) != 1 or matches[0].get('qualified') is not True:
            return None
        proposed = matches[0]
        # record_signals() commits this content-addressed event alongside the
        # decision. Direct primary-key lookup avoids scanning the growing audit.
        key = f'{candidate.event_id}:{revision}:two_wallet_v3'
        state = {'qualified': True, 'qualification_reason': 'qualified',
                 'execution_status': 'opened' if decision['verdict'] == 'PAPER_BUY' else 'blocked',
                 'execution_reason': decision['reason'], 'execution_pool_id': quote.pool_id,
                 'initiating_strategy': decision.get('initiating_strategy')}
        digest = hashlib.sha256(json_text({'key': key, 'state': state,
                                           'proofs': proposed.get('source_proofs', [])}).encode()).hexdigest()
        event = ledger.conn.execute("SELECT payload,observed_at FROM events WHERE event_id=? AND kind='entry_strategy_signal'",
                                    ('entry-signal:' + digest,)).fetchone()
        if event is None or event['observed_at'] != row['observed_at']:
            return None
        authoritative = json.loads(event['payload'])
        if (authoritative.get('signal_key') != key or authoritative.get('event_id') != candidate.event_id
                or authoritative.get('asset_id') != candidate.asset_id
                or authoritative.get('entry_revision') != revision
                or any(authoritative.get(k) != v for k, v in state.items())):
            return None
        token = address(candidate.asset_id.split(':')[-1])
        if candidate.asset_id != f'eip155:{CHAIN_ID}/erc20:{token}':
            return None
        proofs = authoritative.get('source_proofs', [])
        decision_at = committed.metadata.get('decision_at', committed.observed_at)
        if type(decision_at) is not int or not committed.observed_at <= decision_at <= row['observed_at']:
            return None
        if any(type(p.get('observed_at')) is not int or p['observed_at'] > decision_at for p in proofs):
            return None
        checked = evaluate_signals(proofs, token=token, now=row['observed_at'],
            last_closed_at=authoritative.get('last_closed_at'), rules=rules, registry={})[1]
        fields = ('revision', 'rules_hash', 'strategy_id', 'chain_id', 'token_address', 'evaluated_at',
                  'qualified', 'qualification_reason', 'observed_wallets', 'required_wallets',
                  'source_pools', 'source_proofs', 'confirmations', 'wallet_qualification', 'reentry_rule')
        if not checked['qualified'] or any(json_text(authoritative.get(k)) != json_text(checked[k]) for k in fields):
            return None
        current = evaluate_signals(proofs, token=token, now=now,
            last_closed_at=authoritative.get('last_closed_at'), rules=rules, registry={})[1]
        if not current['qualified']:
            return None
        return committed, decision, current
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def record_market_signal(ledger, candidate, quote, result, now):
    """Retain a two-recipient observation backed by the worker's saved proof."""
    parallel = candidate.metadata.get('profile') == PARALLEL_PROFILE or ledger._get('entry_revision') is not None
    signal = None
    if parallel:
        observation = _parallel_observation(ledger, candidate, quote, result, now)
        if observation is None:
            return False
        candidate, result, signal = observation
    if (ledger._get('engine_mode') != 'exploratory_paper_v1'
            or candidate.metadata.get('profile') not in ('two-wallet-30k', 'two-wallet-30k-reentry', PARALLEL_PROFILE)
            or ledger._get('entry_halt') or quote is None
            or quote.asset_id != candidate.asset_id or not quote.executable
            or quote.liquidity_usd < Decimal(30000)
            or not 0 <= now - quote.observed_at <= MAX_AGE
            or not 0 <= now - candidate.safety.observed_at <= MAX_AGE
            or candidate.safety.checks.get('rug_screen_available') is not True
            or any(v is False for v in candidate.safety.checks.values())):
        return False
    source_confirmations = ([WalletConfirmation.from_dict(c) for c in signal['confirmations']]
                            if signal is not None else candidate.confirmations)
    confirmations = [c for c in source_confirmations if c.verified_swap
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
    if signal is not None and not any(json_text(proof) == json_text(p) for p in signal['source_proofs']):
        return False
    unknown = ', '.join(k for k, v in candidate.safety.checks.items() if v is None) or 'none among reported fields'
    moment = datetime.fromtimestamp(quote.observed_at, timezone.utc).isoformat()
    signal_label = 'Signal across pools' if signal is not None and len(signal['source_pools']) > 1 else 'Signal pool'
    pool_line = (f"Execution pool: {quote.pool_id}\n{signal_label}: {', '.join(signal['source_pools'])}\n"
                 if signal is not None else f"Pool: {quote.pool_id}\n")
    qualification = ("Strategy B's observed-recipient confirmations do not establish wallet profitability or controller independence. "
                     if signal is not None else "Wallet profitability and controller independence are unverified. ")
    message = (f"SNIPER | MARKET WATCH — Robinhood Chain\n\n"
        f"Token: {candidate.symbol}\nAsset: {candidate.asset_id}\n{pool_line}"
        f"Observed price: ${quote.price_usd}\nObserved liquidity: ${quote.liquidity_usd:,.2f}\n"
        f"Buying recipients: {len({c.wallet.lower() for c in confirmations})} in distinct transactions within 15 minutes\n"
        f"GoPlus response received; no known failed checks at observation. Unknown checks: {unknown}.\n"
        f"Paper decision: {result['verdict']} ({result.get('reason', 'entry')})\nObserved: {moment}\n\n"
        "Observation only, not a buy instruction or safety guarantee. " + qualification +
        "Bounded market sample; prices and routes can change. Any paper fill is reported separately.")
    return ledger.record_event('market:' + candidate.event_id, 'market_signal', {
        'message': message, 'account_id': ledger._get('account_id'), 'pool_id': quote.pool_id,
        'asset_id': candidate.asset_id, 'observed_at': quote.observed_at,
        **({'entry_revision': signal['rules_hash'], 'signal_strategy': 'two_wallet_v3',
            'signal_pool_ids': signal['source_pools']} if signal is not None else {})}, now)


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
