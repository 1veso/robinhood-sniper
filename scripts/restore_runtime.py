"""Restore a transferred runtime without replacing existing local files or running workers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def restore(source, destination):
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate trees')
    ignored_dirs = {'executor-venv', '.venv', 'venv', '__pycache__', 'node_modules', '.git'}
    files, skipped, preserved = [], [], []
    for folder, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for directory in dirs:
            candidate = Path(folder)/directory
            if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
                raise ValueError('Linked directory in transfer')
        for name in names:
            path = Path(folder)/name
            relative = path.relative_to(source)
            target = destination/relative
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or not target.resolve().is_relative_to(destination):
                raise ValueError('Unsafe linked path in transfer')
            if name.endswith(('.lock', '.tmp', '.pid')):
                skipped.append(str(relative))
                continue
            if target.exists():
                if relative.as_posix() == 'xai-api-key.txt' or digest(path) == digest(target):
                    preserved.append(str(relative))
                    continue
                raise FileExistsError(f'Destination conflict: {relative}')
            files.append((path, target, relative))
    required = sum(p.stat().st_size for p, _, _ in files)
    if shutil.disk_usage(destination.parent).free < required + 1_000_000_000:
        raise RuntimeError('Insufficient free space for runtime restore')
    print(json.dumps({'copy_files':len(files), 'bytes':required,
                      'existing_preserved':preserved, 'stale_process_files_skipped':len(skipped)}), flush=True)
    databases = []
    for index, (path, target, relative) in enumerate(files, 1):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if target.stat().st_size != path.stat().st_size:
            raise RuntimeError(f'Copy size mismatch: {relative}')
        if path.suffix in ('.sqlite3', '.sqlite', '.db'):
            databases.append((path, target, relative))
        if path.name.endswith('-wal') and digest(path) != digest(target):
            raise RuntimeError(f'WAL copy hash mismatch: {relative}')
        if index % 2000 == 0:
            print(json.dumps({'copied':index, 'total':len(files)}), flush=True)
    checked = []
    for path, target, relative in databases:
        source_hash, restored_hash = digest(path), digest(target)
        if source_hash != restored_hash:
            raise RuntimeError(f'Database copy hash mismatch: {relative}')
        conn = sqlite3.connect(target.as_uri()+'?mode=ro', uri=True, timeout=10)
        try:
            conn.execute('PRAGMA query_only=ON')
            checks = [r[0] for r in conn.execute('PRAGMA quick_check')]
        finally:
            conn.close()
        checked.append({'path':str(relative), 'sha256':source_hash, 'quick_check':checks})
        print(json.dumps({'database':str(relative),'quick_check':checks}), flush=True)
        if checks != ['ok']:
            raise RuntimeError(f'Invalid transferred database: {relative}')
    report = {'source':str(source), 'destination':str(destination), 'finished_at':int(time.time()),
              'copied_files':len(files), 'copied_bytes':required, 'preserved':preserved,
              'skipped_stale_process_files':skipped, 'databases':checked,
              'source_modified':False, 'workers_started':False}
    report_path = destination/'local-setup/runtime-restore.json'
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'status':'restored','databases_verified':len(checked),'report':str(report_path)}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    restore(args.source,args.destination)
