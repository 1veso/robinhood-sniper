param([string]$ProjectDirectory = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$python = Join-Path $project 'rug_desk\.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Installed Python environment is missing' }
if (-not (Test-Path -LiteralPath (Join-Path $project 'runtime\exploration\trial.json'))) { throw 'Existing paper observation account required' }
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + (Join-Path $project 'executor.py') + '" once --mode shadow') -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'SNIPER Automatic Executor Shadow' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Automatic executor shadow observation only. Reads candidate inputs; no keys, signing, submission, artificial fills or paid inference.' -Force | Out-Null
Start-ScheduledTask -TaskName 'SNIPER Automatic Executor Shadow'
Get-ScheduledTask -TaskName 'SNIPER Automatic Executor Shadow' | Select-Object TaskName,State
