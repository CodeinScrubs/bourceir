@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "RADAR_PY=venv\Scripts\python.exe"
if not exist "%RADAR_PY%" set "RADAR_PY=.venv\Scripts\python.exe"
if not exist "%RADAR_PY%" (
    echo First run SETUP.bat.
    pause
    exit /b 1
)
"%RADAR_PY%" brs.py radar start
"%RADAR_PY%" brs.py radar status
echo Radar runs locally while this computer remains on. Open the BRS radar page for alerts.
pause
