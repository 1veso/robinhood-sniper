# Windows equivalent of article step 03. Run in your own terminal.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$envPath = Join-Path $PSScriptRoot '.env'
if (Test-Path -LiteralPath $envPath) {
    throw '.env already exists. No credentials were overwritten.'
}
$keySecret = Read-Host 'xAI API key (hidden)' -AsSecureString
$rpcSecret = Read-Host 'Solana RPC URL (hidden; Enter = public)' -AsSecureString
$key = [System.Net.NetworkCredential]::new('', $keySecret).Password
$rpc = [System.Net.NetworkCredential]::new('', $rpcSecret).Password
try {
    if ([string]::IsNullOrWhiteSpace($key)) { throw 'An xAI API key is required.' }
    if ([string]::IsNullOrWhiteSpace($rpc)) { $rpc = 'https://api.mainnet-beta.solana.com' }
    if ($key -match '[\r\n]' -or $rpc -match '[\r\n]') { throw 'Values must be single lines.' }
    $uri = [uri]$rpc
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'https') { throw 'RPC must be an HTTPS URL.' }
    # Create an empty file, restrict its ACL, and only then write the credentials.
    $stream = [System.IO.File]::Open($envPath, [System.IO.FileMode]::CreateNew)
    $stream.Dispose()
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $envPath
    $acl.SetAccessRuleProtection($true, $false)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $envPath -AclObject $acl
    [System.IO.File]::WriteAllText($envPath, "XAI_API_KEY=$key`nRPC_URL=$rpc`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host 'Saved .env with access restricted to your Windows user. No secrets printed.'
} finally {
    $key = $null
    $rpc = $null
    $keySecret.Dispose()
    $rpcSecret.Dispose()
}
