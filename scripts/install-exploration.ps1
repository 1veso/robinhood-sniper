param(
    [string]$AppRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Pythonw = 'C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe'
)
$ErrorActionPreference = 'Stop'
$AppRoot = (Resolve-Path -LiteralPath $AppRoot).Path
if (!(Test-Path -LiteralPath (Join-Path $AppRoot 'runtime\exploration\trial.json'))) {
    throw 'Initialize the separate trial with python explore.py --start first.'
}
if (!(Test-Path -LiteralPath $Pythonw)) { throw 'Python 3.11+ pythonw.exe is required.' }
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"' + (Join-Path $AppRoot 'explore.py') + '"') -WorkingDirectory $AppRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings.Priority = 4
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'SNIPER Exploratory Paper' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Separate seven-day exploratory live-data PAPER account. No signing or real-money execution.' -Force | Out-Null
Start-ScheduledTask -TaskName 'SNIPER Exploratory Paper'
Get-ScheduledTask -TaskName 'SNIPER Exploratory Paper' | Select-Object TaskName,State
