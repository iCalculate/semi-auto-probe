@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PID_FILE=%PROJECT_DIR%.runtime\semi-auto-probe-web.pid"
set "WEB_TOKEN=GEMsE70403"

if exist "%PID_FILE%" (
  set /p WEB_PID=<"%PID_FILE%"
  if defined WEB_PID (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Get-Process -Id %WEB_PID% -ErrorAction Stop; Stop-Process -Id %WEB_PID% -Force; Start-Sleep -Milliseconds 500 } catch { }"
  )
)

if not exist "%PROJECT_DIR%.runtime" mkdir "%PROJECT_DIR%.runtime"

start "Semi Auto Probe Web" /min powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-Location -LiteralPath '%PROJECT_DIR%'; $env:SEMI_AUTO_PROBE_WEB_TOKEN='%WEB_TOKEN%'; uv run semi-auto-probe-web"

endlocal
