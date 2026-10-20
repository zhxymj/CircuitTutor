@echo off
rem ============================================================================
rem  智绘电路 · 本地一键打包（Windows）
rem  用途：把项目打成 zhihui.tar.gz，供上传到服务器后用 update.sh 更新
rem  说明：基于 git archive 只打包【已提交】的文件，天然排除 __pycache__、
rem        测试产物、日志等未跟踪文件 —— 包永远是干净的。
rem ============================================================================
setlocal
chcp 65001 >nul
title 智绘电路 - 本地打包

rem ---- 定位 git 仓库根（本脚本可能在 智绘电路\deploy\ 下）-------------------
cd /d "%~dp0..\.."
for /f "delims=" %%i in ('git rev-parse --show-toplevel 2^>^&1') do set "GITROOT=%%i"
if not defined GITROOT (
  echo [失败] 未找到 git 仓库。请确认本脚本位于项目的 deploy\ 目录下。
  pause & exit /b 1
)
cd /d "%GITROOT%"

rem ---- 确认项目子目录存在 ---------------------------------------------------
if not exist "智绘电路\server.py" (
  echo [失败] 未找到 智绘电路\server.py，仓库结构可能已变动。
  pause & exit /b 1
)

rem ---- 检查未提交的改动（提醒，不阻断）-------------------------------------
echo [打包] 检查工作区状态...
git status --porcelain > "%TEMP%\zh_status.txt" 2>nul
for %%A in ("%TEMP%\zh_status.txt") do if %%~zA GTR 0 (
  echo [注意] 存在未提交的改动，这些改动【不会】被打包：
  echo ------------------------------------------------------------
  git status --short
  echo ------------------------------------------------------------
  echo   如需包含，请先 git add ^&^& git commit
  echo.
)

rem ---- 执行打包 -------------------------------------------------------------
set "OUT=%USERPROFILE%\Desktop\zhihui.tar.gz"
echo [打包] 输出到：%OUT%
git archive --format=tar --prefix=zhihui/ HEAD:智绘电路 | gzip -9 > "%OUT%"

if not exist "%OUT%" (
  echo [失败] 打包失败。
  pause & exit /b 1
)

rem ---- 校验结果 -------------------------------------------------------------
for %%A in ("%OUT%") do set "SZ=%%~zA"
echo.
echo [成功] 打包完成：%OUT%
echo   大小：%SZ% 字节
echo.
echo   内含文件数（校验用）：
git archive --format=tar --prefix=zhihui/ HEAD:智绘电路 | tar -t | find /c "/" 
echo.
echo 下一步：
echo   1. 在 Workbench 网页终端上传 %OUT%
echo   2. 服务器上执行：bash /srv/zhihui/deploy/update.sh
echo.
pause
