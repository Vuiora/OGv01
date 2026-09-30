#requires -Version 7.4
param([switch]$Attached, [string]$NodeExecutable)
$ErrorActionPreference = 'Stop'
$flowRoot = Split-Path -Parent $PSScriptRoot
$flowPython = Join-Path $flowRoot '.venv\Scripts\python.exe'
$flowNode = if ($NodeExecutable) { $NodeExecutable } else { (Get-Command node -ErrorAction Stop).Source }
$flowNodeDirectory = Split-Path -Parent $flowNode
$flowCli = Join-Path $flowRoot '.n8n-test\node_modules\n8n\bin\n8n'
$flowStatePath = Join-Path $flowRoot 'data\local-processes.json'
$flowLogs = Join-Path $flowRoot 'data\logs'
if (!(Test-Path -LiteralPath $flowPython) -or !(Test-Path -LiteralPath $flowCli)) { throw 'Install the local Python and n8n dependencies described in README first.' }
New-Item -ItemType Directory -Path $flowLogs -Force | Out-Null
if (!$Attached) {
    # WMI owns this launcher, so services do not inherit the invoking tool's
    # process job and are not terminated when that tool/session ends.
    $flowShell = (Get-Process -Id $PID).Path
    $flowStartup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow=[uint16]0 }
    $flowCommand = '"'+$flowShell+'" -NoProfile -NonInteractive -WindowStyle Hidden -File "'+$PSCommandPath+'" -Attached -NodeExecutable "'+$flowNode+'"'
    $flowLaunch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine=$flowCommand; CurrentDirectory=$flowRoot; ProcessStartupInformation=$flowStartup
    }
    if ($flowLaunch.ReturnValue -ne 0) { throw "Independent background launch failed (WMI code $($flowLaunch.ReturnValue))." }
    Write-Output "Independent startup process: $($flowLaunch.ProcessId). Progress: data/logs/startup.log"
    exit 0
}
Start-Transcript -Path (Join-Path $flowLogs 'startup.log') -Force | Out-Null
$flowConfigJson = & $flowPython -c 'import json,sys; from dotenv import dotenv_values; print(json.dumps(dotenv_values(sys.argv[1],encoding="utf-8-sig")))' (Join-Path $flowRoot '.env')
if ($LASTEXITCODE -ne 0) { throw 'Could not read local .env' }
$flowConfig = $flowConfigJson | ConvertFrom-Json -AsHashtable
foreach ($flowKey in @('APP_API_KEY','DOCLING_API_KEY','N8N_ENCRYPTION_KEY','ANALYSIS_MODEL','DESIGN_MODEL')) {
    if (!$flowConfig[$flowKey]) { throw "Missing .env setting: $flowKey" }
}
$flowServices = @{}
if (Test-Path -LiteralPath $flowStatePath) { $flowServices = Get-Content -LiteralPath $flowStatePath -Raw | ConvertFrom-Json -AsHashtable }

function Test-FlowProcess($record) {
    if (!$record) { return $false }
    $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    return $running -and $running.StartTime.ToFileTimeUtc().ToString() -eq [string]$record.started
}
function Save-FlowServices {
    ConvertTo-Json -InputObject $flowServices -Depth 5 | Set-Content -LiteralPath $flowStatePath -Encoding utf8
}
function Start-FlowService($name,$exe,$serviceArgs,$port,$environment) {
    if (Test-FlowProcess $flowServices[$name]) { Write-Output "$name already started (PID $($flowServices[$name].pid))"; return }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already occupied by another process." }
    $process = Start-Process -FilePath $exe -ArgumentList $serviceArgs -WorkingDirectory $flowRoot -Environment $environment -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $flowLogs "$name.log") -RedirectStandardError (Join-Path $flowLogs "$name.error.log")
    $flowServices[$name] = @{ pid=$process.Id; started=$process.StartTime.ToFileTimeUtc().ToString(); port=$port }
    Save-FlowServices
    Write-Output "Started $name (PID $($process.Id), port $port)"
}
function Invoke-FlowN8nSetup($step,$setupArgs,$environment) {
    $process = Start-Process -FilePath $flowNode -ArgumentList (@(('"'+$flowCli+'"')) + $setupArgs) -WorkingDirectory $flowRoot -Environment $environment -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $flowLogs "n8n-$step.log") -RedirectStandardError (Join-Path $flowLogs "n8n-$step.error.log")
    if ($process.ExitCode -ne 0) { throw "n8n $step failed; see data/logs/n8n-$step.log" }
    $setupLog = Get-Content -LiteralPath (Join-Path $flowLogs "n8n-$step.log") -Raw
    if ($setupLog -match '(?i)an error occurred|GOT ERROR|error updating database') { throw "n8n $step failed; see data/logs/n8n-$step.log" }
    Write-Output "n8n $step complete"
}

