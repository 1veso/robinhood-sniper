"""Per-user desktop orchestration. Paper workers only; no signing authority."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from rug_desk.book import Book, atomic, encode, process_lock

INPUTS = ('jupiter-server-api-key.txt', 'solana-trading-address.txt', 'evm-trading-address.txt',
          'observation-rpc-url.txt', 'quote-rpc-url.txt', 'solana-rpc-url.txt', 'telegram-bot-token.txt')
JOBS = {
    'paper': ('SNIPER Exploratory Paper', 2, 230, ['explore.py']),
    'research': ('SNIPER Paper Research', 1, 240, ['research.py']),
    'monitor': ('SNIPER Telegram Monitor', 2, 110, ['monitor.py', '--send']),
    'shadow': ('SNIPER Automatic Executor Shadow', 1, 100, ['executor.py', 'once', '--mode', 'shadow']),
    'watch': ('RUG DESK Watch', 10, 480, ['rug_desk/watch.py', '--send']),
    'scan': ('RUG DESK Scan', 30, 1200, ['rug_desk/scan.py', '--send']),
    # Minute trigger is a timezone-independent check, not a report every minute.
    'report': ('RUG DESK Report', 1, 240, ['rug_desk/report.py', '--send']),
    'console': ('RUG DESK Console', 5, 0, ['console_server.py']),
    'neon-signals': ('NEON Paper Signals', 1, 170, ['neon_bot.py', 'signals']),
    'neon-watch': ('NEON Paper Watch', 10, 480, ['neon_bot.py', 'watch']),
    'jev-review': ('Pump Jev Shadow Review', 1, 50, ['jev_review.py', 'once']),
    'krypton-scan': ('KRYPTON Paper Scan', 1, 50, ['krypton_bot.py', 'scan']),
    'krypton-watch': ('KRYPTON Paper Watch', 1, 50, ['krypton_bot.py', 'watch']),
    'helium-scan': ('HELIUM Paper Scan', 1, 50, ['helium_bot.py', 'scan']),
    'helium-watch': ('HELIUM Paper Watch', 1, 50, ['helium_bot.py', 'watch']),
    'radeon-sizing': ('RADEON Paper Size Comparison', 1, 110, ['radeon_sizing.py', 'once']),
}


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).exists() else default


def initialize(root=ROOT):
    """An empty Solana setup book is not permission to start its trial."""
    root = Path(root)
    runtime = root / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    desk = root / 'rug_desk'
    fresh = not (desk / 'runtime/account.json').exists()
    book = Book(desk, initialize=True)
    try:
        if fresh:
            book.enable_costs(int(time.time()))
        book.export()
    finally:
        book.close()
    paid = desk / 'runtime/paid-ai.json'
    if not paid.exists():
        atomic(paid, encode({'enabled': False, 'operator_authorized': False,
                           'reason': 'Desktop paper setup; paid inference requires separate authorization'}))
    from rug_desk.report import write_report
    book = Book(desk)
    try:
        write_report(book, int(time.time()))
    finally:
        book.close()
    from sniper.neon import initialize as initialize_neon
    initialize_neon(root)
    if (root/'krypton_bot/config.json').is_file():
        from sniper.krypton.config import load
        from sniper.krypton.paper import Paper
        krypton=Paper(root/'krypton_bot/runtime',load(root/'krypton_bot'),initialize=True)
        try:krypton.publish()
        finally:krypton.close()
    return status(root)


def status(root=ROOT):
    root = Path(root)
    runtime = root / 'runtime'
    trial = read(runtime / 'exploration/trial.json', {})
    paid = read(root / 'rug_desk/runtime/paid-ai.json', {})
    return {'mode': 'PAPER', 'real_money_enabled': False, 'live_execution_ready': False,
            'account_id': trial.get('account_id'), 'trial_ends_at': trial.get('ends_at'),
            'paper_initialized': bool(trial), 'inputs_present': {x: (runtime / x).is_file() for x in INPUTS},
            'telegram': read(runtime / 'telegram/setup-status.json', {'paired': False}),
            'solana_account': read(root / 'rug_desk/runtime/account.json', {}),
            'paid_ai_enabled': paid.get('enabled') is True,
            'solana_entry_blocker': 'Approved Grok/X gate requires separately authorized funded validation',
            'data_directory': str(runtime)}


def import_input(root, name, source):
    if name not in INPUTS:
        raise ValueError('Only listed API credentials and PUBLIC addresses can be imported')
    source = Path(source)
    if source.stat().st_size > 4096:
        raise ValueError('Input too large')
    value = source.read_text(encoding='utf-8-sig').strip()
    if not value or '\n' in value or '\r' in value:
        raise ValueError('Exactly one nonempty line is required')
    if name == 'solana-trading-address.txt':
        from sniper.adapters.solana import validate_address
        validate_address(value)
    elif name == 'evm-trading-address.txt':
        from sniper.adapters.evm import address
        address(value)
    elif name == 'telegram-bot-token.txt':
        from sniper.telegram import TelegramClient
        TelegramClient(value)
    elif name.endswith('rpc-url.txt'):
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('An HTTPS RPC endpoint without embedded user credentials is required')
    dest = Path(root) / 'runtime' / name
    if dest.exists() and dest.read_text(encoding='utf-8-sig').strip() != value:
        raise ValueError('An existing different input is preserved; review its replacement separately')
    if not dest.exists():
        atomic(dest, value + '\n')
    return {'imported': name, 'secret_displayed': False}


def report_due(now):
    local = datetime.fromtimestamp(now, ZoneInfo('Europe/Berlin'))
    return local.hour == 21 and local.minute >= 30


def child(args, timeout, root=ROOT):
    command = [sys.executable, str(Path(root) / args[0]), *args[1:]]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.run(command, cwd=root, timeout=timeout or None,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags).returncode


def worker(name, root=ROOT):
    root = Path(root)
    folder = root / 'runtime/desktop'
    folder.mkdir(parents=True, exist_ok=True)
    with process_lock(folder / (name + '.lock')):
        started = int(time.time())
        marker = folder / 'report-day.json'
        if name == 'report':
            date = datetime.fromtimestamp(started, ZoneInfo('Europe/Berlin')).date().isoformat()
            if not report_due(started) or read(marker, {}).get('day') == date:
                return 0
        if name in ('paper', 'research', 'shadow', 'monitor') and not (root / 'runtime/exploration/trial.json').exists():
            return 0
        result = {'started_at': started, 'job': name, 'finished_at': None, 'exit_code': None,
                  'pid': os.getpid(), 'phase': 'STARTING_CHILD'}
        if os.name == 'nt':
            import ctypes
            session = ctypes.c_ulong()
            if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
                result['windows_session_id'] = session.value
        atomic(folder / (name + '.json'), encode(result))
        try:
            result['exit_code'] = child(JOBS[name][3], JOBS[name][2], root)
        except subprocess.TimeoutExpired:
            result.update(exit_code=124, error='Child exceeded time budget; journal remains authoritative')
        except Exception as exc:
            result.update(exit_code=1, error=type(exc).__name__)
        result['finished_at'] = int(time.time())
        result['phase'] = 'COMPLETE' if result['exit_code'] == 0 else 'FAILED'
        result['duration_seconds'] = result['finished_at'] - started
        atomic(folder / (name + '.json'), encode(result))
        if name == 'report' and result['exit_code'] == 0:
            atomic(marker, encode({'day': date, 'completed_at': result['finished_at']}))
        return result['exit_code']


def install_jobs(root=ROOT, remove=False, check_only=False):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
               str(Path(root) / 'scripts/desktop-jobs.ps1'), '-AppRoot', str(root), '-Python', sys.executable]
    if remove:
        command.append('-Remove')
    if check_only:
        command.append('-CheckOnly')
    result = subprocess.run(command, capture_output=True, timeout=90, creationflags=flags)
    if result.returncode:
        raise RuntimeError('Task registration failed or another installation owns the task names; existing tasks preserved')


def start_paper(root=ROOT):
    root = Path(root)
    runtime = root / 'runtime'
    initialize(root)
    # Reopening an installed account never resets its trial or modifies its policy.
    if not (runtime / 'exploration/trial.json').exists():
        pairing = read(runtime / 'telegram/setup-status.json', {})
        if pairing.get('test_state') != 'sent' or type(pairing.get('test_message_id')) is not int:
            raise ValueError('Pair Telegram and receive its setup test before starting a new trial')
        install_jobs(root, check_only=True)
        from sniper.adapters.evm import RpcClient
        from monitor import rpc_url
        RpcClient(rpc_url(runtime), timeout=10, max_retries=0).verify_chain()
        from explore import activate_v2
        with process_lock(runtime / 'exploration-controller.lock'):
            activate_v2(runtime)
        # Explicit honest startup state, replaced by the first completed worker.
        atomic(runtime / 'exploration/status.json', encode({'mode': 'EXPLORATORY_PAPER', 'health': 'STARTING',
               'real_money_enabled': False, 'errors': [], 'decisions': []}))
    install_jobs(root)
    return status(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('initialize', 'status', 'start-paper', 'uninstall-jobs'):
        sub.add_parser(command)
    p = sub.add_parser('import-input'); p.add_argument('name', choices=INPUTS); p.add_argument('source', type=Path)
    p = sub.add_parser('pair'); p.add_argument('username')
    p = sub.add_parser('worker'); p.add_argument('name', choices=JOBS)
    args = parser.parse_args()
    try:
        if args.command == 'worker':
            return worker(args.name)
        if args.command == 'import-input':
            out = import_input(ROOT, args.name, args.source)
        elif args.command == 'pair':
            from telegram_setup import connect
            out = connect(ROOT / 'runtime', args.username, send_test=True)
        elif args.command == 'uninstall-jobs':
            install_jobs(remove=True); out = {'jobs_removed': True, 'all_data_preserved': True}
        else:
            out = {'initialize': initialize, 'status': status, 'start-paper': start_paper}[args.command]()
        print(encode(out))
        return 0
    except Exception as exc:
        # Validation messages are local and exclude provider URLs and secrets.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(encode({'status': 'SETUP_REQUIRED', 'reason': message, 'real_money_enabled': False}))
        return 2


if __name__ == '__main__':
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    raise SystemExit(main())
