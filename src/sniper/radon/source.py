"""Authorized export boundary; no session cookies, scraping, or assumed Fomo feed."""
import json
from pathlib import Path

from sniper.adapters.solana import validate_address
from .config import number, digest


class SourceUnavailable(ValueError):
    pass


def timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError('UTC epoch seconds required')
    return value


def validate_bundle(value, *, demo=False):
    try:
        return _validate_bundle(value, demo=demo)
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Malformed normalized export fields; no partial import') from None


def _validate_bundle(value, *, demo=False):
    if not isinstance(value, dict) or value.get('version') != 'radon_export_v1':
        raise ValueError('Unsupported Radon export schema')
    label = value.get('evidence_label')
    if label not in ('synthetic', 'authorized_export') or (label == 'synthetic' and not demo):
        raise ValueError('Synthetic evidence requires an isolated demo account')
    if value.get('authorized') is not True or not isinstance(value.get('authorization_reference'), str) or not value['authorization_reference'].strip():
        raise SourceUnavailable('An operator-authorized feed/export and authorization reference are required')
    if not isinstance(value.get('source_id'), str) or not 1 <= len(value['source_id']) <= 100:
        raise ValueError('Source identity required')
    if value.get('complete') is not True or not isinstance(value.get('coverage'), str) or not value['coverage'].strip():
        raise SourceUnavailable('Incomplete history or unspecified source coverage')
    start, through = timestamp(value['opening_at']), timestamp(value['through'])
    if through < start:
        raise ValueError('Invalid source time range')
    openings = value['openings']
    if not isinstance(openings, dict) or not openings:
        raise ValueError('Verified opening inventories required')
    for leader, opening in openings.items():
        validate_address(leader)
        if opening.get('verified') is not True or not isinstance(opening.get('evidence'), str) or not opening['evidence'].strip():
            raise SourceUnavailable('Opening inventory is unknown; sell fractions cannot be inferred')
        if type(opening.get('sequence')) is not int or opening['sequence'] < 0 or not isinstance(opening.get('balances'), dict):
            raise ValueError('Invalid opening inventory')
        for mint, amount in opening['balances'].items():
            validate_address(mint)
            number(amount)
    if not isinstance(value.get('events'), list):
        raise ValueError('Event list required')
    ids, sequences = {}, {}
    for row in value['events']:
        if not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 200:
            raise ValueError('Stable event ID required')
        if row['leader'] not in openings:
            raise ValueError('Event lacks verified leader opening inventory')
        validate_address(row['mint'])
        if type(row['sequence']) is not int or row['sequence'] <= openings[row['leader']]['sequence']:
            raise ValueError('Invalid sequence')
        if not start <= timestamp(row['at']) <= through:
            raise ValueError('Event is outside export coverage')
        if row['side'] not in ('buy', 'sell', 'unsupported'):
            raise ValueError('Unknown activity must be explicitly unsupported')
        number(row['quantity'], positive=True)
        if not isinstance(row.get('route'), str):
            raise ValueError('Explicit route coverage required')
        fingerprint = digest(row)
        seq = (row['leader'], row['sequence'])
        if (row['id'] in ids and ids[row['id']] != fingerprint) or (seq in sequences and sequences[seq] != row['id']):
            raise ValueError('Conflicting duplicate source events')
        ids[row['id']], sequences[seq] = fingerprint, row['id']
    return value


def read(path, *, demo=False):
    path = Path(path)
    if not path.is_file():
        raise SourceUnavailable('Source unavailable: install an authorized normalized export; no Fomo cookie adapter is connected')
    if path.stat().st_size > 50_000_000:
        raise ValueError('Export exceeds bounded input size')
    return validate_bundle(json.loads(path.read_text(encoding='utf-8-sig')), demo=demo)
