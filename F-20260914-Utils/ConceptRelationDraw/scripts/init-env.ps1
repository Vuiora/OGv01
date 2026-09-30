$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $target) {
    Write-Output '.env already exists; no changes made.'
    exit 0
}
$template = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example') -Raw
foreach ($name in @('APP_API_KEY', 'DOCLING_API_KEY', 'N8N_ENCRYPTION_KEY')) {
    $bytes = New-Object byte[] 36
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $random.Dispose()
    $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $template = [regex]::Replace($template, '(?m)^' + $name + '=.*$', $name + '=' + $secret)
}
[System.IO.File]::WriteAllText($target, $template, [System.Text.UTF8Encoding]::new($false))
Write-Output 'Created .env with independent service secrets. Configure GPT in the service UI.'
