param([switch]$NoBrowser,[ValidateSet('all','n8n','docling')][string]$Service = 'all')
. (Join-Path $PSScriptRoot 'environment.ps1')
New-Item -ItemType Directory -Force -Path (Join-Path $TaskRuntime 'logs'), $env:N8N_USER_FOLDER, $env:HF_HOME, $env:DOCLING_SERVE_SCRATCH_PATH | Out-Null
$records = @()
$recordPath = Join-Path $TaskRuntime 'services.json'
if (Test-Path -LiteralPath $recordPath) { $records = @(Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json) }
function Start-LocalService($name, $port, $exe, $arguments, $signature) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
        if ($owner.CommandLine -notlike "*$signature*") { throw "Port $port is occupied by another application." }
        Write-Output "$name is already running."
        return
    }
    foreach ($record in @($script:records | Where-Object { $_.name -eq $name })) {
        $prior = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
        $priorProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if ($prior -and $priorProcess -and $prior.CommandLine -like "*$($record.signature)*" -and [Math]::Abs(($priorProcess.StartTime.ToUniversalTime() - [datetime]$record.started).TotalSeconds) -lt 2) {
            Write-Output "$name is already starting."
            return
        }
    }
    # WMI launches the hidden service outside the caller's terminal job, so closing
    # the setup terminal or a Codex turn does not terminate the document services.
    $runner = Join-Path $PSScriptRoot 'Run-Service.ps1'
    $powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $commandLine = '"' + $powershellExe + '" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '" -Service ' + $name
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$commandLine;CurrentDirectory=$TaskRuntime;ProcessStartupInformation=$startup}
    if ($created.ReturnValue -ne 0) { throw "Failed to start $name (WMI result $($created.ReturnValue))." }
    $process = Get-Process -Id $created.ProcessId
    $script:records = @($script:records | Where-Object { $_.name -ne $name }) + @(@{name=$name;pid=$process.Id;signature=($runner+'" -Service '+$name);started=$process.StartTime.ToUniversalTime().ToString('o')})
    Write-Output "$name starting (PID $($process.Id))."
}
if ($Service -in @('all','docling')) { Start-LocalService 'docling' 5001 $TaskPython @('-m','docling_serve','run','--host','127.0.0.1','--port','5001') 'docling_serve run' }
if ($Service -in @('all','n8n')) { Start-LocalService 'n8n' 5678 $TaskNode @(('"'+$TaskN8n+'"'),'start') $TaskN8n }
$records | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $recordPath -Encoding UTF8
if (-not $NoBrowser) {
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5678/healthz' -TimeoutSec 2 | Out-Null
            Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5001/ready' -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Services are not ready yet. Check work\n8n-docling\logs, then try again.' }
    Start-Process 'http://127.0.0.1:5678/form/og-blog-translate'
}
