@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".\venv\Scripts\python.exe" (
  echo Python project environment was not found. No files were removed.
  pause
  exit /b 1
)
echo Archiving and checking public CODAL documents. This can take several minutes.
echo Loose originals are removed only after exact recovery checks succeed.
if "%~1"=="" (
  ".\venv\Scripts\python.exe" -X utf8 -m ops.codal_compact sync --apply
) else (
  ".\venv\Scripts\python.exe" -X utf8 -m ops.codal_compact sync --notice-index "%~f1" --apply
)
if errorlevel 1 (
  echo Operation failed. Read the error above; do not delete any files manually.
  pause
  exit /b 1
)
echo Done. Original document bytes remain recoverable in the compact archive.
pause
