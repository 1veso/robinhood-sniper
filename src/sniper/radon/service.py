"""Independent research, entry and exit jobs. No model dependency on exits."""
from datetime import datetime, timezone
import json
import time
import uuid

from rug_desk.book import atomic
from .config import digest, encode, load, policy_hash, number
from .source import read
from .policy import buy_plan, market_check, integrity_check
from .view import snapshot


def sync_source(book, now):
    try:
        bundle = read(book.folder/'source.json', demo=book.demo)
        book.ingest(bundle, now)
        return bundle
    except (ValueError, OSError, KeyError, TypeError) as exc:
        prior = book.get('source', {})
        book.set('source', dict(prior, state='UNAVAILABLE', error=str(exc)[:300], attempted_at=now))
        raise


def research(book, clock=time.time):
    from .replay import research as evaluate
    now = int(clock()); run = book.run_start('research', now); errors = []
    try:
        bundle = read(book.folder/'history.json', demo=book.demo)
        if not 0 <= now-bundle['through'] <= book.cfg['shortlist_ttl_seconds']:
            raise ValueError('Historical export watermark is stale or future-dated')
        boundary = bundle['through']-book.cfg['lookback_days']*86400
        if bundle['opening_at'] > boundary:
            raise ValueError('History does not cover configured lookback with verified opening inventory')
        # Older exports may seed opening inventory, but the strategy window is explicit.
        if bundle['opening_at'] < boundary-300:
            raise ValueError('Export must provide opening inventory at the configured lookback boundary')
        result = evaluate(bundle, book.cfg)
        stamp = int(clock()); identifier = uuid.uuid4().hex
        result.update(created_at=stamp, policy_hash=policy_hash(book.cfg))
        book.db.execute('INSERT INTO research VALUES(?,?,?)', (identifier, stamp, encode(result)))
        atomic(book.folder/'research'/f'{identifier}.json', encode({'result': result, 'input': bundle}))
        book.publish_shortlist(result['selected'], stamp, identifier)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append(str(exc)[:300])
        book.decision(None, 'RESEARCH_BLOCKED', errors[-1], int(clock()))
    finally:
        book.run_finish(run, int(clock()), errors); book.publish()
    return errors


def entries(book, providers=None, model=None, clock=time.time):
    from .providers import Providers
    from .model import Model
    providers = providers or Providers(book.root, book, clock)
    model = model or Model(book.root, book, clock)
    now = int(clock()); run = book.run_start('entries', now); errors = []
    try:
        if policy_hash(load(book.root/'radon_bot')) != policy_hash(book.cfg):
            raise ValueError('Entry config changed; explicit migration required; exits retain bound policy')
        book.set('model_gate', dict(model.gate(), observed_at=int(clock())))
        sync_source(book, int(clock()))
        # The entries process lock guarantees these belong to a prior interrupted run.
        for row in book.db.execute("SELECT id FROM events WHERE state='RESEARCHING'").fetchall():
            book.skip(row['id'], 'Interrupted research; possible billing retained, no automatic retry', int(clock()))
        for pending in book.db.execute("SELECT * FROM events WHERE state='BUY_PENDING' ORDER BY at,leader,sequence").fetchall():
            if int(clock()) < pending['at']+book.cfg['execution_delay_seconds']:
                continue
            evidence = {}
            try:
                row = book.eligibility(pending['id'], int(clock()))
                quote = providers.quote(row['mint'])
                market_check(book.cfg, quote, row['mint'], int(clock()), json.loads(row['payload'])['route'])
                episode = book.db.execute('SELECT spent FROM episodes WHERE mint=?', (row['mint'],)).fetchone()
                buy_plan(book.cfg, quote, row['mint'], int(clock()), cash=book.available_cash(), deployed=book.deployed(),
                    episode_spend=number(episode[0]) if episode else number(0), route=json.loads(row['payload'])['route'])
                evidence = providers.evidence(row['mint'], quote)
                integrity_check(book.cfg, evidence, row['mint'], int(clock()))
                gate = model.gate(); book.set('model_gate', dict(gate, observed_at=int(clock())))
                if not gate['enabled']:
                    raise ValueError('Required Radon X evidence unavailable: '+ '; '.join(gate.get('blockers', [])))
                with book.transaction():
                    book.eligibility(row['id'], int(clock()))
                    book.db.execute("UPDATE events SET state='RESEARCHING' WHERE id=?", (row['id'],))
                    book.decision(row['id'], 'PENDING_EVIDENCE', 'Waiting for authorized X evidence', int(clock()))
                evidence['x'] = model.ask(row['id'], evidence)
                if evidence['x'].get('verdict') != 'BUY':
                    raise ValueError('X research rejected the entry')
                # Re-read the feed after slow research; a leader exit invalidates this buy.
                sync_source(book, int(clock()))
                refreshed = providers.quote(row['mint'], quote['pool'])
                if not model.gate()['enabled']:
                    raise ValueError('Model authorization revoked before fill')
                if policy_hash(load(book.root/'radon_bot')) != policy_hash(book.cfg):
                    raise ValueError('Entry policy changed during research; no fill')
                book.buy(row['id'], refreshed, int(clock()), evidence)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                book.skip(pending['id'], str(exc), int(clock()), {'evidence': evidence})
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append(str(exc)[:300])
    finally:
        book.run_finish(run, int(clock()), errors); book.publish()
    return errors


