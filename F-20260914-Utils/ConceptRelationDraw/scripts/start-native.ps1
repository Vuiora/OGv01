#requires -Version 7.4
param([switch]$Attached, [string]$NodeExecutable, [string]$DoclingPython, [string]$N8nCli)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nodePath = if ($NodeExecutable) { $NodeExecutable } else { (Get-Command node -ErrorAction Stop).Source }
$doclingPath = if ($DoclingPython) { $DoclingPython } else { Join-Path (Split-Path -Parent $projectRoot) 'ThePicWorkingFlow\.venv\Scripts\python.exe' }
$n8nPath = if ($N8nCli) { $N8nCli } else { Join-Path (Split-Path -Parent $projectRoot) 'ThePicWorkingFlow\.n8n-test\node_modules\n8n\bin\n8n' }
foreach ($runtime in @($apiPython, $nodePath, $doclingPath, $n8nPath)) {
    if (-not (Test-Path -LiteralPath $runtime)) { throw "Missing runtime: $runtime. Supply -DoclingPython and -N8nCli, or use Docker deployment." }
}
$statePath = Join-Path $projectRoot 'data\native-processes.json'
$logs = Join-Path $projectRoot 'data\logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
if (-not $Attached) {
    $shellPath = (Get-Process -Id $PID).Path
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow=[uint16]0 }
    $commandLine = '"'+$shellPath+'" -NoProfile -NonInteractive -WindowStyle Hidden -File "'+$PSCommandPath+'" -Attached -NodeExecutable "'+$nodePath+'" -DoclingPython "'+$doclingPath+'" -N8nCli "'+$n8nPath+'"'
    $launch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine=$commandLine; CurrentDirectory=$projectRoot; ProcessStartupInformation=$startup
    }
    if ($launch.ReturnValue -ne 0) { throw "Background startup failed: $($launch.ReturnValue)" }
    Write-Output "Background startup PID: $($launch.ProcessId). Progress: data/logs/native-startup.log"
    exit 0
}
Start-Transcript -Path (Join-Path $logs 'native-startup.log') -Force | Out-Null
$configJson = & $apiPython -c 'import json,sys; from dotenv import dotenv_values; print(json.dumps(dotenv_values(sys.argv[1],encoding="utf-8-sig")))' (Join-Path $projectRoot '.env')
if ($LASTEXITCODE -ne 0) { throw 'Could not load project .env' }
$config = $configJson | ConvertFrom-Json -AsHashtable
foreach ($setting in @('APP_API_KEY', 'DOCLING_API_KEY', 'N8N_ENCRYPTION_KEY')) {
    if (-not $config[$setting]) { throw "Missing .env setting: $setting" }
}
$services = @{}
if (Test-Path -LiteralPath $statePath) { $services = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable }

function Test-NativeProcess($record) {
    if (-not $record) { return $false }
    $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    return $running -and $running.StartTime.ToFileTimeUtc().ToString() -eq [string]$record.started
}
function Save-NativeProcesses {
    ConvertTo-Json -InputObject $services -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
}
function Start-NativeProcess($name, $executable, $arguments, $port, $environment) {
    if (Test-NativeProcess $services[$name]) { Write-Output "$name already running (PID $($services[$name].pid))"; return }
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($name -ne 'api' -or $existing.CommandLine -notlike "*$projectRoot*" -or $existing.CommandLine -notlike '*concept_relation.api:create_app*') {
            throw "Port $port is occupied by an unmanaged process."
        }
        $running = Get-Process -Id $existing.ProcessId
        $services[$name] = @{ pid=$running.Id; started=$running.StartTime.ToFileTimeUtc().ToString(); port=$port }
        Save-NativeProcesses
        Write-Output "Using existing project API on port $port"
        return
    }
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $projectRoot -Environment $environment -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$name.log") -RedirectStandardError (Join-Path $logs "$name.error.log")
    $services[$name] = @{ pid=$process.Id; started=$process.StartTime.ToFileTimeUtc().ToString(); port=$port }
    Save-NativeProcesses
    Write-Output "Started $name (PID $($process.Id), port $port)"
}
function Invoke-N8nSetup($step, $arguments, $environment) {
    $process = Start-Process -FilePath $nodePath -ArgumentList (@(('"'+$n8nPath+'"')) + $arguments) -WorkingDirectory $projectRoot -Environment $environment -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $logs "n8n-$step.log") -RedirectStandardError (Join-Path $logs "n8n-$step.error.log")
    if ($process.ExitCode -ne 0) { throw "n8n $step failed; inspect data/logs/n8n-$step.error.log" }
    $output = Get-Content -LiteralPath (Join-Path $logs "n8n-$step.log") -Raw
    if ($output -match '(?i)an error occurred|GOT ERROR|error updating database') { throw "n8n $step failed; inspect data/logs/n8n-$step.log" }
    Write-Output "n8n $step complete"
}

