param([ValidateSet('all','n8n','docling')][string]$Service = 'all')
. (Join-Path $PSScriptRoot 'environment.ps1')
$recordPath = Join-Path $TaskRuntime 'services.json'
if (-not (Test-Path -LiteralPath $recordPath)) { exit }
$processes = @(Get-CimInstance Win32_Process)
function Stop-ServiceTree([int]$targetId) {
    foreach ($child in $processes | Where-Object { $_.ParentProcessId -eq $targetId }) { Stop-ServiceTree $child.ProcessId }
    Stop-Process -Id $targetId -ErrorAction SilentlyContinue
}
foreach ($record in @(Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json)) {
    if ($Service -ne 'all' -and $record.name -ne $Service) { continue }
    $found = $processes | Where-Object { $_.ProcessId -eq $record.pid }
    if ($found -and $found.CommandLine -like "*$($record.signature)*") {
        $running = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        if ($running -and [Math]::Abs(($running.StartTime.ToUniversalTime() - [datetime]$record.started).TotalSeconds) -lt 2) {
            Stop-ServiceTree $record.pid
            Write-Output "$($record.name) stopped."
        }
    }
}
