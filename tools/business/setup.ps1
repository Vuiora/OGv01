param([switch]$Documents)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Push-Location -LiteralPath $taskRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 environment creation failed' }
    }
    $taskExtras = if ($Documents) { '.[test,documents]' } else { '.[test]' }
    & '.\.venv\Scripts\python.exe' -m pip install -e $taskExtras
    if ($LASTEXITCODE -ne 0) { throw 'Business dependency installation failed' }
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
    }
    Write-Output 'Business environment ready. Configure OG_LLM_API_KEY and OG_API_TOKEN in the local .env.'
} finally { Pop-Location }

