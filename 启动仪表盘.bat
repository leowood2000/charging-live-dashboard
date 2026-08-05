@echo off
cd /d "%~dp0"
set "ADBARG="
if not "%~1"=="" set "ADBARG=--adb-host %~1"
where python >nul 2>nul
if %errorlevel%==0 (
    python server.py --open %ADBARG%
) else (
    py -3 server.py --open %ADBARG%
)
echo.
echo If you see an error above, copy these lines and send them to Codex.
pause
