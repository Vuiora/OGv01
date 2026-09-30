param([int]$Port=8095)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run tools/business/setup.ps1 first' }
Push-Location -LiteralPath $taskRoot
try { & $taskPython -m ogflow.cli serve --port $Port }
finally { Pop-Location }

