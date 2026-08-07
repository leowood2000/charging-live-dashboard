@echo off
setlocal
title 充电实时仪表盘
cd /d "%~dp0"

echo ============================================
echo  充电实时仪表盘 启动脚本
echo  目录: %~dp0
echo ============================================

REM 停止可能仍在运行的旧实例（防止旧进程占用 8765，导致页面停留在旧版本）
echo [1/3] 停止旧服务实例...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ps = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'server\.py' }; if ($ps) { $ps | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('已停止 ' + $ps.Count + ' 个旧实例') } else { Write-Host '无旧实例' }"
timeout /t 1 /nobreak >nul

REM 显示当前代码版本（git 短哈希）
set "VER=dev"
for /f "delims=" %%v in ('git -C "%~dp0" rev-parse --short HEAD 2^>nul') do set "VER=%%v"
echo [2/3] 当前代码版本: %VER%

set "ADBARG="
if not "%~1"=="" (
    set "ADBARG=--adb-host %~1 --serial %~1"
) else (
    set "ADBARG=--adb-host 192.168.5.13:5555 --serial 192.168.5.13:5555"
)

echo [3/3] 启动服务 (port 8765)...
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py --open %ADBARG%
) else (
    python server.py --open %ADBARG%
)
echo.
echo 服务已退出。如需排查错误，请复制上方输出发给 Codex。
pause
