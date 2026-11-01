"""Read-only projection of Oxygen (slot 9), the isolated DEV WATCH article desk."""
import ast
from collections import Counter, deque
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time

LIMIT = 16_000_000
POLICY_FIELDS = {'PAPER', 'MODEL', 'TICKET_USD', 'MAX_OPEN', 'MAX_ENTRIES_PER_DAY',
                 'DAILY_LOSS_HALT_USD', 'COOLDOWN_HOURS', 'ENTRY', 'MIN_CONFIDENCE', 'WATCH', 'PULSE', 'EXIT'}


def read(path):
    if path.stat().st_size > LIMIT:
        raise ValueError('DEV WATCH data exceeds display limit')
    return path.read_text(encoding='utf-8-sig')


def safe(value):
    text = str(value or '')
    text = re.sub(r'https?://\S+', '[provider URL omitted]', text, flags=re.I)
    text = re.sub(r'(?i)\bBearer\s+\S+|\bxai-[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'(?i)\b(api[_-]?key|authorization|password|secret)\s*[:=]\s*\S+', r'\1=[redacted]', text)
    return text[:1000]


def amount(value):
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError('Non-finite ledger value')
    return out


def policy(folder):
    out = {}
    for node in ast.parse(read(folder/'config.py')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in POLICY_FIELDS:
                out[name] = ast.literal_eval(node.value)
    if set(out) != POLICY_FIELDS or type(out['PAPER']) is not bool:
        raise ValueError('Incomplete DEV WATCH policy')
    return out


def source_status(folder):
    try:
        manifest = json.loads(read(folder/'article-manifest.json'))
        approved = json.loads(read(folder/'approved-live-exit-fix.json'))['files']
        names = {'config.py','book.py','facts.py','grok.py','prompts.py','scan.py',
                 'enter.py','rpc.py','watch.py','pulse.py','report.py'}
        if set(manifest['files']) != names or set(approved) != {'book.py', 'watch.py'}:
            return False
        for name, item in manifest['files'].items():
            data = (folder/name).read_bytes()
            if name == 'config.py':
                if data.count(b'PAPER = True                 #') != 1:
                    return False
            if hashlib.sha256(data).hexdigest() != approved.get(name, item)['sha256']:
                return False
        return hashlib.sha256((folder/'ARTICLE.txt').read_bytes()).hexdigest() == manifest['article_sha256']
    except (OSError, ValueError, KeyError, TypeError):
        return False


def objects(folder, name):
    path = folder/name
    value = json.loads(read(path)) if path.exists() else {}
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError('Invalid DEV WATCH ledger')
    return value


def csv_rows(folder, name):
    path = folder/name
    if not path.exists():
        return
    if path.stat().st_size > LIMIT:
        raise ValueError('DEV WATCH CSV exceeds display limit')
    with path.open(encoding='utf-8-sig', newline='') as f:
        yield from csv.DictReader(f)


def view(root, now=None):
    folder = Path(root)/'dev_watch'
    result = {'bot':'oxygen', 'slot':9, 'title':'Oxygen', 'purpose':'DEV WATCH developer-wallet desk',
              'initialized':(folder/'config.py').is_file(), 'account_id':None,
              'assets':[], 'status':{}, 'execution_target':'paper', 'live_execution_ready':False}
    if not result['initialized']:
        result['status'] = {'health':'NOT_INSTALLED'}
        return result
    cfg = policy(folder)
    positions = []
    for mint, p in objects(folder, 'positions.json').items():
        pending = p.get('pending_exit') or {}
        positions.append({'mint':safe(mint), 'symbol':safe(p['symbol']),
                          'qty':str(amount(p['qty'])), 'entry':str(amount(p['entry'])),
                          'pulse':safe(p['pulse']), 'opened':p['opened'],
                          'pending_exit':{k:safe(pending.get(k)) for k in ('id','reason','qty')} if pending else None})
    proposals = [{'mint':safe(mint), 'symbol':safe(p['symbol'])}
                 for mint,p in objects(folder,'pending.json').items()]
    checks, trades, costs = deque(maxlen=100), deque(maxlen=100), deque(maxlen=100)
    rejects = Counter(); count = buys = fills = posts = 0; pnl = spend = Decimal(0)
    for row in csv_rows(folder,'checks.csv'):
        count += 1; buys += row.get('verdict') == 'BUY'
        if row.get('verdict') == 'SKIP': rejects[safe(row.get('reason'))] += 1
        checks.append({k:safe(row.get(k)) for k in ('time','mint','symbol','stage','verdict','confidence','reason')})
    for row in csv_rows(folder,'trades.csv'):
        fills += 1; pnl += amount(row['pnl'])
        trades.append({k:safe(row.get(k)) for k in ('time','mint','symbol','action','usd','pnl','reason')})
    for row in csv_rows(folder,'costs.csv'):
        spend += amount(row['usd']); posts += int(row['x_posts'])
        costs.append({k:safe(row.get(k)) for k in ('time','job','usd','x_posts')})
    observed = [p.stat().st_mtime for p in (folder/'checks.csv',folder/'trades.csv',folder/'costs.csv',folder/'positions.json') if p.exists()]
    result.update(policy=cfg, mode='PAPER' if cfg['PAPER'] else 'MANUAL LIVE', positions=positions,
                  pending_buys=proposals, checks=list(reversed(checks)), trades=list(reversed(trades)),
                  costs=list(reversed(costs)), rejection_counts=dict(rejects.most_common()),
                  summary={'checked':count,'buy_verdicts':buys,'fill_records':fills,'realized_pnl':str(pnl),
                           'api_spend':str(spend),'net_realized_after_api':str(pnl-spend),'x_posts':posts},
                  status={'health':'OBSERVING','sources_verified':source_status(folder),
                          'credential_file_present':(folder/'.env').is_file(),
                          'last_record_at':max(observed) if observed else None,
                          'schedule_host':'Codex local routines','schedule_verified_by_console':False},
                  cadence_seconds=600, generated_at=int(time.time() if now is None else now))
    return result


def health(root):
    folder = Path(root)/'dev_watch'
    return {'available':(folder/'config.py').is_file(), 'account_id':None, 'slot':9,
            'last_completed_at':None, 'execution':'paper_simulation_only'}
