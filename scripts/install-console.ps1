param(
    [string]$ProjectDirectory = (Split-Path -Parent $PSScriptRoot),
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$script = Join-Path $project 'console_server.py'
if (-not (Test-Path -LiteralPath $script)) { throw 'console_server.py is missing' }
if (-not (Test-Path -LiteralPath (Join-Path $project 'runtime\exploration\ledger.sqlite3'))) { throw 'Existing installed paper account required' }
$python = Join-Path $project 'rug_desk\.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Installed Python environment is missing' }
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '"' + $script + '" --data-root "' + $project + '" --port ' + $Port
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
$loginTrigger = New-ScheduledTaskTrigger -AtLogOn -User $owner
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
# Normal priority keeps interactive reads responsive during background research.
# This changes only the read-only console, never trading worker scheduling.
$settings.Priority = 4
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger @($loginTrigger,$recoveryTrigger) -Settings $settings -Principal $principal -Description 'Local read-only paper observation console. Binds only to 127.0.0.1. Does not trade, sign, send alerts or alter accounts.'
Register-ScheduledTask -TaskName 'RUG DESK Console' -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName 'RUG DESK Console'
Write-Output "Console task installed: http://127.0.0.1:$Port"