def watch(book, providers=None, clock=time.time):
    from .providers import Providers
    providers = providers or Providers(book.root, book, clock)
    run = book.run_start('watch', int(clock())); errors = []
    try:
        try:
            sync_source(book, int(clock()))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(str(exc)[:300])
        # Even if source refresh failed, already-observed sells retain their authority.
        for row in book.db.execute("SELECT * FROM events WHERE state='EXIT_PENDING' ORDER BY at,leader,sequence").fetchall():
            if int(clock()) < row['at']+book.cfg['execution_delay_seconds']:
                continue
            try:
                p = book.position(row['leader'], row['mint'])
                quote = providers.quote(row['mint']) if number(p['quantity']) else {}
                book.sell(row['id'], quote, int(clock()))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append(str(exc)[:300])
                book.decision(row['id'], 'EXIT_PENDING', str(exc), int(clock()))
        for mint, in book.db.execute('SELECT DISTINCT mint FROM positions WHERE CAST(quantity AS REAL)>0').fetchall():
            try:
                book.mark(providers.quote(mint), int(clock()))
            except (ValueError, OSError, KeyError, TypeError):
                errors.append('Open position mark unavailable: '+mint)
    finally:
        book.run_finish(run, int(clock()), errors); book.publish()
    return errors


def report(book, clock=time.time):
    now = int(clock()); run = book.run_start('report', now)
    try:
        with book.transaction():
            view = snapshot(book.db, now)
        path = book.folder/'reports'/f'{now}-{uuid.uuid4().hex[:8]}.json'
        atomic(path, encode(view))
        s = view['summary']; shortlist = view['shortlist']
        lines = [f"RADON — {'SYNTHETIC' if book.demo else 'PROSPECTIVE PAPER'} — slot 8",
            f"ACCOUNT cash={s['cash']} equity={s['equity']} starting={s['starting_cash']} net_return={s['net_account_return']} [{path}]",
            f"CAPITAL turnover={s['buying_turnover']} peak_deployed={s['peak_deployed']} rent_reserved={s['rent_reserved']} [{path}]",
            f"RESULT realized={s['realized_pnl']} unrealized={s['unrealized_pnl']} network_costs={s['network_costs']} model_billed={s['model_billed']} model_unsettled={s['model_unsettled']} [{path}]",
            f"SHORTLIST {encode(shortlist)} [{path}]",
            f"HEALTH source={view['status']['source']['state']} pending_exits={view['pending_exits']} [{path}]",
            f"QUESTION signal_expiry_seconds={book.cfg['signal_expiry_seconds']}: compare recorded research/fill latency against closed leader holding times before changing it. No retuning performed. [{path}]",
            f"Historical conditional replay and current paper fills are separate datasets. Missing prices are unknown, never fabricated sales. [{path}]" ]
        atomic(path.with_suffix('.txt'), '\n'.join(lines)+'\n')
        book.run_finish(run, int(clock()), []); book.publish()
        return str(path)
    except BaseException:
        book.run_finish(run, int(clock()), ['Report publication failed'])
        raise


def daily_due(book, kind, now):
    stamp = datetime.fromtimestamp(now, timezone.utc)
    hour = book.cfg[kind+'_hour_utc']
    done = book.get('schedule:'+kind, {})
    return stamp.hour >= hour and done.get('day') != stamp.date().isoformat()
