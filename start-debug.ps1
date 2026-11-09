param([int]$Port = 8765, [switch]$Foreground)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$debugPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $debugPython)) {
    & (Join-Path $PSScriptRoot 'setup-debug.ps1')
}
if ($Foreground) {
    & $debugPython -u (Join-Path $PSScriptRoot 'debug_server.py') --port $Port
    exit $LASTEXITCODE
}
$debugListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($debugListener) {
    throw "Port $Port is in use. Open http://127.0.0.1:$Port if the inspector is already running, or choose -Port 8766."
}
$debugLogs = Join-Path $PSScriptRoot '.debug-server'
New-Item -ItemType Directory -Path $debugLogs -Force | Out-Null
$debugWorkdir = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$debugProcess = Start-Process -FilePath $debugPython -ArgumentList @('-u', 'debug_server.py', '--port', $Port) -WorkingDirectory $debugWorkdir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $debugLogs "stdout-$Port.log") -RedirectStandardError (Join-Path $debugLogs "stderr-$Port.log")
$debugProcess.Id | Set-Content -LiteralPath (Join-Path $debugLogs "server-$Port.pid") -Encoding ascii
Write-Host "Runtime inspector: http://127.0.0.1:$Port"
Write-Host "Server PID: $($debugProcess.Id). Close with Stop-Process -Id $($debugProcess.Id) after stopping active playback in the page."
