$ErrorActionPreference = 'Stop'

$apiKey = if ([string]::IsNullOrWhiteSpace($env:CODEXZH_API_KEY)) {
    '__API_KEY__'
} else {
    $env:CODEXZH_API_KEY
}
if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -eq '__API_KEY__') {
    throw '未检测到 API Key，请设置 CODEXZH_API_KEY 后重试'
}

$codexDir = Join-Path $env:USERPROFILE '.codex'
$configPath = Join-Path $codexDir 'config.toml'
$authPath = Join-Path $codexDir 'auth.json'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$topValues = [ordered]@{
    'model_provider' = 'model_provider = "codexzh"'
    'model' = 'model = "gpt-5.6-terra"'
    'model_reasoning_effort' = 'model_reasoning_effort = "high"'
    'disable_response_storage' = 'disable_response_storage = false'
    'cache_size_mb' = 'cache_size_mb = 512'
    'cache_ttl' = 'cache_ttl = "30m"'
    'smart_cache' = 'smart_cache = true'
    'cache_compression' = 'cache_compression = true'
}

New-Item -ItemType Directory -Path $codexDir -Force | Out-Null

if (Test-Path -LiteralPath $configPath) {
    Copy-Item -LiteralPath $configPath -Destination "$configPath.bak" -Force
    $sourceLines = @(Get-Content -LiteralPath $configPath -Encoding UTF8)
} else {
    $sourceLines = @()
}

$keptLines = New-Object System.Collections.Generic.List[string]
$skipProvider = $false
foreach ($line in $sourceLines) {
    if ($line -match '^\s*\[model_providers\.codexzh\]\s*$') {
        $skipProvider = $true
        continue
    }
    if ($skipProvider -and $line -match '^\s*\[') {
        $skipProvider = $false
    }
    if (-not $skipProvider) {
        $keptLines.Add([string]$line)
    }
}

$outputLines = New-Object System.Collections.Generic.List[string]
$done = @{}
$inTop = $true
foreach ($line in $keptLines) {
    if ($inTop -and $line -match '^\s*\[') {
        foreach ($key in $topValues.Keys) {
            if (-not $done.ContainsKey($key)) {
                $outputLines.Add($topValues[$key])
                $done[$key] = $true
            }
        }
        $outputLines.Add('')
        $inTop = $false
    }
    if ($inTop) {
        $matchedKey = $null
        foreach ($key in $topValues.Keys) {
            if ($line -match ('^\s*' + [regex]::Escape($key) + '\s*=')) {
                $matchedKey = $key
                break
            }
        }
        if ($null -ne $matchedKey) {
            if (-not $done.ContainsKey($matchedKey)) {
                $outputLines.Add($topValues[$matchedKey])
                $done[$matchedKey] = $true
            }
            continue
        }
    }
    $outputLines.Add([string]$line)
}
foreach ($key in $topValues.Keys) {
    if (-not $done.ContainsKey($key)) {
        $outputLines.Add($topValues[$key])
        $done[$key] = $true
    }
}

$outputLines.Add('')
$outputLines.Add('[model_providers.codexzh]')
$outputLines.Add('name = "codexzh"')
$outputLines.Add('base_url = "https://api.codexzh.com/v1"')
$outputLines.Add('wire_api = "responses"')
$outputLines.Add('requires_openai_auth = true')
$outputLines.Add('web_search = "live"')

if (Test-Path -LiteralPath $configPath) {
    [System.IO.File]::SetAttributes($configPath, [System.IO.FileAttributes]::Normal)
}
[System.IO.File]::WriteAllText($configPath, (($outputLines -join [Environment]::NewLine) + [Environment]::NewLine), $utf8)

if (Test-Path -LiteralPath $authPath) {
    Copy-Item -LiteralPath $authPath -Destination "$authPath.bak" -Force
    [System.IO.File]::SetAttributes($authPath, [System.IO.FileAttributes]::Normal)
}
$authJson = [PSCustomObject]@{ OPENAI_API_KEY = $apiKey } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($authPath, $authJson + [Environment]::NewLine, $utf8)

Write-Host "配置完成：$codexDir" -ForegroundColor Green
Write-Host '请重启 Codex/VS 后再连接。' -ForegroundColor Green
