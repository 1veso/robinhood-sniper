param([string]$AppRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$AppRoot = (Resolve-Path -LiteralPath $AppRoot).Path
$python = Join-Path $AppRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $AppRoot 'paper_autopilot.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $script)) { throw 'Paper supervisor installation missing' }
$manifest = Get-Content -LiteralPath (Join-Path $AppRoot 'runtime\desktop\paper-autopilot.json') -Raw | ConvertFrom-Json
if ($manifest.state -ne 'ACTIVE' -or $manifest.mode -ne 'paper' -or $manifest.real_orders_enabled -ne $false) { throw 'Active paper enrollment required' }
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder('\')
$name = 'SNIPER Paper Autopilot'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$definition = $service.NewTask(0)
$definition.RegistrationInfo.Description = 'Persistent paper-only worker supervisor. No real orders. Per-job evidence gates and seven-day entry deadlines remain mandatory.'
$definition.Principal.UserId = $sid
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
$definition.Settings.Hidden = $true
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.RestartCount = 3
$definition.Settings.RestartInterval = 'PT1M'
$logon = $definition.Triggers.Create(9)
$logon.UserId = $sid
$logon.Delay = 'PT20S'
$repeat = $definition.Triggers.Create(1)
$repeat.StartBoundary = (Get-Date).AddSeconds(10).ToString('yyyy-MM-ddTHH:mm:ss')
$repeat.Repetition.Interval = 'PT1M'
$repeat.Repetition.StopAtDurationEnd = $false
$action = $definition.Actions.Create(0)
$action.Path = $python
$action.Arguments = '-X utf8 "' + $script + '" run'
$action.WorkingDirectory = $AppRoot
# Never adopt a task belonging to another installation.
$old = @($folder.GetTasks(0)) | Where-Object { $_.Name -eq $name }
if ($old -and $old.Definition.Actions.Item(1).WorkingDirectory -ne $AppRoot) { throw 'Autopilot task belongs to another installation' }
$boot = $definition.Triggers.Create(8)
$boot.Delay = 'PT30S'
$definition.Principal.LogonType = 2
try {
    $task = $folder.RegisterTaskDefinition($name,$definition,6,$sid,$null,2,$null)
    $logonMode = 'S4U'
} catch {
    # Standard-user Windows sessions may not grant unattended logon rights.
    $definition.Triggers.Remove(3)
    $definition.Principal.LogonType = 3
    $task = $folder.RegisterTaskDefinition($name,$definition,6,$sid,$null,3,$null)
    $logonMode = 'InteractiveOnly'
}
$backup = Join-Path $AppRoot ('runtime\desktop\previous-tasks\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
$names = @('SNIPER Exploratory Paper','SNIPER Paper Research','SNIPER Telegram Monitor','SNIPER Automatic Executor Shadow','RUG DESK Watch','RUG DESK Scan','RUG DESK Report','RUG DESK Console','NEON Paper Signals','NEON Paper Watch','Pump Jev Shadow Review','KRYPTON Paper Scan','KRYPTON Paper Watch','HELIUM Paper Scan','HELIUM Paper Watch','NITROGEN Search and Judge','NITROGEN Paper Exits','NITROGEN Outcomes and Report','RADON Chain Collector','RADON Copier Research','RADON Paper Entries','RADON Paper Exits','RADON Paper Report','RADEON Paper Size Comparison')
$owned = @($folder.GetTasks(0)) | Where-Object { $names -contains $_.Name }
foreach ($prior in $owned) {
    $working = $prior.Definition.Actions.Item(1).WorkingDirectory
    if ($working -ne $AppRoot -and $working -ne (Join-Path $AppRoot 'rug_desk')) { throw ('Different installation owns ' + $prior.Name) }
}
$disabled = @()
try {
    foreach ($prior in $owned) {
        if ($prior.Enabled) {
            $prior.Xml | Set-Content -LiteralPath (Join-Path $backup ($prior.Name + '.xml')) -Encoding Unicode
            $prior.Enabled = $false
            $disabled += $prior
        }
    }
    $null = $task.Run($null)
    @{task=$name;logon_mode=$logonMode;old_tasks_disabled=$disabled.Count;real_orders_enabled=$false} | ConvertTo-Json
} catch {
    foreach ($prior in $disabled) { $prior.Enabled = $true }
    $task.Enabled = $false
    throw
}
