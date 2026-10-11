@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"
title BRS Setup - نصب برنامه

echo ==============================================================
echo   BRS - نصب و راه اندازی اولیه   (First-time setup)
echo ==============================================================
echo.

rem ---- 1) ساخت محیط پایتون 3.11 ----
if exist "venv\Scripts\python.exe" (
    echo [1/4] محیط پایتون از قبل وجود دارد.  ^(venv found^)
    goto install
)

where uv > nul 2> nul
if %errorlevel%==0 (
    echo [1/4] ساخت محیط پایتون 3.11 با uv ...
    uv venv --python 3.11 venv
    if errorlevel 1 goto nopython
    goto install
)

py -3.11 -c "import sys" > nul 2> nul
if %errorlevel%==0 (
    echo [1/4] ساخت محیط پایتون 3.11 ...
    py -3.11 -m venv venv
    if errorlevel 1 goto nopython
    goto install
)
goto nopython

:install
rem ---- 2) نصب کتابخانه ها با نسخه های قفل شده ----
echo [2/4] نصب کتابخانه ها (چند دقیقه) ...  ^(installing packages^)
where uv > nul 2> nul
if %errorlevel%==0 (
    uv pip install --python venv\Scripts\python.exe -r requirements.lock.txt
) else (
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r requirements.lock.txt
)
if errorlevel 1 (
    echo.
    echo [X] نصب کتابخانه ها ناموفق بود. اتصال اینترنت/VPN را بررسی کنید و دوباره SETUP.bat را اجرا کنید.
    pause
    exit /b 1
)

rem ---- 3) فایل تنظیمات محرمانه ----
if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo [3/4] فایل .env ساخته شد. کلید هوش مصنوعی TypeSafe را داخل آن وارد کنید.
) else (
    echo [3/4] فایل .env وجود دارد.
)

rem ---- 4) عیب یابی ----
echo [4/4] بررسی سلامت سامانه ...
echo.
set PYTHONIOENCODING=utf-8
venv\Scripts\python.exe brs.py doctor
echo.
echo ==============================================================
echo   نصب تمام شد. برای اجرای برنامه روی START.bat دوبار کلیک کنید.
echo ==============================================================
pause
exit /b 0

:nopython
echo.
echo [X] پایتون 3.11 روی این کامپیوتر پیدا نشد.  ^(Python 3.11 not found^)
echo     1. از صفحه ای که باز می شود Python 3.11 را دانلود و نصب کنید.
echo     2. هنگام نصب تیک "Add python.exe to PATH" را بزنید.
echo     3. دوباره روی SETUP.bat دوبار کلیک کنید.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1
