$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/python-path.txt') -Raw
& $pythonPath.Trim() (Join-Path $PSScriptRoot 'supervise.py') --once
exit $LASTEXITCODE
