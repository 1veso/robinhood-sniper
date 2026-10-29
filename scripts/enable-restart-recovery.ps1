param(
    [Parameter(Mandatory=$true)][string]$AppRoot,
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory
)
$ErrorActionPreference='Stop'
$app=(Resolve-Path -LiteralPath $AppRoot).Path
$python=Join-Path $app 'rug_desk\.venv\Scripts\python.exe'
$checker=Join-Path $app 'restart_check.py'
$evidence=[IO.Path]::GetFullPath($EvidenceDirectory)
[IO.Directory]::CreateDirectory($evidence)|Out-Null
$result=@{started_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); changed=@(); ok=$false; reverted=@()}
$names=@('SNIPER Exploratory Paper','SNIPER Paper Research','SNIPER Telegram Monitor',
    'SNIPER Automatic Executor Shadow','RUG DESK Watch','RUG DESK Scan','RUG DESK Report',
    'RUG DESK Console','NEON Paper Signals','NEON Paper Watch','Pump Jev Shadow Review')
$before=@{}
$folder=$null
$probeName='SNIPER Recovery Probe '+[guid]::NewGuid().ToString('N')
$probeCreated=$false
try {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($identity)
    if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Windows administrator consent is required for noninteractive task registration and boot triggers.'
    }
    & $python $checker --root $app --output (Join-Path $evidence 'before.json')
    if($LASTEXITCODE -ne 0) { throw 'Paper/account/paid-call preflight failed; tasks unchanged.' }
    $baseline=Get-Content -Raw -LiteralPath (Join-Path $evidence 'before.json')|ConvertFrom-Json
    $svc=New-Object -ComObject Schedule.Service
    $svc.Connect(); $folder=$svc.GetFolder('\')
    # Validate every owner/action BEFORE mutation. Never substitute a different
    # interpreter, root, account or recurrence. Existing report timing is retained.
    foreach($name in $names) {
        $task=$folder.GetTask($name)
        $def=$task.Definition
        if($def.Actions.Count -ne 1) { throw "Unexpected actions: $name" }
        $action=$def.Actions.Item(1)
        $working=[IO.Path]::GetFullPath($action.WorkingDirectory).TrimEnd('\')
        if($working -notin @($app,(Join-Path $app 'rug_desk'))) { throw "Foreign working directory: $name" }
        if(-not $action.Arguments.Contains($app+'\')) { throw "Foreign action: $name" }
        $taskUser=$def.Principal.UserId
        # Task Scheduler may persist a local short name, qualified name or SID.
        # Compare the resolved security identity, never the display spelling.
        $taskSid=if($taskUser -match '^S-1-') {
            (New-Object Security.Principal.SecurityIdentifier($taskUser)).Value
        } else {
            (New-Object Security.Principal.NTAccount($taskUser)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
        if($taskSid -ne $identity.User.Value) { throw "Different Windows owner: $name" }
        if($def.Principal.LogonType -notin @(2,3) -or $def.Principal.RunLevel -ne 0) { throw "Unexpected security context: $name" }
        $before[$name]=$task.Xml
        [IO.File]::WriteAllText((Join-Path $evidence ($name+'.before.xml')), $task.Xml)
    }
    # S4U has no reusable Windows network credentials or encrypted-file access.
    # Prove THIS installation's file + bearer/public HTTPS access in Session 0
    # before converting any production task. Never request/store a password.
    $probe=$svc.NewTask(0)
    $probe.Principal.UserId=$identity.Name; $probe.Principal.LogonType=2; $probe.Principal.RunLevel=0
    $probe.Settings.Priority=4; $probe.Settings.ExecutionTimeLimit='PT3M'; $probe.Settings.MultipleInstances=2
    $probe.Settings.DisallowStartIfOnBatteries=$false; $probe.Settings.StopIfGoingOnBatteries=$false
    $action=$probe.Actions.Create(0); $action.Path=$python; $action.WorkingDirectory=$app
    $probeFile=Join-Path $evidence 'noninteractive-probe.json'
    if(Test-Path -LiteralPath $probeFile) { throw 'Use a new evidence directory; old probes cannot authorize changes.' }
    $action.Arguments='"'+$checker+'" --root "'+$app+'" --output "'+$probeFile+'" --network'
    $pt=$folder.RegisterTaskDefinition($probeName,$probe,2,$identity.Name,$null,2)
    $probeCreated=$true; $pt.Run($null)|Out-Null
    $deadline=(Get-Date).AddSeconds(180)
    while(-not (Test-Path -LiteralPath $probeFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if(-not (Test-Path -LiteralPath $probeFile)) { throw 'Noninteractive probe did not complete; production tasks unchanged.' }
    $verified=Get-Content -Raw -LiteralPath $probeFile|ConvertFrom-Json
    if(-not $verified.ok -or $verified.windows_session_id -ne 0 -or -not $verified.network_requested) {
        throw 'Noninteractive file/network checks failed; production tasks unchanged.'
    }
    if(($baseline.protected_sha256|ConvertTo-Json -Compress) -ne ($verified.protected_sha256|ConvertTo-Json -Compress)) {
        throw 'Protected account/settings changed during preflight; production tasks unchanged.'
    }
    foreach($name in $names) {
        $task=$folder.GetTask($name)
        if($task.Xml -ne $before[$name]) { throw "Task changed during preflight: $name" }
        $def=$task.Definition
        $def.Principal.LogonType=2
        if($name -eq 'Pump Jev Shadow Review') { $def.Settings.Priority=4 }
        # Daily report must NEVER become an unconditional boot-time message.
        if($name -ne 'RUG DESK Report') {
            $boots=@($def.Triggers | Where-Object {$_.Type -eq 8})
            if($boots.Count -eq 0) { $trigger=$def.Triggers.Create(8); $trigger.Delay='PT30S' }
        }
        $updated=$folder.RegisterTaskDefinition($name,$def,4,$identity.Name,$null,2)
        $result.changed+=@{name=$name;logon_type=$updated.Definition.Principal.LogonType;priority=$updated.Definition.Settings.Priority}
        [IO.File]::WriteAllText((Join-Path $evidence ($name+'.after.xml')), $updated.Xml)
    }
    & $python $checker --root $app --output (Join-Path $evidence 'after.json')
    if($LASTEXITCODE -ne 0) { throw 'Post-migration paper checks failed.' }
    $after=Get-Content -Raw -LiteralPath (Join-Path $evidence 'after.json')|ConvertFrom-Json
    if(($baseline.protected_sha256|ConvertTo-Json -Compress) -ne ($after.protected_sha256|ConvertTo-Json -Compress)) { throw 'Protected settings changed.' }
    $result.ok=$true
} catch {
    $result.error=$_.Exception.Message
    if($folder) {
        foreach($changed in $result.changed) {
            try {
                $xml=$before[$changed.name]
                $oldLogon=if(([xml]$xml).Task.Principals.Principal.LogonType -eq 'S4U'){2}else{3}
                $folder.RegisterTask($changed.name,$xml,4,$identity.Name,$null,$oldLogon,$null)|Out-Null
                $result.reverted+=$changed.name
            } catch { $result.rollback_failed=$true }
        }
    }
} finally {
    if($probeCreated) {
        try { $folder.GetTask($probeName).Stop(0); $folder.DeleteTask($probeName,0) } catch { $result.probe_cleanup_failed=$true }
    }
    $result.finished_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $result|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $evidence 'migration.json') -Encoding UTF8
}
if(-not $result.ok) { exit 1 }
