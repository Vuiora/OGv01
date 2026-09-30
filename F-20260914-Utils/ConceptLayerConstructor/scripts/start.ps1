$ErrorActionPreference = 'Stop'
$clcRoot = Split-Path -Parent $PSScriptRoot
$clcPython = Join-Path $clcRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $clcPython)) { throw 'Create .venv and install the project first. See README.md.' }
if (-not (Test-Path -LiteralPath (Join-Path $clcRoot '.env'))) { & $clcPython (Join-Path $PSScriptRoot 'init_env.py') }
$clcRunDir = Join-Path $clcRoot 'data\runtime'
New-Item -ItemType Directory -Force -Path $clcRunDir | Out-Null
$clcPidFile = Join-Path $clcRunDir 'processes.json'
if (Test-Path -LiteralPath $clcPidFile) {
    $clcSaved = Get-Content -LiteralPath $clcPidFile -Raw | ConvertFrom-Json
    foreach ($clcRecord in $clcSaved) {
        $clcRunning = Get-Process -Id $clcRecord.id -ErrorAction SilentlyContinue
        if ($clcRunning -and $clcRunning.StartTime.ToUniversalTime().ToString('o') -eq $clcRecord.started) {
            throw 'CLC is already running locally. Open http://127.0.0.1:8000/docs . To switch to Docker, run scripts/stop.ps1 first.'
        }
    }
}
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8000 is already in use. Local and Docker CLC cannot bind the same port. Stop the existing mode before starting this one.'
}
$clcSpecs = @(
    @{name='api'; args=@('-m','uvicorn','clc.api:create_app','--factory','--host','127.0.0.1','--port','8000')},
    @{name='worker'; args=@('-m','clc.worker')}
)
$clcRecords = @()
foreach ($clcSpec in $clcSpecs) {
    $clcProcess = Start-Process -FilePath $clcPython -ArgumentList $clcSpec.args -WorkingDirectory $clcRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $clcRunDir ($clcSpec.name+'.out.log')) -RedirectStandardError (Join-Path $clcRunDir ($clcSpec.name+'.err.log'))
    $clcRecords += @{name=$clcSpec.name; id=$clcProcess.Id; started=$clcProcess.StartTime.ToUniversalTime().ToString('o')}
}
$clcRecords | ConvertTo-Json | Set-Content -LiteralPath $clcPidFile -Encoding utf8
Write-Output 'CLC launched. Workbench: http://127.0.0.1:8000/ . API documentation: http://127.0.0.1:8000/docs . Logs: data/runtime .'
