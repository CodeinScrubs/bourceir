@echo off
setlocal
chcp 65001 > nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
title BRS - Export development market evidence

if not exist "venv\Scripts\python.exe" (
    echo [BRS] Project Python is missing. Run SETUP.bat, then try again.
    pause
    exit /b 1
)

venv\Scripts\python.exe -X utf8 -m ops.environment --check
if errorlevel 1 (
    echo [BRS] Project environment check failed. Run SETUP.bat and review its output.
    pause
    exit /b 1
)

venv\Scripts\python.exe -X utf8 -m ops.research_bundle --latest
if errorlevel 1 (
    echo [BRS] Export failed. No missing archive is replaced with fabricated data.
    echo [BRS] Keep the error above for review. Do not send your entire database.
    pause
    exit /b 1
)

echo [BRS] Export completed. The ZIP path is printed above.
echo [BRS] Review the ZIP before sharing it. This command uploads nothing.
pause
exit /b 0
