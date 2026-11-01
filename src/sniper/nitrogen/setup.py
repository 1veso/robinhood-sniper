"""Explicit initial watchlist configuration without replacing an account or its history."""
from copy import deepcopy
import json
from .config import validate, digest, encode
from rug_desk.book import atomic


def configure_wallets(book, wallets, reference, source_reference):
    if any(not isinstance(v,str) or not v.strip() for v in (reference,source_reference)):
        raise ValueError('Operator authorization and watchlist source references required')
    proposed=deepcopy(book.cfg);proposed['tracked_wallets']=wallets
    validate(proposed)
    if not wallets:raise ValueError('At least one explicit tracked wallet required')
    if proposed==book.cfg:return {'changed':False,'account_id':book.identity}
    if book.cfg['tracked_wallets']:
        raise ValueError('An existing watchlist requires a separately reviewed migration')
    path=book.root/'nitrogen_bot/config.json'
    before=path.read_bytes()
    if digest(json.loads(before))!=digest(book.cfg):raise ValueError('Existing policy file differs')
    wrote=False
    try:
        with book.transaction():
            for table in ('leads','purchases','assessments','orders','positions','fills','api_requests'):
                if book.db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
                    raise ValueError('Initial watchlist configuration requires no candidate, model or trading activity')
            event={'at':int(book.clock()),'account_id':book.identity,'scope':'INITIAL_TRACKED_WALLETS_ONLY',
                   'previous_config_hash':digest(book.cfg),'config_hash':digest(proposed),
                   'wallets':wallets,'authorization_reference':reference,'source_reference':source_reference,
                   'performance_qualified':False,'real_money_enabled':False}
            book.set('initial_watchlist_configuration',event)
            book.set('config',proposed);book.set('config_hash',digest(proposed))
            atomic(path,encode(proposed));wrote=True
        book.cfg=proposed
        book.publish()
        return dict(event,changed=True)
    except BaseException:
        # If interrupted between file and DB commit, policy mismatch blocks entries.
        # Ordinary errors roll back both representations; no ledger is reset.
        if wrote and book.get('config_hash')!=digest(proposed):atomic(path,before.decode('utf-8-sig'))
        raise
