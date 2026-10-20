@echo off
setlocal
rem ============================================================
rem  CircuitTutor one-click demo launcher (ASCII-only on purpose:
rem  batch files must stay ANSI-safe; Chinese lives in the web UI)
rem ============================================================
title CircuitTutor - AI Circuit Compliance Demo
cd /d "%~dp0"

rem ---- load the local demo-only .env without printing secrets ----
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

rem ---- already running? just open the browser ----
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
  echo Server already running on port 8765. Opening browser...
  start "" http://127.0.0.1:8765
  pause
  exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH. Please install Python 3.10+ first.
  pause
  exit /b 1
)

if not defined DEEPSEEK_API_KEY (
  echo [INFO] DEEPSEEK_API_KEY is not set. The offline template path will be used.
)
set "LSQC_HOST=127.0.0.1"
set "LSQC_PORT=8765"
set "PYTHONUTF8=1"

echo Starting server at http://127.0.0.1:8765 ...
echo The browser will open automatically in about 2 seconds.
echo Keep this window open; press Ctrl+C here to stop the server.
echo.

rem ---- open browser 2s later via a detached helper ----
start "" /min cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"

python -B server.py

echo.
echo Server stopped.
pause
endlocal
