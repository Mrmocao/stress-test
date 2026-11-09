$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$debugPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $debugPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required. Install Python, then retry.' }
}
& $debugPython -m pip install -r (Join-Path $PSScriptRoot 'requirements-debug.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $debugPython -c "from playback_runtime import ffmpeg_executable; import subprocess; subprocess.run([ffmpeg_executable(), '-version'], check=True)"
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg verification failed.' }
Write-Host 'Ready. Run .\start-debug.ps1'
