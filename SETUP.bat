@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo [1/5] Preparing Python 3.11...
where uv >nul 2>nul
if errorlevel 1 goto missinguv
if not exist "venv\Scripts\python.exe" (
  uv venv --python 3.11 venv
  if errorlevel 1 goto failed
)
venv\Scripts\python.exe -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,11) else 1)"
if errorlevel 1 goto wrongpython
echo [2/5] Installing locked dependencies...
uv pip sync --python venv\Scripts\python.exe requirements.lock.txt
if errorlevel 1 goto failed
uv pip check --python venv\Scripts\python.exe
if errorlevel 1 goto failed
echo [3/5] Checking code and offline regression tests...
venv\Scripts\ruff.exe check .
if errorlevel 1 goto failed
venv\Scripts\python.exe -m pytest -m "not network"
if errorlevel 1 goto failed
echo [4/5] Preparing optional local settings...
if not exist ".env" copy ".env.example" ".env" >nul
if not exist logs mkdir logs
echo [5/5] Checking the local database...
venv\Scripts\python.exe brs.py doctor --offline --json logs\doctor-local.json
if errorlevel 1 goto failed
echo LOCAL SETUP PASSED. Market access has NOT been tested.
echo AI keys are optional. Next: CHECK_FEED.bat, then START.bat
if /I not "%~1"=="--ci" pause
exit /b 0
:missinguv
echo Install uv first: winget install --id astral-sh.uv -e
echo Close and reopen Terminal, then run SETUP.bat again.
goto failed
:wrongpython
echo Existing venv is not Python 3.11. Rename the venv folder and rerun SETUP.bat.
goto failed
:failed
echo SETUP FAILED. Copy the FIRST error above. Data has not been deleted.
if /I not "%~1"=="--ci" pause
exit /b 1
