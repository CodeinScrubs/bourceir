@echo off
chcp 65001 > nul
setlocal
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

rem اگر داشبورد از قبل در حال اجراست، فقط مرورگر باز شود
netstat -ano | findstr /R /C:":8501 .*LISTENING" > nul
if %errorlevel%==0 (
    start "" "http://127.0.0.1:8501"
    exit /b 0
)

set PYTHONIOENCODING=utf-8
echo ==============================================================
echo   داشبورد BRS در حال اجراست. مرورگر چند ثانیه دیگر باز می شود.
echo   برای خاموش کردن، این پنجره را ببندید.   ^(close this window to stop^)
echo ==============================================================
start "" cmd /c "timeout /t 5 > nul & start "" http://127.0.0.1:8501"
venv\Scripts\python.exe -m streamlit run dashboard/app.py
pause
