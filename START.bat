@echo off
chcp 65001 > nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
title BRS - دستیار نوسان گیری بورس

if not exist "venv\Scripts\python.exe" (
    echo برنامه هنوز نصب نشده؛ نصب خودکار شروع می شود...  ^(running setup^)
    call "%~dp0SETUP.bat"
    if errorlevel 1 exit /b 1
    if not exist "venv\Scripts\python.exe" exit /b 1
)

rem پیش از اجرا، نسخه بسته‌ها و رسید فایل قفل بررسی شوند.
venv\Scripts\python.exe -m ops.environment --check > nul 2> nul
if errorlevel 1 (
    echo [BRS] محیط تغییر کرده؛ همگام‌سازی وابستگی‌های قفل‌شده با SETUP ...
    call "%~dp0SETUP.bat" --ci
    if errorlevel 1 exit /b 1
    venv\Scripts\python.exe -m ops.environment --check
    if errorlevel 1 exit /b 1
)

set PYTHONIOENCODING=utf-8
echo ==============================================================
echo   مرورگر پس از راه‌اندازی سرویس محلی BRS باز می‌شود.
echo   برای خاموش کردن، این پنجره را ببندید.   ^(close this window to stop^)
echo ==============================================================
venv\Scripts\python.exe -m streamlit run dashboard/app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=false --browser.serverAddress=127.0.0.1 --browser.serverPort=8501
set "BRS_START_EXIT=%errorlevel%"
if not "%BRS_START_EXIT%"=="0" echo [BRS] Dashboard startup or runtime failed. See the message above.
pause
exit /b %BRS_START_EXIT%
