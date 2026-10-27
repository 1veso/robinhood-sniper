$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/python-path.txt') -Raw
$pythonExe = $pythonPath.Trim()
if ($pythonExe.Contains("`n") -or -not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'Invalid Python path; run install_service.ps1 again.'
}
& $pythonExe (Join-Path $PSScriptRoot 'supervise.py') --once
exit $LASTEXITCODE
