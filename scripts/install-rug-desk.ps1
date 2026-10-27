param([string]$AppRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$AppRoot = (Resolve-Path -LiteralPath $AppRoot).Path
$desk = Join-Path $AppRoot 'rug_desk'
$pythonw = Join-Path $desk '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $pythonw)) { throw 'Create rug_desk\.venv and install its requirements first.' }
if (!(Test-Path -LiteralPath (Join-Path $desk 'runtime\account.json'))) { throw 'Initialize the paper book first.' }
if ((Get-TimeZone).Id -ne 'W. Europe Standard Time') {
    throw 'This installer requires Windows Europe/Berlin-equivalent W. Europe Standard Time. It does not change the system timezone.'
}
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$jobs = @(
    @{ Name = 'RUG DESK Watch'; Script = 'watch.py'; Minutes = 10; Timeout = 8 },
    @{ Name = 'RUG DESK Scan'; Script = 'scan.py'; Minutes = 30; Timeout = 20 },
    @{ Name = 'RUG DESK Report'; Script = 'report.py'; Minutes = 0; Timeout = 5 }
)
foreach ($job in $jobs) {
    $scriptPath = Join-Path $desk $job.Script
    if (!(Test-Path -LiteralPath $scriptPath)) { throw ('Missing ' + $scriptPath) }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $scriptPath + '" --send') -WorkingDirectory $desk
    if ($job.Minutes -gt 0) {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $job.Minutes)
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At '21:30'
    }
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $job.Timeout) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $settings.Priority = 4
    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Solana RUG DESK paper/reading only. Independent ledger. No wallet signing or execution. Paid X requests remain opt-in.' -Force | Out-Null
}
Get-ScheduledTask -TaskName 'RUG DESK *' | Select-Object TaskName,State
