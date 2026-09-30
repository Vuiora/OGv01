$ErrorActionPreference = 'Stop'
$clcRoot = Split-Path -Parent $PSScriptRoot
$clcPidFile = Join-Path $clcRoot 'data\runtime\processes.json'
if (-not (Test-Path -LiteralPath $clcPidFile)) { Write-Output 'No recorded CLC processes.'; return }
$clcSaved = Get-Content -LiteralPath $clcPidFile -Raw | ConvertFrom-Json
foreach ($clcRecord in $clcSaved) {
    $clcProcess = Get-Process -Id $clcRecord.id -ErrorAction SilentlyContinue
    if ($clcProcess -and $clcProcess.StartTime.ToUniversalTime().ToString('o') -eq $clcRecord.started) {
        $clcInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($clcRecord.id)"
        if ($clcInfo.CommandLine -notmatch 'clc\.(api|worker)' -or $clcInfo.ExecutablePath -notlike "$clcRoot\*") {
            throw 'Process identity differs; refusing to stop an unrelated process.'
        }
        if ($clcRecord.name -eq 'worker') {
            $clcChildren = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($clcRecord.id)"
            foreach ($clcChild in $clcChildren) {
                if ($clcChild.CommandLine -match 'clc\.run_job' -and $clcChild.ExecutablePath -like "$clcRoot\*") {
                    Stop-Process -Id $clcChild.ProcessId -ErrorAction SilentlyContinue
                }
            }
        }
        Stop-Process -Id $clcRecord.id
    }
}
Remove-Item -LiteralPath $clcPidFile
Write-Output 'Stopped recorded CLC processes. Interrupted jobs fail when the next worker recovers expired leases.'
