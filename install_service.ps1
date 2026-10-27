[CmdletBinding()]
param([string]$TaskName = 'SNIPER Paper Research')
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'run_service.ps1'
$pythonPath = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$runtimePath = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
Set-Content -LiteralPath (Join-Path $runtimePath 'python-path.txt') -Value $pythonPath
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not ($existing.Actions.Arguments -like "*$scriptPath*")) {
    throw "Task name belongs to another installation; choose another TaskName."
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$scriptPath`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Read-only Robinhood Chain observation and PAPER simulation; no signing or live orders.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
