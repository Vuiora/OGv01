[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$Csv,

  [Parameter(Mandatory = $true)]
  [string]$Target,

  [int]$K = 20,
  [int]$Cv = 5,
  [string]$Methods = "pearson,spearman,mi",
  [string]$Store = "experience/experience.jsonl",
  [int]$Repeats = 1,
  [int]$TimeBudgetSec = 0,
  [UInt64]$Seed = 0,
  [ValidateSet("Debug", "ReleaseSafe", "ReleaseFast", "ReleaseSmall")]
  [string]$Optimize = "ReleaseFast",

  # 透传给 zig build run 的额外参数（会追加到末尾）
  [string[]]$ExtraZigArgs = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Update-PathForCurrentProcess {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  if ([string]::IsNullOrWhiteSpace($machine)) { $machine = "" }
  if ([string]::IsNullOrWhiteSpace($user)) { $user = "" }
  $env:Path = $machine + ";" + $user
}

function Assert-ZigAvailable {
  try {
    $null = Get-Command zig -ErrorAction Stop
  } catch {
    throw "未检测到 Zig（命令 zig 不可用）。请先安装 Zig，例如：winget install -e --id zig.zig"
  }
}

function Resolve-AbsolutePath([string]$path, [string]$baseDir) {
  if ([System.IO.Path]::IsPathRooted($path)) {
    return [System.IO.Path]::GetFullPath($path)
  }
  return [System.IO.Path]::GetFullPath((Join-Path $baseDir $path))
}

function Get-RelativePathCompat([string]$baseDir, [string]$targetPath) {
  # Windows PowerShell 5.1 / .NET Framework does not have [System.IO.Path]::GetRelativePath.
  # Use Uri relative path; if different drive, fall back to absolute.
  $baseFull = [System.IO.Path]::GetFullPath($baseDir)
  $targetFull = [System.IO.Path]::GetFullPath($targetPath)

  if ($baseFull.Length -ge 2 -and $targetFull.Length -ge 2) {
    $baseDrive = $baseFull.Substring(0, 2).ToLowerInvariant()
    $targetDrive = $targetFull.Substring(0, 2).ToLowerInvariant()
    if ($baseDrive -ne $targetDrive) {
      return $targetFull
    }
  }

  if (-not $baseFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
    $baseFull = $baseFull + [System.IO.Path]::DirectorySeparatorChar
  }

  $baseUri = [Uri]$baseFull
  $targetUri = [Uri]$targetFull
  $rel = $baseUri.MakeRelativeUri($targetUri).ToString()

  # Uri returns '/' separators; convert to Windows separator for nicer logs.
  return ($rel -replace "/", [System.IO.Path]::DirectorySeparatorChar)
}

# 约定：该脚本位于 <repo>/scripts/ 下
$repoRoot = Split-Path -Parent $PSScriptRoot
$zigDir = Join-Path $repoRoot "zig_version"

if (-not (Test-Path $zigDir)) {
  throw "未找到 zig_version 目录：$zigDir"
}

Update-PathForCurrentProcess
Assert-ZigAvailable

# 输入路径按“仓库根目录”解析；运行时切到 zig_version，因此需要把路径改成相对 zig_version 的路径
$csvAbs = Resolve-AbsolutePath $Csv $repoRoot
$storeAbs = Resolve-AbsolutePath $Store $repoRoot

if (-not (Test-Path $csvAbs)) {
  throw "CSV 文件不存在：$csvAbs"
}

$csvRelFromZigDir = Get-RelativePathCompat $zigDir $csvAbs
$storeRelFromZigDir = Get-RelativePathCompat $zigDir $storeAbs

Write-Host "RepoRoot : $repoRoot"
Write-Host "ZigDir   : $zigDir"
Write-Host "CSV      : $csvAbs (as $csvRelFromZigDir)"
Write-Host "Store    : $storeAbs (as $storeRelFromZigDir)"
Write-Host "Optimize : $Optimize"
Write-Host "Methods  : $Methods"
Write-Host "K/CV     : $K / $Cv"
Write-Host "Repeats  : $Repeats"
Write-Host "Budget(s): $TimeBudgetSec"
Write-Host "Seed     : $Seed"

Push-Location $zigDir
try {
  $zigArgs = @(
    "run",
    "src/main.zig",
    "-O", $Optimize,
    "--",
    "--csv", $csvRelFromZigDir,
    "--target", $Target,
    "--k", "$K",
    "--cv", "$Cv",
    "--methods", $Methods,
    "--store", $storeRelFromZigDir,
    "--repeats", "$Repeats",
    "--time-budget-sec", "$TimeBudgetSec",
    "--seed", "$Seed"
  )

  if ($ExtraZigArgs.Count -gt 0) {
    $zigArgs += $ExtraZigArgs
  }

  # 直接调用 zig（避免 PowerShell 解析/转义问题）
  & zig @zigArgs
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}