$flowCommon = @{ NO_PROXY='localhost,127.0.0.1,architecture-api,docling,n8n'; PYTHONUTF8='1' }
Start-FlowService 'api' $flowPython @('-m','uvicorn','architecture_flow.api:app','--host','127.0.0.1','--port','8080','--workers','1') 8080 $flowCommon
$flowDocling = $flowCommon.Clone()
$flowDocling['DOCLING_SERVE_API_KEY'] = $flowConfig['DOCLING_API_KEY']
$flowDocling['DOCLING_SERVE_ENABLE_UI'] = 'false'
Start-FlowService 'docling' $flowPython @('-m','docling_serve','run','--host','127.0.0.1','--port','5001','--no-enable-ui') 5001 $flowDocling

$flowN8n = @{
    N8N_USER_FOLDER=(Join-Path $flowRoot 'data\n8n'); N8N_ENCRYPTION_KEY=$flowConfig['N8N_ENCRYPTION_KEY'];
    N8N_DIAGNOSTICS_ENABLED='false'; N8N_PERSONALIZATION_ENABLED='false'; N8N_VERSION_NOTIFICATIONS_ENABLED='false'; N8N_TEMPLATES_ENABLED='false';
    N8N_PORT='5678'; N8N_LISTEN_ADDRESS='127.0.0.1'; N8N_HOST='127.0.0.1'; N8N_SECURE_COOKIE='false';
    WEBHOOK_URL='http://127.0.0.1:5678/'; GENERIC_TIMEZONE='Asia/Shanghai'; EXECUTIONS_TIMEOUT='3600';
    NO_PROXY='localhost,127.0.0.1';
    PATH=($flowNodeDirectory + ';' + $env:PATH)
    N8N_RUNNERS_BROKER_PORT='5680'
}
if (!(Test-FlowProcess $flowServices['n8n'])) {
    $flowCredentialPath = Join-Path $flowRoot 'data\local-credentials.json'
    $flowWorkflowPath = Join-Path $flowRoot 'data\local-workflow.json'
    $flowCredential = @{ id='ArchitectureLocalAuth'; name='Architecture Flow API (local)'; type='httpHeaderAuth'; data=@{ name='X-API-Key'; value=$flowConfig['APP_API_KEY'] } }
    ConvertTo-Json -InputObject @($flowCredential) -Depth 5 | Set-Content -LiteralPath $flowCredentialPath -Encoding utf8
    $flowWorkflow = Get-Content -LiteralPath (Join-Path $flowRoot 'n8n\workflow.json') -Raw | ConvertFrom-Json
    foreach ($flowNodeConfig in $flowWorkflow.nodes) {
        if ($flowNodeConfig.parameters.url) { $flowNodeConfig.parameters.url = $flowNodeConfig.parameters.url.Replace('architecture-api:8080','127.0.0.1:8080') }
        if ($flowNodeConfig.credentials) { $flowNodeConfig.credentials.httpHeaderAuth = @{ id='ArchitectureLocalAuth'; name='Architecture Flow API (local)' } }
    }
    ConvertTo-Json -InputObject $flowWorkflow -Depth 50 | Set-Content -LiteralPath $flowWorkflowPath -Encoding utf8
    try {
        Invoke-FlowN8nSetup 'credentials' @('import:credentials',('--input="'+$flowCredentialPath+'"')) $flowN8n
        Invoke-FlowN8nSetup 'workflow' @('import:workflow',('--input="'+$flowWorkflowPath+'"')) $flowN8n
        Invoke-FlowN8nSetup 'publish' @('publish:workflow',('--id='+$flowWorkflow.id)) $flowN8n
    } finally {
        if (Test-Path -LiteralPath $flowCredentialPath) { Remove-Item -LiteralPath $flowCredentialPath }
    }
    Start-FlowService 'n8n' $flowNode @(('"'+$flowCli+'"'),'start') 5678 $flowN8n
}

$flowReady = @{}
$flowChecks = @{ api='http://127.0.0.1:8080/health'; docling='http://127.0.0.1:5001/health'; n8n='http://127.0.0.1:5678/healthz/readiness' }
$flowDeadline = (Get-Date).AddMinutes(15)
while ($flowReady.Count -lt 3 -and (Get-Date) -lt $flowDeadline) {
    foreach ($flowName in $flowChecks.Keys) {
        if ($flowReady[$flowName]) { continue }
        if (!(Test-FlowProcess $flowServices[$flowName])) { throw "$flowName exited; see data/logs/$flowName.error.log" }
        try {
            $response = Invoke-WebRequest -Uri $flowChecks[$flowName] -TimeoutSec 3 -NoProxy -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200) { $flowReady[$flowName]=$true; Write-Output "$flowName ready" }
        } catch { }
    }
    if ($flowReady.Count -lt 3) { Start-Sleep -Seconds 2 }
}
if ($flowReady.Count -lt 3) { throw 'Startup health check timed out. Services are retained; inspect data/logs.' }
Write-Output 'Architecture Flow is ready: http://127.0.0.1:8080 (n8n: http://127.0.0.1:5678)'
Stop-Transcript | Out-Null
