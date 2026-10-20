@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if /I "%~1"=="--check" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\capture_farabi.ps1" -Watch -Check
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\capture_farabi.ps1" -Watch
)
set "watch_exit=%ERRORLEVEL%"
if /I "%~1"=="--check" exit /b %watch_exit%
pause
exit /b %watch_exit%
