"""Manual/replayed Solana leader events into the existing paper entry pipeline."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import Book, encode, process_lock
from rug_desk.check import check
from rug_desk.grok import Judge
from rug_desk.traders import Selections, POLICY, handle, reference, timestamp, strict_json
from sniper.adapters.solana import validate_address


class LeaderGate(ValueError):
    pass


def event_identity(event):
    try:
        if not isinstance(event, dict) or event.get('chain') != 'solana':
            raise ValueError('Only Solana event identities are supported')
        signature = event['transaction_signature']
        if not isinstance(signature, str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,88}', signature):
            raise ValueError('Required transaction signature missing or malformed')
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        decoded = 0
        for char in signature:
            decoded = decoded*58+alphabet.index(char)
        if (decoded.bit_length()+7)//8 + len(signature)-len(signature.lstrip('1')) != 64:
            raise ValueError('Signature must encode 64 bytes')
        fields = ['solana', validate_address(event['wallet']), validate_address(event['mint']), signature, event['action']]
        return hashlib.sha256(encode(fields).encode()).hexdigest()
    except (ValueError, KeyError, TypeError):
        return 'invalid:' + hashlib.sha256(encode(event).encode()).hexdigest()


def validate_event(event, now, replay, event_verifier=None):
    if not isinstance(event, dict) or event.get('chain') != 'solana':
        raise LeaderGate('non_solana_or_malformed_event')
    if event.get('source') == 'solana_finalized_cp_swap':
        if event_verifier is None or event_verifier(event, now) is not True:
            raise LeaderGate('automatic_event_proof_missing_or_invalid')
        validate_address(event['pool'])
    elif event.get('source') not in ('manual_alert', 'replay') or (event['source'] == 'replay' and not replay):
        raise LeaderGate('replay_requires_isolated_account')
    reference(event['source_ref']); handle(event['handle'])
    validate_address(event['mint']); validate_address(event['wallet'])
    if event_identity(event).startswith('invalid:') or event.get('action') not in ('BUY', 'SELL'):
        raise LeaderGate('unsupported_event_or_missing_transaction_identity')
    occurred, observed = timestamp(event['occurred_at']), timestamp(event['observed_at'])
    if not occurred <= observed <= now or now-occurred > POLICY['event_max_age_seconds']:
        raise LeaderGate('stale_or_future_event')


def record_decision(store, identity, result, now, *, finish=True):
    # Caller owns the book transaction, so an entry observation and fill commit together.
    store.db.execute('INSERT INTO leader_decisions(event_id,at,verdict,stage,reason,payload) VALUES (?,?,?,?,?,?)',
        (identity, now, result['verdict'], result.get('stage', 'leader'), result['reason'], encode(result)))
    if finish:
        store.db.execute('UPDATE leader_events SET status=?,result=? WHERE id=?',
            ('PAPER_FILLED' if result.get('entry') else 'SKIP' if result['verdict']=='SKIP' else 'NO_FILL', encode(result), identity))


def process_event(book, event, *, clock=time.time, market=None, rugcheck=None, judge=None,
                  event_verifier=None, admission_guard=None):
    store = Selections(book); now = int(clock())
    identity = event_identity(event)
    digest = hashlib.sha256(encode(event).encode()).hexdigest()
    with book.transaction():
        prior = store.db.execute('SELECT * FROM leader_events WHERE id=?', (identity,)).fetchone()
        if prior:
            result = {'event_id': identity, 'verdict': 'SKIP', 'stage': 'deduplication', 'entry': False,
                'reason': 'duplicate_event' if prior['digest']==digest else 'duplicate_identity_conflicting_payload'}
            record_decision(store, identity, result, now, finish=False)
        else:
            store.db.execute('INSERT INTO leader_events VALUES (?,?,?,?,?,NULL)',
                (identity, digest, now, encode(event), 'CLAIMED'))
            result = None
    if result:
        store.export(now)
        return result

    expected_selection = None
    def gate():
        moment = int(clock())
        validate_event(event, moment, store.replay_account(), event_verifier)
        if admission_guard:
            admission_guard(moment)
        selection = store.status(moment)
        if not selection['entry_enabled']:
            raise LeaderGate('selection_'+selection['status'].lower()+('_empty' if selection.get('empty_selection') else ''))
        if expected_selection and selection['selection_id'] != expected_selection:
            raise LeaderGate('selection_changed_during_check')
        name = handle(event['handle'])
        leader = next((l for l in selection['leaders'] if l['handle']==name and l['wallet']==event['wallet']), None)
        if leader is None:
            raise LeaderGate('unapproved_leader_or_wallet')
        return selection, leader

    try:
        selection, leader = gate()
        expected_selection = selection['selection_id']
        if event['action'] == 'SELL':
            raise LeaderGate('mirror_exit_disabled_existing_watcher_remains_authoritative')
        context = {'event_id': identity, 'selection_id': expected_selection, 'handle': leader['handle'],
            'wallet': leader['wallet'], 'source': event['source'], 'source_ref': event['source_ref'],
            'transaction_signature': event['transaction_signature'], 'metrics': leader['metrics'],
            'evidence_scope': leader['evidence_scope'], 'behavioral_pattern_evidence': leader['behavioral_pattern_evidence']}
        model = judge or Judge(book.root)
        class LeaderJudge:
            def ask(self, mint, facts, quote):
                gate()
                return model.ask(mint, facts, quote, context=context)
        def committed(position):
            record_decision(store, identity, {'event_id': identity, 'stage': 'entry', 'verdict': 'BUY',
                'entry': True, 'position_id': position['id'], 'selection_id': expected_selection,
                'reason': 'Existing paper pipeline recorded a simulated fill; not a CopyFomo or on-chain execution'}, int(clock()))
        result = check(book, event['mint'], market=market, rugcheck=rugcheck, judge=LeaderJudge(),
                       clock=clock, entry_guard=gate, entry_hook=committed, signal_context=context,
                       expected_pool=event.get('pool') if event.get('source') == 'solana_finalized_cp_swap' else None)
        result['event_id'] = identity
        if not result.get('entry'):
            try:
                gate()
            except LeaderGate as exc:
                result.update(pipeline_verdict=result['verdict'], verdict='SKIP', stage='leader_recheck', reason=str(exc))
    except Exception as exc:
        result = {'event_id': identity, 'stage': 'leader', 'verdict': 'SKIP', 'entry': False,
                  'reason': str(exc) if isinstance(exc, LeaderGate) else 'Invalid event: '+type(exc).__name__}
    committed_row = store.db.execute('SELECT status,result FROM leader_events WHERE id=?', (identity,)).fetchone()
    if committed_row['status'] == 'PAPER_FILLED' and not result.get('entry'):
        result = {**json.loads(committed_row['result']),
                  'reporting_error': 'Post-commit reporting failed; the recorded paper fill is retained'}
    if not result.get('entry'):
        with book.transaction():
            record_decision(store, identity, result, int(clock()))
    store.export(int(clock()))
    return result


def load_records(path):
    path = Path(path)
    if path.stat().st_size > 10_000_000:
        raise ValueError('Input exceeds ten megabytes')
    return strict_json(path.read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    select = sub.add_parser('select'); select.add_argument('--records', type=Path, required=True)
    ingest = sub.add_parser('ingest'); ingest.add_argument('--events', type=Path, required=True)
    sub.add_parser('status')
    args = parser.parse_args()
    with process_lock(args.root/'runtime/leader-command.lock'):
        book = Book(args.root)
        try:
            with book.run('leaders_'+args.command) as run:
                store = Selections(book)
                if args.command=='select':
                    result = store.select(lambda: load_records(args.records))
                elif args.command=='ingest':
                    events = load_records(args.events)
                    if not isinstance(events, list) or len(events)>100:
                        raise ValueError('Expected at most 100 leader events')
                    result = [process_event(book, event) for event in events]
                else:
                    result = store.status(int(time.time()))
                    store.export(int(time.time()))
                run['result'] = result
            print(encode(result))
            return 2 if args.command=='select' and result['status']=='FAILED' else 0
        finally:
            book.close()


if __name__=='__main__':
    raise SystemExit(main())
