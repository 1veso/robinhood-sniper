"""Atomic, opt-in comparison inputs in the existing Solana observation journal.

No provider calls or portfolio decisions occur on this capture path. A comparison
consumer reads the full recorded inputs rather than reconstructing omitted costs
from the console's compact market events.
"""
import json
from pathlib import Path
import sqlite3
import time
import uuid


def original_start(result, finished, decided):
    value = result.get('started_at') if isinstance(result, dict) else None
    return value if type(value) is int and type(finished) is int and 0 <= value <= finished <= decided else None


def configuration(book):
    path = Path(book.root).parent / 'runtime/crew/settings.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid comparison settings')
    if value.get('enabled') is False:
        return None
    # The same engine serves Neon and isolated portfolios. Their identities
    # intentionally do not participate in this Xenon comparison.
    if value.get('source_account_id') != book.identity['account_id']:
        return None
    if (value.get('schema') != 'sniper_crew_v1' or value.get('mode') != 'COMPARISON_ONLY'
            or value.get('enabled') is not True
            or value.get('source_config_sha256') != book.identity['config_sha256']):
        raise ValueError('Comparison settings binding invalid')
    uuid.UUID(value['comparison_id'])
    return value


class CachedEvidence:
    """Read existing producer records without initialization or a writer lock."""
    def __init__(self, root):
        path = Path(root) / 'runtime/crew/evidence.sqlite3'
        self.db = None
        if path.is_file():
            self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
            try:
                self.db.row_factory = sqlite3.Row
                self.db.execute('PRAGMA query_only=ON')
                self.db.execute('BEGIN')
                self.source_id = self.db.execute("SELECT value FROM metadata WHERE key='source_id'").fetchone()[0]
            except BaseException:
                self.db.close()
                self.db = None
                raise

    def close(self):
        if self.db is not None: self.db.close()

    def available(self, *args, **kwargs):
        if self.db is None: return []
        from .crew_evidence import EvidenceStore
        return EvidenceStore.available(self, *args, **kwargs)

    def records_for(self, dossier):
        if self.db is None:
            if any(k.startswith('rug_desk:crew:') for k in dossier.get('source_refs', {})):
                raise ValueError('Referenced producer journal is unavailable')
            return []
        from .crew_evidence import EvidenceStore
        return EvidenceStore.records_for(self, dossier)


def candidate_packet(book, result, original_at):
    """Local review at capture time; never manufacture an earlier decision."""
    from .jev_dossier import build
    from .jev_review import detail
    from .crew_evidence import enrich
    from .crew_review import review
    from .crew_comparison import make_baseline_packet, paid_authority
    from rug_desk.book import encode
    root = Path(book.root).parent
    identifier = (result.get('jev') or {}).get('assessment_id') or (result.get('judgment') or {}).get('jev_assessment_id')
    cached = CachedEvidence(root)
    try:
        assessment = None
        if identifier:
            assessment = detail(root, identifier)
            if assessment['account_id'] != book.identity['account_id']:
                raise ValueError('Assessment belongs to another source account')
            dossier, baseline = assessment['dossier'], assessment['baseline']
            quote = result.get('refreshed_pool') or result.get('pool') or {}
            if (dossier['mint'] != result.get('mint') or dossier['pool'] != quote.get('pool')
                    or quote.get('mint') != dossier['mint']):
                raise ValueError('Assessment and final source candidate identities differ')
            # The dossier actually presented to Jev is immutable. Later producer
            # records cannot repair it or make a past decision look prospective.
            records = cached.records_for(dossier)
        else:
            snapshot_at = int(time.time())
            dossier, baseline = build('rug_desk', book.identity, {'mint': result['mint'], 'at': snapshot_at,
                'payload': encode(result), 'stage': result.get('stage'), 'reason': result.get('reason', '')})
            dossier = enrich(dossier, cached, now=snapshot_at)
            baseline.update(eligible=False, verdict='SKIP', prospective=False,
                            reason='Research-only capture: no original mandatory model admission')
            records = cached.records_for(dossier)
        decision_at = int(time.time())
        judgment = result.get('judgment')
        baseline_packet = make_baseline_packet(book.root, assessment, judgment, decision_at) if assessment else None
        authority = baseline_packet['paid_authority'] if baseline_packet else paid_authority(book.root, decision_at)
        packet = review(dossier, baseline, now=decision_at, producer_records=records, model_evidence=judgment)
        ambiguous = result.get('stage') == 'judge' and not judgment and authority.get('grok_enabled') is True
        return decision_at, {'result': result, 'baseline_packet': baseline_packet, 'crew_packet': packet,
            'producer_records': records, 'dossier': dossier, 'baseline': baseline,
            'paid_authority': authority, 'ambiguous_model_attempt': ambiguous,
            'original_check_at': original_start(result, original_at, decision_at), 'source_finished_at': original_at, 'decision_at': decision_at,
            'scope': 'Review created at the actual capture time from previously available local evidence; no retrospective decision or provider call.'}
    finally:
        cached.close()


def record(book, key, kind, at, payload):
    """Caller owns the existing transaction; capture and source state commit together.

    This never initializes a comparison. Invalid optional settings produce a
    durable gap rather than interfere with an existing position's exit.
    """
    from .paper_events import emit
    try:
        enabled = configuration(book)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        emit(book, 'crew-gap:' + key, 'crew_capture_gap', at,
             {'reason': 'comparison_settings_unreadable', 'source_key': key})
        return
    if enabled is None:
        return
    if kind not in ('candidate', 'market'):
        raise ValueError('Unsupported crew input kind')
    if kind == 'candidate':
        try:
            at, payload = candidate_packet(book, payload, at)
        except Exception as exc:
            original_at = at
            at = int(time.time())
            emit(book, 'crew-gap:' + key, 'crew_capture_gap', at,
                 {'reason': 'candidate_review_capture_failed', 'error_type': type(exc).__name__, 'source_key': key})
            payload = {'result': payload, 'baseline_packet': None, 'crew_packet': None, 'producer_records': [],
                       'original_check_at': original_start(payload, original_at, at),
                       'source_finished_at': original_at, 'decision_at': at,
                       'capture_error': type(exc).__name__, 'scope': 'Incomplete local capture; no comparison admission authority.'}
    emit(book, 'crew:' + key, 'crew_' + kind + '_input', at,
         {'schema': 'crew_source_input_v1', 'source_account_id': book.identity['account_id'],
          'source_config_sha256': book.identity['config_sha256'],
          'comparison_id': enabled['comparison_id'], 'input': payload})
