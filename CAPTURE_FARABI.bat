@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ابزار فقط گزارش می‌خواند. خودتان در مرورگر وارد حساب شوید و سپس Enter بزنید.
if /I "%~1"=="--check" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\capture_farabi.ps1" -Check
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\capture_farabi.ps1"
)
set "capture_exit=%ERRORLEVEL%"
if not "%capture_exit%"=="0" echo برداشت کامل نشد. خروجی ناقص را وارد نکنید؛ راهنمای فارابی را بخوانید.
if /I "%~1"=="--check" exit /b %capture_exit%
pause
exit /b %capture_exit%
