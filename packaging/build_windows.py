"""Build an offline-installable Windows x64 EXE from allowlisted source only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PYTHON_ZIP = 'python-3.14.10-embed-amd64.zip'
PYTHON_HASH = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
DEPENDENCIES = {'certifi': '2026.10.23', 'charset_normalizer': '3.5.2', 'idna': '3.20',
                'requests': '2.34.2', 'tzdata': '2026.5', 'urllib3': '2.8.0'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files(root=ROOT):
    files = []
    candidates = []
    excluded = {'runtime', '.venv', '.git', '__pycache__', 'dist', 'build', 'tests', 'packaging', '.github', '.impeccable'}
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d not in excluded and not (Path(folder) / d).is_symlink()]
        candidates.extend(Path(folder) / n for n in names)
    for path in candidates:
        rel = path.relative_to(root)
        if any(p in ('runtime', '.venv', '.git', '__pycache__', 'dist', 'build') for p in rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if (len(rel.parts) == 1 and (path.suffix in ('.py', '.toml') or path.name=='requirements-executor.txt')) or (
                rel.parts[0] in ('src', 'rug_desk', 'console', 'config', 'docs', 'scripts')
                and path.suffix in ('.py', '.json', '.toml', '.html', '.js', '.css', '.svg', '.md', '.ps1')):
            if any(x in path.name.lower() for x in ('api-key', 'trading-address', 'recipient', 'token.txt')):
                raise ValueError('Private-looking file in source allowlist')
            files.append(path)
    return sorted(files)


def build():
    downloads = ROOT / 'runtime/desktop-build/downloads'
    archive = downloads / PYTHON_ZIP
    if digest(archive) != PYTHON_HASH:
        raise ValueError('Official Python archive hash mismatch')
    stage = ROOT / 'runtime/desktop-build' / ('stage-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    for path in source_files():
        target = stage / 'app' / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    with zipfile.ZipFile(archive) as z:
        z.extractall(stage / 'python')
    (stage / 'python/python314._pth').write_text('python314.zip\n.\nLib/site-packages\n../app\n../app/src\nimport site\n')
    deps = {}
    pins = json.loads((ROOT / 'packaging/dependencies.sha256.json').read_text())
    for name, version in DEPENDENCIES.items():
        wheels = sorted(downloads.glob(f'{name}-{version}-*none-any.whl'))
        if len(wheels) != 1:
            raise ValueError('Exactly one pure-Python pinned wheel required: ' + name)
        wheel = wheels[0]
        deps[wheel.name] = digest(wheel)
        if deps[wheel.name] != pins.get(wheel.name):
            raise ValueError('Pinned wheel checksum mismatch: ' + wheel.name)
        with zipfile.ZipFile(wheel) as z:
            z.extractall(stage / 'python/Lib/site-packages')
    csc = Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe')
    references = ['/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Core.dll', '/r:System.Web.Extensions.dll']
    subprocess.run([str(csc), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(stage / 'RugDesk.exe'),
                    *references, str(ROOT / 'packaging/Launcher.cs')], check=True)
    shutil.copy2(ROOT / 'packaging/QUICK_START.txt', stage / 'QUICK_START.txt')
    manifest = {str(p.relative_to(stage)).replace('\\', '/'): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
    (stage / 'payload.sha256').write_text(''.join(h + '  ' + name + '\n' for name, h in manifest.items()), encoding='utf-8')
    payload = stage.parent / (stage.name + '.zip')
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(stage))
    setup = stage.parent / (stage.name + '-Setup.cs')
    setup.write_text((ROOT / 'packaging/Setup.cs').read_text().replace('PAYLOAD_HASH', digest(payload)))
    output = ROOT / 'dist/RUG-DESK-Setup-0.1.22-win-x64.exe'
    output.parent.mkdir(exist_ok=True)
    subprocess.run([str(csc), '/nologo', '/target:winexe', '/platform:x64', '/out:' + str(output),
                   *references, '/r:System.IO.Compression.dll', '/r:System.IO.Compression.FileSystem.dll',
                   '/resource:' + str(payload) + ',payload.zip', str(setup)], check=True)
    result = {'version': '0.1.22', 'exe': str(output), 'sha256': digest(output), 'bytes': output.stat().st_size,
              'python_archive_sha256': PYTHON_HASH, 'python_version': '3.14.10', 'dependencies': deps,
              'stage': str(stage), 'source_files': len(source_files()), 'payload_files': len(manifest),
              'runtime_data_included': False, 'signed': False}
    (output.parent / 'windows-build.json').write_text(json.dumps(result, indent=2))
    (output.parent / (output.name + '.sha256')).write_text(result['sha256'] + '  ' + output.name + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    build()
