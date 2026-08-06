@echo off
cd /d "%~dp0"
set "ADBARG="
if not "%~1"=="" (
    set "ADBARG=--adb-host %~1 --serial %~1"
) else (
    set "ADBARG=--adb-host 192.168.5.13:5555 --serial 192.168.5.13:5555"
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py --open %ADBARG%
) else (
    python server.py --open %ADBARG%
)
echo.
echo If you see an error above, copy these lines and send them to Codex.
pause
