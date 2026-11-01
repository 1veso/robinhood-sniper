"""Repository entry point for Oxygen (slot 9), the isolated DEV WATCH paper desk."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status','verify','scan','watch','pulse','report'])
    args = parser.parse_args()
    folder = ROOT/'dev_watch'
    remaining = None
    trial_path = folder/'runtime/trial.json'
    if args.command == 'scan' and trial_path.exists():
        import time
        sys.path.insert(0, str(ROOT/'src'))
        from sniper.paper_window import require_open
        trial = json.loads(trial_path.read_text(encoding='utf-8-sig'))
        require_open(trial, 'oxygen:article-paper', time.time())
        remaining = trial['ends_at'] - time.time()
    if args.command == 'status':
        from sniper.devwatch_console import view
        print(json.dumps(view(ROOT), indent=2))
        return 0
    python = folder/'.venv/Scripts/python.exe' if sys.platform == 'win32' else folder/'.venv/bin/python'
    interpreter = str(python) if python.is_file() else sys.executable
    verified = subprocess.run([interpreter, '-X', 'utf8', 'verify_article.py'], cwd=folder)
    if verified.returncode or args.command == 'verify':
        return verified.returncode
    command = [interpreter, '-X', 'utf8', '-u', args.command+'.py']
    if args.command == 'watch':
        command += ['--minutes', '9', '--every', '20']
    if remaining is not None:
        remaining = trial['ends_at'] - time.time()
        if remaining <= 0:
            return 0
    try:
        return subprocess.run(command, cwd=folder, timeout=remaining).returncode
    except subprocess.TimeoutExpired:
        print('Oxygen paper entry window ended; scan stopped; exits remain active')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
