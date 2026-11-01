"""Repository entry point for the isolated DEV WATCH bot. No wallet execution."""
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
    if args.command == 'status':
        from sniper.devwatch_console import view
        print(json.dumps(view(ROOT), indent=2))
        return 0
    python = folder/'.venv/Scripts/python.exe' if sys.platform == 'win32' else folder/'.venv/bin/python'
    interpreter = str(python) if python.is_file() else sys.executable
    verified = subprocess.run([interpreter, 'verify_article.py'], cwd=folder)
    if verified.returncode or args.command == 'verify':
        return verified.returncode
    command = [interpreter, '-u', args.command+'.py']
    if args.command == 'watch':
        command += ['--minutes', '9', '--every', '20']
    return subprocess.run(command, cwd=folder).returncode


if __name__ == '__main__':
    raise SystemExit(main())
