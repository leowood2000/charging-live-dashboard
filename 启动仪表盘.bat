@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python server.py --open
) else (
    py -3 server.py --open
)
echo.
echo If you see an error above, copy these lines and send them to Codex.
pause
