@echo off
setlocal
cd /d "%~dp0"

echo Requesting dashboard shutdown...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/system/shutdown' -Method Post -TimeoutSec 5; $response | ConvertTo-Json -Depth 6 } catch { Write-Output $_.Exception.Message; exit 1 }"

if errorlevel 1 (
  echo.
  echo Shutdown request failed. If the dashboard is still running, check logs\backend.log.
  exit /b 1
)

echo.
echo Shutdown request sent. You can close the browser tab if it is still open.
