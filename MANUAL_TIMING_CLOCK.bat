@echo off
setlocal DisableDelayedExpansion
set "clock_ps=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "clock_ps=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%clock_ps%" (
  echo Windows PowerShell 5.1 was not found.
  pause
  exit /b 1
)
if not exist "%~dp0scripts\start_manual_timing_clock.ps1" (
  echo The clock launcher is missing. Keep this file beside the scripts folder.
  pause
  exit /b 1
)
"%clock_ps%" -NoLogo -NoProfile -STA -File "%~dp0scripts\start_manual_timing_clock.ps1"
set "clock_exit=%errorlevel%"
if not "%clock_exit%"=="0" (
  echo.
  echo Timing Clock did not start successfully. Review the error above.
  pause
)
exit /b %clock_exit%
