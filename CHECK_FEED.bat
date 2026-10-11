@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Run SETUP.bat first.
  exit /b 1
)
set "PYTHONUTF8=1"
venv\Scripts\python.exe brs.py doctor --market-only --json logs\doctor-feed.json
set "BRS_EXIT=%errorlevel%"
echo Report: logs\doctor-feed.json
pause
exit /b %BRS_EXIT%
