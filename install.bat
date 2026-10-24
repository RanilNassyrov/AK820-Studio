@echo off
rem AK820 Studio installer. The real work is in install.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
timeout /t 5 >nul 2>&1
exit /b 0
