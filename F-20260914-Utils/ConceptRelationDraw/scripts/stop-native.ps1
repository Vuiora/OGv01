#requires -Version 7.4
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $projectRoot 'data\native-processes.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Output 'No managed services found.'; exit 0 }
$services = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable
function Stop-NativeChildren([int]$parentId) {
    foreach ($child in (Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId")) {
        Stop-NativeChildren $child.ProcessId
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
}
foreach ($name in @('n8n','docling','api')) {
    $record = $services[$name]
    if (-not $record) { continue }
    $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($running -and $running.StartTime.ToFileTimeUtc().ToString() -eq [string]$record.started) {
        Stop-NativeChildren $running.Id
        Stop-Process -Id $running.Id -ErrorAction SilentlyContinue
        Write-Output "Stopped $name"
    }
}
