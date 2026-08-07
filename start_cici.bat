@echo off
REM ============================================================
REM  Start Cici with Chrome DevTools Protocol (remote debugging)
REM  Required before running the Cici API wrapper.
REM ============================================================
set CICI_EXE=%LOCALAPPDATA%\Cici\Application\app\Cici.exe
set USER_DATA=%LOCALAPPDATA%\Cici\User Data

echo [1] Stopping any running Cici instances...
taskkill /F /IM Cici.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2] Launching Cici with --remote-debugging-port=9222 ...
start "" "%CICI_EXE%" --remote-debugging-port=9222 --user-data-dir="%USER_DATA%"

echo [3] Waiting for CDP endpoint (port 9222) ...
:wait
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 3).StatusCode } catch { 0 }" > %TEMP%\cdp_check.txt
set /p CODE=<%TEMP%\cdp_check.txt
if not "%CODE%"=="200" goto wait

echo.
echo [OK] Cici is running with CDP on http://127.0.0.1:9222
echo      Now start the API:  uvicorn main:app --port 8000
echo.
echo Keep this Cici window open. Do NOT close it.
pause
