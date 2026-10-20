@echo off
rem ============================================================
rem  CircuitTutor one-click regression self-check
rem  (ASCII-only on purpose: batch files must stay ANSI-safe)
rem ============================================================
title CircuitTutor - regression self-check
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH. Please install Python 3.10+ first.
  pause
  exit /b 1
)

python -B tests/run_tests.py %*
echo.
pause
