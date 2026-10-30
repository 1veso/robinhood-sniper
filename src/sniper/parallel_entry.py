"""Independent paper entry signals sharing one unchanged monetary account.

The runner owns canonical RPC checks and route acquisition. This engine rechecks
the pinned entry rules, current wallet eligibility, signal freshness and re-entry
inside the existing financial transaction. It never provisions live execution.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import time

from .adapters.evm import CHAIN_ID, address
from .confirmation_book import ConfirmationBook
from .entry_strategies import evaluate_signals, load_wallet_registry, rules_hash
from .exploration import ExploratoryEngine
from .ledger import json_text
from .models import WalletConfirmation
from .radeon_exits import BoundRadeonExits


PROFILE = 'parallel-whale-v1'
STRATEGY_IDS = ('whale_follow', 'two_wallet_v3')


def qualified_signals(signals):
    return [s for s in signals if s.get('qualified') is True]


def signal_proofs(signals):
    """Union original purchase evidence without merging pool-specific facts."""
    if not isinstance(signals, (list, tuple)):
        raise ValueError('Entry signals must be a sequence')
    out = {}
    for signal in signals:
        if not isinstance(signal, dict) or not isinstance(signal.get('source_proofs', []), (list, tuple)):
            raise ValueError('Malformed entry signal')
        for proof in signal.get('source_proofs', []):
            if not isinstance(proof, dict):
                raise ValueError('Malformed entry proof')
            key = (proof.get('tx_hash'), proof.get('log_index'), proof.get('block_hash'), proof.get('pool'))
            if key in out and json_text(out[key]) != json_text(proof):
                raise ValueError('Conflicting entry proof identity')
            out[key] = deepcopy(proof)
    return list(out.values())


def record_signals(ledger, event_id, asset_id, revision, signals, now, *,
                   execution_status='not_evaluated', execution_reason=None,
                   execution_pool_id=None, initiating_strategy=None, symbol=None):
    """Append idempotent state transitions; never overwrite earlier evidence."""
    qualified = [s['strategy_id'] for s in qualified_signals(signals)]
    for signal in signals:
        key = f'{event_id}:{revision}:{signal["strategy_id"]}'
        payload = {**deepcopy(signal), 'signal_key': key, 'event_id': event_id,
                   'asset_id': asset_id, 'symbol': symbol or asset_id.split(':')[-1][:10],
                   'entry_revision': revision, 'qualified_strategies': qualified,
                   'execution_status': execution_status, 'execution_reason': execution_reason,
                   'execution_pool_id': execution_pool_id, 'initiating_strategy': initiating_strategy}
        state = {k: payload.get(k) for k in ('qualified', 'qualification_reason',
                 'execution_status', 'execution_reason', 'execution_pool_id', 'initiating_strategy')}
        digest = hashlib.sha256(json_text({'key': key, 'state': state,
                                          'proofs': signal.get('source_proofs', [])}).encode()).hexdigest()
        ledger.record_event('entry-signal:' + digest, 'entry_strategy_signal', payload, now)


class ParallelEntryEngine(BoundRadeonExits, ExploratoryEngine):
    """The old engine family and monetary policy remain pinned, not duplicated."""
    def __init__(self, ledger, policy, *, entry_rules, runtime, store, registry_loader=load_wallet_registry, clock=time.time, read_only_source=False):
        self.entry_rules = deepcopy(entry_rules)
        self.entry_revision = rules_hash(self.entry_rules)
        self.runtime, self.registry_loader = runtime, registry_loader
        self.store, self.book, self.clock = store, ConfirmationBook(store, read_only=read_only_source), clock
        self._canonical_receipts = {}
        self._evaluated = {}
        self._admitted = {}
        self._transaction_registry = None
        self._resolution_error = None
        if (ledger._get('entry_revision') != self.entry_revision
                or ledger._get('entry_rules') != json_text(self.entry_rules)):
            raise ValueError('Independent entry rules differ from the recorded account revision')
        super().__init__(ledger, policy)

    @staticmethod
    def _proof_hash(proof):
        return hashlib.sha256(json_text(proof).encode()).hexdigest()

    def _journal_proofs(self, token, now):
        return self.book.recent_token_proofs(token, now=now,
            age=self.policy.max_confirmation_age_seconds, minimum_spend=self.policy.min_wallet_spend_usd)

    def confirm_proofs(self, proofs, rpc):
        """Obtain process-local receipts for actual RPC canonical checks.

        Candidate metadata alone cannot assert this check. Restarted workers
        must recheck the persisted purchase evidence before admitting it.
        """
        now = int(self.clock())
        tokens = {address(p['token_address']) for p in proofs}
        bound = {self._proof_hash(p) for token in tokens for p in self._journal_proofs(token, now)}
        if not proofs or any(self._proof_hash(p) not in bound for p in proofs):
            raise ValueError('Entry proof is not bound to the canonical source journal')
        for proof in proofs:
            self._canonical_receipts.pop(self._proof_hash(proof), None)
        self.book.confirm(proofs, rpc)
        checked = int(self.clock())
        self._canonical_receipts = {k: t for k, t in self._canonical_receipts.items()
                                    if 0 <= checked - t <= self.policy.max_quote_age_seconds}
        for proof in proofs:
            self._canonical_receipts[self._proof_hash(proof)] = checked
        return checked

    def _current_signals(self, candidate, now):
        if (candidate.metadata.get('entry_revision') != self.entry_revision
                or self.ledger._get('entry_revision') != self.entry_revision
                or self.ledger._get('entry_rules') != json_text(self.entry_rules)):
            return None, 'entry_revision_mismatch'
        trial = json.loads(self.ledger._get('trial_manifest'))
        if (candidate.metadata.get('source_id') != trial.get('source_id')
                or self.store.identity != trial.get('source_id')):
            return None, 'entry_source_mismatch'
        if now >= trial['ends_at']:
            return None, 'trial_entry_deadline'
        checked = candidate.metadata.get('canonical_checked_at')
        if type(checked) is not int or not 0 <= now - checked <= self.policy.max_quote_age_seconds:
            return None, 'canonical_signal_check_stale'
        try:
            token = address(candidate.asset_id.split(':')[-1])
            if candidate.asset_id != f'eip155:{CHAIN_ID}/erc20:{token}':
                return None, 'entry_asset_mismatch'
            proofs = signal_proofs(candidate.metadata.get('strategy_signals', []))
            decision_at = candidate.metadata.get('decision_at', candidate.observed_at)
            if type(decision_at) is not int or any(p['observed_at'] > decision_at for p in proofs):
                return None, 'confirmation_after_decision'
            bound = {self._proof_hash(p) for p in self._journal_proofs(token, now)}
            if not proofs:
                return None, 'entry_signal_evidence_invalid'
            for proof in proofs:
                digest = self._proof_hash(proof)
                receipt = self._canonical_receipts.get(digest)
                if (digest not in bound or receipt != checked
                        or type(receipt) is not int or not 0 <= now - receipt <= self.policy.max_quote_age_seconds):
                    return None, 'canonical_signal_check_missing'
            closed = self.ledger.conn.execute('SELECT MAX(closed_at) FROM closed_positions WHERE asset_id=?',
                                              (candidate.asset_id,)).fetchone()[0]
            registry = (self._transaction_registry if self._transaction_registry is not None
                        else self.registry_loader(self.runtime, now))
            signals = evaluate_signals(proofs, token=token, now=now,
                                      last_closed_at=closed, rules=self.entry_rules, registry=registry)
            return signals, None
        except (ValueError, TypeError, KeyError):
            return None, 'entry_signal_evidence_invalid'

    def _selected_signal(self, candidate, now):
        if self._resolution_error:
            return None, self._resolution_error
        signals, error = self._current_signals(candidate, now)
        if error:
            return None, error
        initiator = candidate.metadata.get('initiating_strategy')
        if initiator not in STRATEGY_IDS:
            return None, 'entry_initiator_missing'
        selected = next((s for s in signals if s['strategy_id'] == initiator), None)
        if selected is None or not selected.get('qualified'):
            return None, 'entry_strategy_no_longer_qualified'
        # The decision may select A or B, but cannot invent its confirmations or
        # let a second path lend eligibility to the selected path.
        expected = [WalletConfirmation.from_dict(c).as_dict() for c in selected['confirmations']]
        actual = [c.as_dict() for c in candidate.confirmations]
        sort = lambda rows: sorted(rows, key=lambda c: (c['wallet'], c['tx_hash']))
        if json_text(sort(actual)) != json_text(sort(expected)):
            return None, 'entry_confirmation_evidence_mismatch'
        self._evaluated[candidate.event_id] = signals
        return selected, None

    def consider(self, candidate, quote, now):
        self._transaction_registry, self._resolution_error = None, None
        try:
            return super().consider(candidate, quote, now)
        finally:
            self._transaction_registry, self._resolution_error = None, None

    def _entry_candidate(self, candidate, now):
        """Resolve the OR and actual initiator under the financial writer lock.

        A registry publication can change after quoting. If A ceases to qualify,
        already verified B evidence can still admit the same token and route.
        One registry snapshot is used throughout this financial transaction.
        """
        self._transaction_registry = self.registry_loader(self.runtime, now)
        signals, error = self._current_signals(candidate, now)
        if error:
            self._resolution_error = error
            return candidate
        try:
            supplied = next(s for s in candidate.metadata['strategy_signals']
                            if s['strategy_id'] == candidate.metadata['initiating_strategy'])
            sort = lambda rows: sorted(rows, key=lambda c: (c['wallet'], c['tx_hash']))
            expected = [WalletConfirmation.from_dict(c).as_dict() for c in supplied['confirmations']]
            if json_text(sort([c.as_dict() for c in candidate.confirmations])) != json_text(sort(expected)):
                raise ValueError('Supplied confirmation mismatch')
        except (KeyError, TypeError, ValueError, StopIteration):
            self._resolution_error = 'entry_confirmation_evidence_mismatch'
            return candidate
        eligible = qualified_signals(signals)
        if not eligible:
            return candidate
        selected = eligible[0]
        metadata = {**candidate.metadata, 'strategy_signals': signals,
                    'initiating_strategy': selected['strategy_id'],
                    'qualified_strategies': [s['strategy_id'] for s in eligible]}
        return replace(candidate, confirmations=[WalletConfirmation.from_dict(c) for c in selected['confirmations']],
                       metadata=metadata)

    def _confirmations(self, candidate, now, *, observed_after=None):
        selected, error = self._selected_signal(candidate, now)
        if error:
            return 0, error
        confirmations = [WalletConfirmation.from_dict(c) for c in selected['confirmations']]
        return sum(observed_after is None or c.observed_at > observed_after for c in confirmations), None

    def _confirmation_requirement(self, candidate):
        return 1 if candidate.metadata.get('initiating_strategy') == 'whale_follow' else 2

    def _entry_cooldown_seconds(self, candidate):
        return 86400 if (candidate.metadata.get('initiating_strategy') == 'whale_follow'
            and self.entry_rules['whale_follow']['reentry'] == 'cooldown_24h') else 0

    def _position_strategy(self, candidate):
        return candidate.metadata['initiating_strategy']

    def _additional_entry_check(self, candidate, quote, now):
        _, error = self._selected_signal(candidate, now)
        if not error:
            self._admitted[candidate.event_id] = deepcopy(self._evaluated[candidate.event_id])
        return {'reason': error} if error else None

    def _finish_decision(self, candidate, now, result, quote=None):
        if result['verdict'] == 'PAPER_BUY':
            # The admission snapshot was validated immediately before sizing
            # and the financial writes in this same SQLite transaction. Do not
            # replace it with a later registry publication after the fill.
            signals, error = self._admitted[candidate.event_id], None
        else:
            signals, error = self._current_signals(candidate, now)
        if error:
            signals = [{'strategy_id': sid, 'qualified': False, 'qualification_reason': error,
                        'source_proofs': [], 'source_pools': [], 'observed_wallets': 0,
                        'required_wallets': self.entry_rules[sid]['required_wallets']}
                       for sid in STRATEGY_IDS]
        self._evaluated[candidate.event_id] = signals
        result = super()._finish_decision(candidate, now, result, quote)
        record_signals(self.ledger, candidate.event_id, candidate.asset_id,
                       self.entry_revision, signals, now,
                       execution_status='opened' if result['verdict'] == 'PAPER_BUY' else 'blocked',
                       execution_reason=result['reason'], execution_pool_id=quote.pool_id if quote else None,
                       initiating_strategy=candidate.metadata.get('initiating_strategy'), symbol=candidate.symbol)
        return result

    def decision_context(self, candidate):
        return {**super().decision_context(candidate), 'entry_revision': self.entry_revision,
                'initiating_strategy': candidate.metadata.get('initiating_strategy'),
                'qualified_strategies': [s['strategy_id'] for s in qualified_signals(self._evaluated.get(candidate.event_id, []))],
                'strategy_results_are_shared_account_attribution': True}
