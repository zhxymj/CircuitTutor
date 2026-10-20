@echo off
chcp 65001 >nul
setlocal
title Open KiCad Project
cd /d "%~dp0"
set "PRO=candidate.kicad_pro"
if not exist "%PRO%" (
  echo [ERROR] Missing %PRO% in this folder.
  echo Please extract the whole zip first, then run this bat again.
  pause
  exit /b 1
)
set "KICAD_EXE="
if exist "F:\智绘电路工具链\kicad\bin\kicad.exe" set "KICAD_EXE=F:\智绘电路工具链\kicad\bin\kicad.exe"
where kicad.exe >nul 2>nul && for /f "delims=" %%I in ('where kicad.exe') do if not defined KICAD_EXE set "KICAD_EXE=%%I"
if not defined KICAD_EXE if exist "%ProgramFiles%\KiCad\9.0\bin\kicad.exe" set "KICAD_EXE=%ProgramFiles%\KiCad\9.0\bin\kicad.exe"
if not defined KICAD_EXE if exist "%ProgramFiles%\KiCad\8.0\bin\kicad.exe" set "KICAD_EXE=%ProgramFiles%\KiCad\8.0\bin\kicad.exe"
if not defined KICAD_EXE if exist "%ProgramFiles%\KiCad\7.0\bin\kicad.exe" set "KICAD_EXE=%ProgramFiles%\KiCad\7.0\bin\kicad.exe"
if not defined KICAD_EXE if exist "D:\KICAD\bin\kicad.exe" set "KICAD_EXE=D:\KICAD\bin\kicad.exe"
if not defined KICAD_EXE if exist "C:\KICAD\bin\kicad.exe" set "KICAD_EXE=C:\KICAD\bin\kicad.exe"
if not defined KICAD_EXE (
  echo [ERROR] KiCad not found. Install KiCad 8/9, or open %PRO% manually.
  echo Do NOT use File - Import. Use Project Manager - Open Project.
  pause
  exit /b 2
)
echo Opening %PRO% with %KICAD_EXE%
echo If the sheet looks empty, press Home in Eeschema to zoom to fit.
start "" "%KICAD_EXE%" "%cd%\%PRO%"
exit /b 0
