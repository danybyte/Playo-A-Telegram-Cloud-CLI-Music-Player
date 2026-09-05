@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py setup.py
) else (
    python setup.py
)
pause
