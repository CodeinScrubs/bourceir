@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Project Python environment is missing. Open this chat for help.
  pause
  exit /b 1
)
"venv\Scripts\python.exe" "scripts\farabi_morning_check.py" --isin IRO3SIMC0001 --target 2026-10-31T08:45:00
if errorlevel 1 (
  echo Diagnostics did not start. Check the explicit date and time and try again.
  pause
  exit /b 1
)
endlocal
