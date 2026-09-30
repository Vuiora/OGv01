$ErrorActionPreference = 'Stop'
$flowRoot = Split-Path -Parent $PSScriptRoot
$flowEnv = Join-Path $flowRoot '.env'
if (Test-Path -LiteralPath $flowEnv) { Write-Output '.env already exists; left unchanged.'; exit 0 }
$flowContent = Get-Content -LiteralPath (Join-Path $flowRoot '.env.example') -Raw
foreach ($flowKey in @('APP_API_KEY', 'DOCLING_API_KEY', 'N8N_ENCRYPTION_KEY')) {
    $flowBytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($flowBytes)
    $flowSecret = [Convert]::ToHexString($flowBytes).ToLowerInvariant()
    $flowContent = [regex]::Replace($flowContent, "(?m)^$flowKey=.*$", "$flowKey=$flowSecret")
}
Set-Content -LiteralPath $flowEnv -Value $flowContent -Encoding UTF8
Write-Output 'Created .env. Set your provider URL, API key and analysis/design model names locally.'