$common = @{ NO_PROXY='localhost,127.0.0.1'; PYTHONUTF8='1' }
Start-NativeProcess 'api' $apiPython @('-m','uvicorn','concept_relation.api:create_app','--factory','--host','127.0.0.1','--port','8092','--workers','1') 8092 $common
$doclingEnvironment = $common.Clone()
$doclingEnvironment['DOCLING_SERVE_API_KEY'] = $config['DOCLING_API_KEY']
$doclingEnvironment['DOCLING_SERVE_ENABLE_UI'] = 'false'
Start-NativeProcess 'docling' $doclingPath @('-m','docling_serve','run','--host','127.0.0.1','--port','5002','--no-enable-ui') 5002 $doclingEnvironment
$n8nEnvironment = @{
    N8N_USER_FOLDER=(Join-Path $projectRoot 'data\n8n'); N8N_ENCRYPTION_KEY=$config['N8N_ENCRYPTION_KEY'];
    N8N_DIAGNOSTICS_ENABLED='false'; N8N_PERSONALIZATION_ENABLED='false'; N8N_VERSION_NOTIFICATIONS_ENABLED='false'; N8N_TEMPLATES_ENABLED='false';
    N8N_PORT='5679'; N8N_LISTEN_ADDRESS='127.0.0.1'; N8N_HOST='127.0.0.1'; N8N_SECURE_COOKIE='false';
    N8N_WEBHOOK_URL='http://127.0.0.1:5679/'; GENERIC_TIMEZONE='Asia/Shanghai'; EXECUTIONS_TIMEOUT='7200';
    NO_PROXY='localhost,127.0.0.1'; PATH=((Split-Path -Parent $nodePath)+';'+$env:PATH); N8N_RUNNERS_BROKER_PORT='5681'
}
if (-not (Test-NativeProcess $services['n8n'])) {
    $credentialPath = Join-Path $projectRoot 'data\native-credentials.json'
    $workflowPath = Join-Path $projectRoot 'data\native-workflow.json'
    $credential = @{ id='ConceptRelationNativeAuth'; name='ConceptRelationDraw API'; type='httpHeaderAuth'; data=@{ name='X-API-Key'; value=$config['APP_API_KEY'] } }
    ConvertTo-Json -InputObject @($credential) -Depth 5 | Set-Content -LiteralPath $credentialPath -Encoding utf8
    $workflow = Get-Content -LiteralPath (Join-Path $projectRoot 'n8n\workflow.json') -Raw | ConvertFrom-Json
    foreach ($workflowNode in $workflow.nodes) {
        if ($workflowNode.parameters.url) { $workflowNode.parameters.url = $workflowNode.parameters.url.Replace('concept-api:8091','127.0.0.1:8092') }
        if ($workflowNode.credentials) { $workflowNode.credentials.httpHeaderAuth = @{ id='ConceptRelationNativeAuth'; name='ConceptRelationDraw API' } }
    }
    ConvertTo-Json -InputObject $workflow -Depth 50 | Set-Content -LiteralPath $workflowPath -Encoding utf8
    try {
        Invoke-N8nSetup 'credentials' @('import:credentials',('--input="'+$credentialPath+'"')) $n8nEnvironment
        Invoke-N8nSetup 'workflow' @('import:workflow',('--input="'+$workflowPath+'"')) $n8nEnvironment
        Invoke-N8nSetup 'publish' @('publish:workflow',('--id='+$workflow.id)) $n8nEnvironment
    } finally {
        if (Test-Path -LiteralPath $credentialPath) { Remove-Item -LiteralPath $credentialPath }
    }
    Start-NativeProcess 'n8n' $nodePath @(('"'+$n8nPath+'"'),'start') 5679 $n8nEnvironment
}
$ready = @{}
$checks = @{ api='http://127.0.0.1:8092/health'; docling='http://127.0.0.1:5002/health'; n8n='http://127.0.0.1:5679/healthz/readiness' }
$deadline = (Get-Date).AddMinutes(10)
while ($ready.Count -lt 3 -and (Get-Date) -lt $deadline) {
    foreach ($name in $checks.Keys) {
        if ($ready[$name]) { continue }
        if (-not (Test-NativeProcess $services[$name])) { throw "$name exited; inspect data/logs/$name.error.log" }
        try {
            $response = Invoke-WebRequest -Uri $checks[$name] -TimeoutSec 3 -NoProxy -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200) { $ready[$name]=$true; Write-Output "$name ready" }
        } catch { }
    }
    if ($ready.Count -lt 3) { Start-Sleep -Seconds 2 }
}
if ($ready.Count -lt 3) { throw 'Service startup timed out. Inspect data/logs/native-startup.log.' }
Write-Output 'ConceptRelationDraw services ready: http://127.0.0.1:8092 (n8n: http://127.0.0.1:5679)'
Stop-Transcript | Out-Null
