param([Parameter(Mandatory=$true)][string]$AppRoot, [Parameter(Mandatory=$true)][string]$Python, [switch]$Remove, [switch]$CheckOnly, [switch]$NeonOnly, [switch]$JevOnly)
$ErrorActionPreference = 'Stop'
$app = (Resolve-Path -LiteralPath $AppRoot).Path
$pythonExe = (Resolve-Path -LiteralPath $Python).Path
$windowless = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (Test-Path -LiteralPath $windowless) { $pythonExe = $windowless }
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$names = @{
    paper='SNIPER Exploratory Paper'; research='SNIPER Paper Research'; monitor='SNIPER Telegram Monitor';
    shadow='SNIPER Automatic Executor Shadow'; watch='RUG DESK Watch'; scan='RUG DESK Scan';
    report='RUG DESK Report'; console='RUG DESK Console'; 'neon-signals'='NEON Paper Signals'; 'neon-watch'='NEON Paper Watch'; 'jev-review'='Pump Jev Shadow Review'
}
$minutes = @{paper=2;research=1;monitor=2;shadow=1;watch=10;scan=30;report=1;console=5;'neon-signals'=1;'neon-watch'=10;'jev-review'=1}
$limits = @{paper=5;research=5;monitor=3;shadow=3;watch=9;scan=21;report=5;console=0;'neon-signals'=3;'neon-watch'=9;'jev-review'=2}
if ($NeonOnly) { $names = @{'neon-signals'='NEON Paper Signals';'neon-watch'='NEON Paper Watch'} }
if ($JevOnly) { $names = @{'jev-review'='Pump Jev Shadow Review'} }
# COM avoids repeated CIM/module startup and keeps validation and mutation in one session.
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\')
$existing = @{}
foreach ($name in $names.Values) {
    try { $old = $folder.GetTask($name) } catch {
        if ($_.Exception.HResult -notin @(-2147024894,-2147352567)) { throw }
        $old = $null
    }
    if ($old) {
        $working = [IO.Path]::GetFullPath($old.Definition.Actions.Item(1).WorkingDirectory).TrimEnd('\')
        if ($working -ne $app -and $working -ne (Join-Path $app 'rug_desk')) { throw 'Another installation owns these task names' }
        $existing[$name] = $old
    }
}
if ($CheckOnly) { Write-Output 'Task ownership check passed.'; exit 0 }
foreach ($key in $names.Keys) {
    $name = $names[$key]
    $old = $existing[$name]
    if ($Remove) {
        if ($old) { $old.Stop(0); $folder.DeleteTask($name,0) }
        continue
    }
    if (-not $old) {
        $definition = $service.NewTask(0)
        $definition.RegistrationInfo.Description = 'RUG DESK desktop PAPER worker. No live authorization. Sign-in required.'
        $definition.Principal.UserId = $owner
        $definition.Principal.LogonType = 3
        $definition.Principal.RunLevel = 0
        $definition.Settings.MultipleInstances = 2
        $definition.Settings.StartWhenAvailable = $true
        $definition.Settings.DisallowStartIfOnBatteries = $false
        $definition.Settings.StopIfGoingOnBatteries = $false
        # Default BelowNormal can starve interactive reads and shadow imports
        # under load. Existing jobs are left untouched by this installer.
        if ($key -in @('console','shadow','research','jev-review')) { $definition.Settings.Priority = 4 }
        if ($key.StartsWith('neon-')) {
            $definition.Settings.RestartCount = 3
            $definition.Settings.RestartInterval = 'PT1M'
        }
        $definition.Settings.ExecutionTimeLimit = ('PT' + $limits[$key] + 'M')
        $login = $definition.Triggers.Create(9)
        $login.UserId = $owner
        $repeat = $definition.Triggers.Create(1)
        $repeat.StartBoundary = (Get-Date).AddMinutes(1).ToString('s')
        $repeat.Repetition.Interval = ('PT' + $minutes[$key] + 'M')
        $action = $definition.Actions.Create(0)
        $action.Path = $pythonExe
        $action.Arguments = '"' + (Join-Path $app 'desktop.py') + '" worker ' + $key
        $action.WorkingDirectory = $app
        # TASK_CREATE, never overwrite a task that appeared after preflight.
        $old = $folder.RegisterTaskDefinition($name,$definition,2,$owner,$null,3)
    }
    if ($key -in @('console','paper','watch','scan','shadow','neon-signals','neon-watch','jev-review')) { $old.Run($null) | Out-Null }
}
Write-Output 'Task ownership checked; operation completed.'
