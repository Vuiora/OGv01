param(
    [switch]$SkipCuda,
    [switch]$UseLock
)
$ErrorActionPreference = 'Stop'
$moduleRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $moduleRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv (Join-Path $moduleRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the project Python environment' }
}
if ($UseLock) {
    & $pythonPath -m pip install --only-binary=:all: -r (Join-Path $moduleRoot 'requirements-hardware.lock.txt')
} elseif ($SkipCuda) {
    & $pythonPath -m pip install --only-binary=:all: numpy openvino
} else {
    # Matches the tested NVIDIA 592.01 / CUDA 13.1 driver family.
    & $pythonPath -m pip install --only-binary=:all: openvino 'cupy-cuda13x[ctk]' 'cuda-toolkit<13.2'
}
if ($LASTEXITCODE -ne 0) { throw 'Hardware dependency installation failed' }
& $pythonPath -m pip install --no-deps -e $moduleRoot
if ($LASTEXITCODE -ne 0) { throw 'PLDA installation failed' }
Write-Output 'Installed into the project .venv. Run its python -m plda devices to probe hardware.'
