param([Parameter(Mandatory=$true)][ValidateSet('n8n','docling')][string]$Service)
. (Join-Path $PSScriptRoot 'environment.ps1')
if ($Service -eq 'n8n') {
    $child = Start-Process -FilePath $TaskNode -ArgumentList @(('"'+$TaskN8n+'"'),'start') -WorkingDirectory $TaskRuntime -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $TaskRuntime 'logs\n8n.log') -RedirectStandardError (Join-Path $TaskRuntime 'logs\n8n-error.log')
} else {
    $child = Start-Process -FilePath $TaskPython -ArgumentList @('-m','docling_serve','run','--host','127.0.0.1','--port','5001') -WorkingDirectory $TaskRuntime -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $TaskRuntime 'logs\docling.log') -RedirectStandardError (Join-Path $TaskRuntime 'logs\docling-error.log')
}
exit $child.ExitCode
