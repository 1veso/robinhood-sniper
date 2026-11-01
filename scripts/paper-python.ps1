param([Parameter(ValueFromRemainingArguments=$true)][string[]]$PythonArgs)
$ErrorActionPreference = 'Stop'
$paperAppRoot = Split-Path -Parent $PSScriptRoot
$paperPython = Join-Path $paperAppRoot '.venv\Scripts\python.exe'
$paperPreviousCertificates = $env:SSL_CERT_FILE
try {
    if (-not $env:SSL_CERT_FILE) {
        $env:SSL_CERT_FILE = (& $paperPython -c 'import certifi; print(certifi.where())').Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Install the bot dependencies before using this launcher.' }
    }
    Push-Location -LiteralPath $paperAppRoot
    try { & $paperPython -X utf8 @PythonArgs; $paperResult = $LASTEXITCODE }
    finally { Pop-Location }
} finally {
    $env:SSL_CERT_FILE = $paperPreviousCertificates
}
exit $paperResult
