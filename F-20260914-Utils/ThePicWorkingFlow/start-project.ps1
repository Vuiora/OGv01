[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSCommandPath
$startScript = Join-Path $projectRoot 'scripts\start-local.ps1'

if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "启动脚本不存在: $startScript"
}

Push-Location $projectRoot
try {
    $currentIsSupported = $PSVersionTable.PSVersion -ge [Version]'7.4'
    if ($currentIsSupported) {
        & $startScript
    } else {
        $pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
        if (-not $pwshCommand) {
            throw '当前是 Windows PowerShell 5.1。请安装 PowerShell 7.4 或以上版本（命令 pwsh），再运行此脚本。'
        }
        & $pwshCommand.Source -NoProfile -ExecutionPolicy Bypass -File $startScript
    }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "本地服务启动失败，退出码: $LASTEXITCODE"
    }

    $healthUrls = @(
        'http://127.0.0.1:8080/health',
        'http://127.0.0.1:5001/health',
        'http://127.0.0.1:5678/healthz/readiness'
    )
    # First-time n8n database/workflow initialization can take several minutes.
    $deadline = (Get-Date).AddMinutes(10)
    do {
        $ready = $true
        foreach ($healthUrl in $healthUrls) {
            try {
                $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing
                if ($response.StatusCode -ne 200) { $ready = $false }
            } catch {
                $ready = $false
            }
        }
        if (-not $ready) { Start-Sleep -Seconds 2 }
    } while (-not $ready -and (Get-Date) -lt $deadline)

    if (-not $ready) {
        throw '服务启动超时，请检查 data\logs\startup.log。'
    }

    Write-Host '项目已启动:' -ForegroundColor Green
    Write-Host '  工作台: http://127.0.0.1:8080'
    Write-Host '  n8n:    http://127.0.0.1:5678'
    if (-not $NoBrowser) {
        Start-Process 'http://127.0.0.1:8080'
    }
} finally {
    Pop-Location
}
