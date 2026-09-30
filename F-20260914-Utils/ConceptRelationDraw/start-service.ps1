param([switch]$NoBrowser, [ValidateSet('Auto','Docker','Native')][string]$Deployment='Auto')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\scripts\init-env.ps1"
if ($Deployment -eq 'Native' -or ($Deployment -eq 'Auto' -and -not (Get-Command docker -ErrorAction SilentlyContinue))) {
    & "$PSScriptRoot\scripts\start-native.ps1"
    if ($LASTEXITCODE -ne 0) { throw 'Native startup failed. See data/logs/native-startup.log.' }
    Write-Output 'Background services are starting. UI: http://localhost:8092  n8n: http://localhost:5679'
    if (-not $NoBrowser) { Start-Process 'http://localhost:8092' }
    exit 0
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker Desktop with Compose is required for -Deployment Docker.' }
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed; inspect the output above.' }
Write-Output 'Services started. UI: http://localhost:8091  n8n: http://localhost:5679'
Write-Output 'First run: import n8n/workflow.json, configure Header Auth and publish the workflow. See README.md.'
if (-not $NoBrowser) { Start-Process 'http://localhost:8091' }
