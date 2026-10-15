@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Installing local Node.js, Playwright and Chromium for read-only Farabi capture...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_farabi.ps1"
set "setup_exit=%ERRORLEVEL%"
if not "%setup_exit%"=="0" echo Setup failed. Read the error above and docs/farabi-guide.md.
if /I not "%~1"=="--ci" pause
exit /b %setup_exit%
