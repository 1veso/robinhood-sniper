"""Isolated artifact verification. No production accounts or network providers."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent


def main():
    build = json.loads((ROOT / 'dist/windows-build.json').read_text())
    target = ROOT / 'runtime/desktop-build' / ("acceptance space's " + uuid.uuid4().hex)
    flags = subprocess.CREATE_NO_WINDOW
    def run(args, expected=0, timeout=90):
        r = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout, creationflags=flags)
        if r.returncode != expected:
            raise AssertionError((r.returncode, r.stdout[-2000:], r.stderr[-2000:]))
        return r.stdout
    run([build['exe'], '--extract-only', target])
    assert not list(target.rglob('*.sqlite3'))
    assert not (target / 'app/runtime').exists()
    run([build['exe'], '--extract-only', target], expected=2)
    py, app = target / 'python/python.exe', target / 'app'
    first = json.loads(run([py, app / 'desktop.py', 'initialize']))
    second = json.loads(run([py, app / 'desktop.py', 'status']))
    assert first['solana_account'] == second['solana_account']
    assert first['solana_account']['mode'] == 'paper' and not first['paid_ai_enabled']
    neon_before=json.loads((app/'neon_bot/runtime/account.json').read_text())
    run([py,app/'neon_bot.py','watch','--no-send'])
    neon_state=json.loads(run([py,app/'neon_bot.py','status']))
    assert neon_state['account_id']==neon_before['account_id'] and neon_state['trial'] is None
    assert not neon_state['entry_enabled'] and neon_state['snapshot']['cash']=='1000'
    assert not first['paper_initialized']
    run([py, app / 'desktop.py', 'start-paper'], expected=2)
    assert not (app / 'runtime/exploration/trial.json').exists()
    run([py, app / 'desktop.py', 'worker', 'watch'])
    with sqlite3.connect(app / 'rug_desk/runtime/book.sqlite3') as c:
        assert c.execute('select count(*) from fills').fetchone()[0] == 0
        runs = c.execute("select count(*) from runs where job='watch' and status='ok' and finished is not null").fetchone()[0]
        assert runs == 1
    # Unique task, bound to this isolated empty account; removed after completion.
    task = 'RUG DESK Packaging Check ' + uuid.uuid4().hex
    script = target / 'check-task.ps1'
    def sq(value): return "'" + str(value).replace("'", "''") + "'"
    script.write_text(r"""
$ErrorActionPreference='Stop'
$name=TASK
$app=APP
$python=PYTHON
$workerArguments='"'+(Join-Path $app 'desktop.py')+'" worker watch'
$service=New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder=$service.GetFolder('\')
$definition=$service.NewTask(0)
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.UserId=$owner
$definition.Principal.LogonType=3
$definition.Principal.RunLevel=0
$definition.Settings.ExecutionTimeLimit='PT2M'
$action=$definition.Actions.Create(0)
$action.Path=$python
$action.Arguments=$workerArguments
$action.WorkingDirectory=$app
try {
 $t=$folder.RegisterTaskDefinition($name,$definition,2,$owner,$null,3)
 $t.Run($null) | Out-Null
 $completed=$false
 for($i=0;$i -lt 90;$i++) {
  Start-Sleep -Seconds 1
  $t=$folder.GetTask($name)
  if($t.State -ne 4 -and $t.LastRunTime.Year -ge 2026){$completed=$true;break}
 }
 if(-not $completed -or $t.LastTaskResult -ne 0){throw ('Scheduled worker failed: state='+$t.State+' code='+$t.LastTaskResult)}
 [pscustomobject]@{task=$name;last_result=$t.LastTaskResult;last_run=$t.LastRunTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress
} finally {
 try{$owned=$folder.GetTask($name)}catch{$owned=$null}
 if($owned -and $owned.Definition.Actions.Item(1).WorkingDirectory -eq $app){$owned.Stop(0);$folder.DeleteTask($name,0)}
}
""".replace('TASK', sq(task)).replace('APP', sq(app)).replace('PYTHON', sq(target / 'python/pythonw.exe')))
    scheduled = json.loads(run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script],timeout=125))
    with sqlite3.connect(app / 'rug_desk/runtime/book.sqlite3') as c:
        assert c.execute("select count(*) from runs where job='watch' and status='ok' and finished is not null").fetchone()[0] == 2
        assert c.execute('select count(*) from fills').fetchone()[0] == 0
    server = subprocess.Popen([str(py), str(app / 'console_server.py'), '--port', '8766'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        health = None
        for _ in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8766/api/health', timeout=2) as response:
                    health = json.load(response)
                break
            except OSError:
                time.sleep(.5)
        assert health and health['version'] == build['version'] and health['read_only']
        with urllib.request.urlopen('http://127.0.0.1:8766/api/state?bot=solana', timeout=20) as response:
            state = json.load(response)
        assert state['real_money_enabled'] is False
        with urllib.request.urlopen('http://127.0.0.1:8766/api/state?bot=neon',timeout=20) as response:
            neon=json.load(response)
        assert neon['account_id']==neon_before['account_id'] and neon['fill_count']==0
    finally:
        server.terminate();server.wait(timeout=15)
    result = {'artifact_sha256': build['sha256'], 'extracted_without_private_state': True,
              'overwrite_refused': True, 'persistent_identity_across_processes': True,
              'new_trial_blocked_without_telegram_ack': True, 'isolated_watch_completions': 2,
              'actual_windows_job': scheduled, 'console_health': health, 'strategy_fills_created': 0,
              'signing_calls': 0, 'broadcast_calls': 0, 'telegram_calls': 0,
              'neon_isolated_identity_watch_console_verified':True,
              'root': str(target), 'verified_at': int(time.time())}
    (ROOT / 'dist/windows-verification.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
