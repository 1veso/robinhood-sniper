[CmdletBinding()]
param([string]$TaskName = 'SNIPER Telegram Monitor')
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'monitor.py'
$legacyLauncher = Join-Path $PSScriptRoot 'run_monitor.ps1'
$pythonPath = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$pythonWindowless = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
    throw 'The standard Windows Python pythonw.exe interpreter is required for hidden scheduling.'
}
$runtimePath = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
Set-Content -LiteralPath (Join-Path $runtimePath 'python-path.txt') -Value $pythonPath
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not (($existing.Actions.Arguments -like "*$scriptPath*") -or ($existing.Actions.Arguments -like "*$legacyLauncher*"))) {
    throw 'Task name belongs to another installation; choose another TaskName.'
}
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument "`"$scriptPath`" --send" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Priority 4 -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Two-minute read-only Robinhood launch checks and conditional Telegram research alerts; no orders.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
