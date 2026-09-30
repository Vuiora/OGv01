#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$flowRoot = Split-Path -Parent $PSScriptRoot
$flowStatePath = Join-Path $flowRoot 'data\local-processes.json'
if (!(Test-Path -LiteralPath $flowStatePath)) { Write-Output 'No managed local services found.'; exit 0 }
$flowServices = Get-Content -LiteralPath $flowStatePath -Raw | ConvertFrom-Json -AsHashtable
function Stop-FlowChildren([int]$parent) {
    foreach ($child in (Get-CimInstance Win32_Process -Filter "ParentProcessId = $parent")) {
        Stop-FlowChildren $child.ProcessId
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
}
foreach ($flowName in @('n8n','docling','api')) {
    $flowRecord = $flowServices[$flowName]
    if (!$flowRecord) { continue }
    $flowProcess = Get-Process -Id $flowRecord.pid -ErrorAction SilentlyContinue
    if ($flowProcess -and $flowProcess.StartTime.ToFileTimeUtc().ToString() -eq [string]$flowRecord.started) {
        Stop-FlowChildren $flowProcess.Id
        Stop-Process -Id $flowProcess.Id -ErrorAction SilentlyContinue
        Write-Output "Stopped $flowName (PID $($flowRecord.pid))"
    }
}
