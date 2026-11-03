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

rem اشغال بودن پورت هویت سرویس را ثابت نمی‌کند؛ سرویس نامعلوم را باز نکنید.
netstat -ano | findstr /R /C:":8501 .*LISTENING" > nul
if %errorlevel%==0 (
    echo [BRS] Port 8501 is already in use; the service identity was not verified.
    echo If BRS is already open, use its existing tab. Otherwise free the port and retry.
    exit /b 1
)

set PYTHONIOENCODING=utf-8
echo ==============================================================
echo   داشبورد BRS در حال اجراست. مرورگر چند ثانیه دیگر باز می شود.
echo   برای خاموش کردن، این پنجره را ببندید.   ^(close this window to stop^)
echo ==============================================================
start "" cmd /c "timeout /t 5 > nul & start "" http://127.0.0.1:8501"
venv\Scripts\python.exe -m streamlit run dashboard/app.py
pause
