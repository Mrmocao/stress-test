param([int]$Port = 8766, [switch]$Foreground)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$monitorPython = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $monitorPython)) { $monitorPython = (Get-Command python -ErrorAction Stop).Source }
if ($Foreground) {
    & $monitorPython -u monitor.py --port $Port
    exit $LASTEXITCODE
}
$monitorListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($monitorListener) { Write-Host "Port $Port is in use. Check http://127.0.0.1:$Port"; exit 0 }
$monitorLogDir = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $monitorLogDir -Force | Out-Null
$monitorProcess = Start-Process -FilePath $monitorPython -ArgumentList @('-u', 'monitor.py', '--port', $Port) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $monitorLogDir 'server.stdout.log') -RedirectStandardError (Join-Path $monitorLogDir 'server.stderr.log')
$monitorProcess.Id | Set-Content -LiteralPath (Join-Path $monitorLogDir 'server.pid') -Encoding ascii
Write-Host "Public view monitor: http://127.0.0.1:$Port"
Write-Host "Server PID: $($monitorProcess.Id)"
