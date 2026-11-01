"""Explicit seven-day observation windows; entry evidence remains mandatory."""
import json
from rug_desk.book import atomic, encode
from rug_desk.model_gate import status


def start_observation(book, now, reference, blockers):
    if book.identity['mode'] != 'paper' or not isinstance(reference, str) or not reference.strip():
        raise ValueError('Explicit paper-trial authorization required')
    if not status(book.root, now)['enabled']:
        raise ValueError('Current funded model validation is required')
    with book.transaction():
        row = book.db.execute("SELECT value FROM metadata WHERE key='trial'").fetchone()
        if row:
            trial = json.loads(row[0])
        else:
            trial = {'account_id': book.identity['account_id'], 'started_at': now,
                'ends_at': now+7*86400, 'mode': 'paper',
                'purpose': 'User-started operational observation; entry gates remain mandatory',
                'authorization_reference': reference, 'entry_blockers_at_start': list(blockers)}
            book.db.execute("INSERT INTO metadata VALUES('trial',?)", (encode(trial),))
    atomic(book.runtime/'trial.json', encode(trial))
    return trial
