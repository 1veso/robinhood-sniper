param([Parameter(Mandatory=$true)][string]$SourcePath,
      [string]$AppRoot = '')
$ErrorActionPreference = 'Stop'
if (-not $AppRoot) { $AppRoot = Split-Path -Parent $PSScriptRoot }
$rootPath = (Resolve-Path -LiteralPath $AppRoot).Path
$destination = Join-Path $rootPath 'runtime\xai-api-key.txt'
$keyText = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $SourcePath).Path).Trim()
$matchesFound = [regex]::Matches($keyText, '(?<![A-Za-z0-9_-])xai-[A-Za-z0-9_-]+')
if ($matchesFound.Count -ne 1) { throw 'Expected exactly one xAI key. Contents were not printed.' }
$keyValue = $matchesFound[0].Value
try {
    if (Test-Path -LiteralPath $destination) {
        if ([System.IO.File]::ReadAllText($destination).Trim() -ne $keyValue) {
            throw 'A different shared key already exists; preserved without overwrite.'
        }
    } else {
        New-Item -ItemType Directory -Path (Join-Path $rootPath 'runtime') -Force | Out-Null
        $stream = [System.IO.File]::Open($destination, [System.IO.FileMode]::CreateNew)
        $stream.Dispose()
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $destination
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
    Set-Acl -LiteralPath $destination -AclObject $acl
    [System.IO.File]::WriteAllText($destination, $keyValue+"`n", [System.Text.UTF8Encoding]::new($false))
    Write-Output 'Shared xAI key imported with restricted permissions. No account or paid gate changed.'
} finally {
    $keyText = $null; $keyValue = $null; $matchesFound = $null
}
