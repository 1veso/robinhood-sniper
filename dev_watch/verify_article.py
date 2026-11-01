"""Verify original article sources plus the exact user-approved live-mode fixes."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def verify():
    manifest = json.loads((ROOT / 'article-manifest.json').read_text(encoding='utf-8'))
    approved_fixes = json.loads((ROOT / 'approved-live-exit-fix.json').read_text(encoding='utf-8'))
    if set(approved_fixes['files']) != {'book.py', 'watch.py'}:
        raise RuntimeError('Unexpected approved exit-fix file list')
    assert hashlib.sha256((ROOT / 'ARTICLE.txt').read_bytes()).hexdigest() == manifest['article_sha256'], 'Article changed'
    for name, expected in manifest['files'].items():
        data = (ROOT / name).read_bytes()
        baseline = data
        if name == 'config.py':
            approved = b'PAPER = False                 # fake fills at real prices. flip after a week of reading logs'
            original = b'PAPER = True                 # fake fills at real prices. flip after a week of reading logs'
            if data.count(approved) != 1:
                raise RuntimeError('Expected the user-approved PAPER=False setting')
            baseline = data.replace(approved, original, 1)
        expected_hash = approved_fixes['files'].get(name, {}).get('sha256', expected['sha256'])
        if hashlib.sha256(baseline).hexdigest() != expected_hash:
            raise RuntimeError(f'{name} differs from the extracted article')
        ast.parse(data, filename=name)
    print(f"PASS: {len(manifest['files'])} sources verified; only approved PAPER=False and live exit confirmation fixes differ; syntax valid")


if __name__ == '__main__':
    verify()
